"""自测：用构造的 Jaeger 风格 trace 验证指标提取与聚合。"""
import json
import sys

sys.path.insert(0, "src")

from aios_trace_eval.adapters.hermes.normalizer import HermesAdapter
from aios_trace_eval.adapters.registry import select_adapter
from aios_trace_eval.extractor import extract_run_metrics, aggregate
from aios_trace_eval.otlp import _is_run_trace
from aios_trace_eval.evalset import EvalCase, RuleAssertion
from aios_trace_eval.judge import judge_case, evaluate_set, build_report
from aios_trace_eval.code_eval import UnsafeExpression, evaluate_expression, normalize_number
from aios_trace_eval.experiment_runner import _adapter_name_for_protocol
from aios_trace_eval.runner import RunResult


def make_trace():
    """构造一条包含 run + 2 次 model.call + usage 的 Jaeger 风格 trace。"""
    def span(name, tags, duration_us):
        return {"operationName": name, "tags": tags, "duration": duration_us}

    return {
        "traceID": "abc123",
        "spans": [
            span("openclaw.run", {"openclaw.agent": "main"}, 5_000_000),
            span(
                "openclaw.model.call",
                {
                    "openclaw.provider": "anthropic",
                    "openclaw.model": "claude",
                    "openclaw.tokens.input": "1200",
                    "openclaw.tokens.output": "800",
                    "openclaw.tokens.cache_read": "300",
                    "openclaw.tokens.cache_write": "100",
                    "openclaw.tokens.total": "2400",
                    "openclaw.model_call.time_to_first_byte_ms": "650",
                },
                2_000_000,
            ),
            span(
                "openclaw.model.call",
                {
                    "openclaw.tokens.input": "600",
                    "openclaw.tokens.output": "400",
                    "openclaw.tokens.total": "1000",
                },
                1_000_000,
            ),
            span(
                "openclaw.model.usage",
                {"openclaw.tokens.total": "3400"},
                3_000_000,
            ),
            span(
                "openclaw.message.processed",
                {"openclaw.outcome": "completed"},
                1000,
            ),
        ],
    }


def test_hermes_adapter():
    def span(span_id, name, attrs, start, end, status="ok"):
        return {
            "span_id": span_id,
            "parent_span_id": "",
            "name": name,
            "started_at_ns": start,
            "ended_at_ns": end,
            "duration_ms": (end - start) / 1_000_000,
            "status": status,
            "attributes_json": json.dumps(attrs),
        }

    spans = [
        span("h1", "hermes.session", {
            "hermes.span.kind": "AGENT", "hermes.session.id": "sess-hermes",
        }, 1_000_000_000, 5_000_000_000),
        span("h2", "hermes.turn", {
            "hermes.span.kind": "CHAIN", "hermes.turn.index": 0,
        }, 1_100_000_000, 4_900_000_000),
        span("h3", "chat test-model", {
            "hermes.span.kind": "LLM",
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "hermes-agent",
            "gen_ai.request.model": "test-model",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
            "hermes.cache.read_tokens": 20,
            "hermes.cache.write_tokens": 10,
            "hermes.reasoning_tokens": 5,
            "hermes.provider": "openai",
        }, 1_200_000_000, 2_000_000_000),
        span("h4", "hermes.tool.shell", {
            "hermes.span.kind": "TOOL", "hermes.tool.name": "shell",
        }, 2_100_000_000, 2_500_000_000, "error"),
    ]
    adapter = HermesAdapter()
    run = adapter.normalize_run(
        spans, {"service.name": "hermes-agent", "aios.run.id": "run-hermes"}, "trace-hermes"
    )
    assert run.run_id == "run-hermes"
    assert run.model == "test-model" and run.provider == "openai"
    assert (run.tokens.input, run.tokens.output) == (100, 50)
    assert (run.tokens.cache_read, run.tokens.cache_write) == (20, 10)
    assert run.tokens.reasoning == 5
    # Hermes 口径：reasoning 已含在 output，不重复加
    assert run.tokens.total == 180, f"total={run.tokens.total}"
    assert run.model_calls == 1
    assert run.tool_calls == 1 and run.tool_errors == 1
    # 工具失败单独计数，不把整次 run 标成 runtime_error
    assert run.runtime_status == "completed"
    assert run.duration_ms == 4000.0
    assert select_adapter(spans, {"service.name": "hermes-agent"}).name == "hermes"
    stdout = (
        '{"type":"system","subtype":"init"}\n'
        '{"type":"text","text":"partial"}\n'
        '{"type":"result","text":"final answer"}\n'
    )
    assert adapter.parse_output(stdout) == "final answer"
    noisy = "  ⚠ tirith security scanner enabled but not available — x\n第一行\n第二行\nsession_id: s1\n"
    assert adapter.parse_output(noisy) == "第一行\n第二行"
    command = adapter.build_command(
        {"input": "hello"}, {"model": "test-model", "profile": "", "extra_args": []}
    )
    assert command == [
        "hermes", "chat", "-Q", "--model", "test-model", "-q", "hello",
    ], command
    assert _adapter_name_for_protocol("hermes") == "hermes"


def main():
    test_hermes_adapter()
    assert not _is_run_trace([{"name": "openclaw.diagnostic.phase"}])
    assert not _is_run_trace([{"name": "openclaw.model.usage"}])
    assert _is_run_trace([{"name": "openclaw.run"}, {"name": "openclaw.model.call"}])
    trace = make_trace()
    m = extract_run_metrics(trace)

    assert m.trace_id == "abc123"
    assert m.model_call_count == 2
    assert m.tokens.input == 1800, f"input={m.tokens.input}"
    assert m.tokens.output == 1200, f"output={m.tokens.output}"
    assert m.tokens.cache_read == 300
    assert m.tokens.cache_write == 100
    assert m.tokens.total == 3400, f"total={m.tokens.total}"
    assert m.ttft_ms == 650
    assert m.outcome == "completed"
    assert m.duration_ms is not None
    print("extractor OK:", json.dumps(m.__dict__, default=str, ensure_ascii=False))

    agg = aggregate([m])
    assert agg["successful_runs"] == 1
    assert agg["tokens"]["total"] == 3400
    assert agg["tokens_per_successful_run"]["total"] == 3400
    print("aggregate OK:", json.dumps(agg, ensure_ascii=False))

    case = EvalCase(
        id="echo_basic",
        query="请回复 PONG",
        rule_assertions=[
            RuleAssertion(name="tokens_bounded", field="tokens.total", operator="lt", value=100000),
        ],
    )
    rr = RunResult(case_id="echo_basic", attempt=0, metrics=m)
    v = judge_case(case, rr)
    assert v.passed, v.fail_reasons
    print("judge OK:", json.dumps(v.__dict__, ensure_ascii=False))

    summary = evaluate_set(case, [rr])
    assert summary["pass_k"] == 1.0 and summary["pass_hat_k"] == 1.0
    report = build_report("smoke", [summary], [m])
    print("report OK:", json.dumps(report, ensure_ascii=False, indent=2))

    assert normalize_number("answer: 1,200.00") == "1200"
    assert evaluate_expression(
        "normalize_number(response) == normalize_number(reference)",
        {"query": "", "response": "The answer is 18", "reference": "18", "trace": {}, "trajectory": []},
    ) is True
    try:
        evaluate_expression("__import__('os').system('whoami')", {})
        raise AssertionError("unsafe expression was accepted")
    except UnsafeExpression:
        pass

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
