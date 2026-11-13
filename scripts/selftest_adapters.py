"""适配器层单测（不接受真实 OTLP，直接构造合成 span 调 normalize_run）。

验证点：
  1. DSH：cache_read 是 input 子集不并列累计，total = input + output，
     invoke_agent 聚合 span 被跳过，仅 LLM span 计入 token；
  2. Claude Code：token 读取 input_tokens/output_tokens/cache_read_tokens/
     cache_creation_tokens，total = input+output+cache_read+cache_write，
     工具只由父 span 计数、错误只由 execution 子 span 计数（不双算）；
  3. Codex：run_turn 根 span 判定 run trace、stream_request 计模型调用、
     codex.tool.phase=dispatch 计工具（unified_exec 子 span 不双算），
     token 从 exec JSONL 的 turn.completed 解析并按 input+output 汇总；
  4. registry 自动探测选对适配器；
  5. 0 时间戳不污染 started/ended，str(None) 不污染 run_id/service_name。

运行：`.venv/bin/python scripts/selftest_adapters.py`
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aios_trace_eval.adapters.registry import select_adapter
from aios_trace_eval.adapters.codex.normalizer import CodexAdapter
from aios_trace_eval.adapters.dsh.normalizer import DSHAdapter
from aios_trace_eval.adapters.claude_code.normalizer import ClaudeCodeAdapter

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def span(sid, name, attrs, *, status="unset", start=0, end=0, parent="", operation=None):
    a = dict(attrs)
    if operation:
        a["gen_ai.operation.name"] = operation
    return {
        "span_id": sid,
        "parent_span_id": parent,
        "name": name,
        "kind": "INTERNAL",
        "started_at_ns": start,
        "ended_at_ns": end,
        "duration_ms": (end - start) / 1_000_000 if end else 0.0,
        "status": status,
        "_attributes": a,
    }


def dsh_spans():
    # invoke_agent 携带整 turn 聚合 usage，必须被跳过；token 只从两个 chat 累加。
    return [
        span("e", "enter_ai_application_system", {}, start=1_000_000, end=900_000_000),
        span("ag", "invoke_agent", {
            "gen_ai.usage.input_tokens": 300, "gen_ai.usage.output_tokens": 200,
        }, start=2_000_000, end=890_000_000),
        span("st", "react step 1", {}, start=3_000_000, end=880_000_000),
        span("c1", "chat deepseek", {
            "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 80,
            "gen_ai.usage.cache_read.input_tokens": 30,
            "gen_ai.request.model": "deepseek-flash",
            "gen_ai.agent.id": "main",
        }, operation="chat", start=10_000_000, end=50_000_000),
        span("c2", "chat deepseek", {
            "gen_ai.usage.input_tokens": 50, "gen_ai.usage.output_tokens": 40,
            "gen_ai.request.model": "deepseek-flash",
        }, operation="chat", start=60_000_000, end=90_000_000),
        span("t1", "execute_tool bash", {}, operation="execute_tool",
             status="error", start=30_000_000, end=40_000_000),
    ]


def claude_spans():
    # 一条 llm_request 计一次 model_call；父 tool 计 tool_calls，execution 子 span 记 tool_errors。
    return [
        span("s0", "claude_code.interaction", {}, start=100_000_000, end=9_000_000_000),
        span("s1", "claude_code.llm_request", {
            "input_tokens": 1000, "output_tokens": 200,
            "cache_read_tokens": 300, "cache_creation_tokens": 50,
            "model": "claude-sonnet-4", "agent_id": "main", "session.id": "sess-1",
        }, start=200_000_000, end=5_000_000_000, parent="s0"),
        span("s2", "claude_code.llm_request", {
            "input_tokens": 500, "output_tokens": 100,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "model": "claude-sonnet-4",
        }, start=5_500_000_000, end=7_000_000_000, parent="s0"),
        span("s3", "claude_code.tool", {"tool_name": "read_file"},
             start=3_000_000_000, end=4_000_000_000, parent="s0"),
        span("s4", "claude_code.tool.execution", {"tool_name": "read_file"},
             status="error", start=3_200_000_000, end=3_900_000_000, parent="s3"),
    ]


def codex_spans():
    # run_turn 根 + 两次模型请求 + 一次工具 dispatch（unified_exec 子 span 不双算）。
    return [
        span("r", "run_turn", {
            "conversation.id": "conv-1", "turn.id": "turn-1",
        }, start=1_000_000, end=9_000_000_000),
        span("b", "built_tools", {"model": "gpt-5.6-sol"},
             start=1_500_000, end=1_600_000, parent="r"),
        span("m1", "stream_request", {}, start=2_000_000, end=5_000_000_000, parent="r"),
        span("m1x", "responses_websocket.stream_request", {},
             start=2_100_000, end=4_900_000_000, parent="m1"),
        span("t1", "exec_command", {
            "codex.tool.phase": "dispatch", "tool_name": "exec_command",
        }, status="error", start=5_100_000_000, end=5_400_000_000, parent="r"),
        span("t1x", "unified_exec.exec_command", {}, status="error",
             start=5_100_000_000, end=5_400_000_000, parent="t1"),
        span("m2", "stream_request", {}, start=6_000_000_000, end=8_000_000_000, parent="r"),
    ]


CODEX_EXEC_STDOUT = "\n".join([
    '{"type":"thread.started","thread_id":"01a1d070"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"我先运行命令。"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"OTLP_OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":16156,"cached_input_tokens":12672,'
    '"cache_write_input_tokens":0,"output_tokens":7,"reasoning_output_tokens":0}}',
])


def main() -> int:
    print("== DSH 适配器 ==")
    run = DSHAdapter().normalize_run(dsh_spans(), {"service.name": "dsh-agent"}, "trace_dsh")
    s = run.to_trace_summary()
    check("model_calls", s["model_calls"], 2)
    check("input_tokens", s["input_tokens"], 150)
    check("output_tokens", s["output_tokens"], 120)
    check("cache_read_tokens(子集不并列)", s["cache_read_tokens"], 0)
    check("total(input+output)", s["total_tokens"], 270)
    check("tool_calls", s["tool_calls"], 1)
    check("tool_errors", s["tool_errors"], 1)
    check("started 未被 0 污染", s["started_at_ns"], 1_000_000)

    print("== Claude Code 适配器 ==")
    run = ClaudeCodeAdapter().normalize_run(claude_spans(), {"service.name": "claude-code"}, "trace_cc")
    s = run.to_trace_summary()
    check("model", s["model"], "claude-sonnet-4")
    check("agent", s["agent"], "main")
    check("model_calls", s["model_calls"], 2)
    check("input_tokens", s["input_tokens"], 1500)
    check("output_tokens", s["output_tokens"], 300)
    check("cache_read_tokens", s["cache_read_tokens"], 300)
    check("cache_write_tokens", s["cache_write_tokens"], 50)
    check("total(四项之和)", s["total_tokens"], 2150)
    check("tool_calls(仅父 span)", s["tool_calls"], 1)
    check("tool_errors(仅 execution)", s["tool_errors"], 1)
    check("started 未被 0 污染", s["started_at_ns"], 100_000_000)

    print("== Codex 适配器 ==")
    adapter = CodexAdapter()
    resource = {"service.name": "codex_exec", "aios.run.id": "run_x", "aios.case.id": "case_x"}
    run = adapter.normalize_run(codex_spans(), resource, "trace_codex")
    s = run.to_trace_summary()
    check("model", s["model"], "gpt-5.6-sol")
    check("model_calls", s["model_calls"], 2)
    check("tool_calls(仅 dispatch)", s["tool_calls"], 1)
    check("tool_errors(仅 dispatch)", s["tool_errors"], 1)
    check("runtime_status(工具失败不阻断运行)", s["runtime_status"], "completed")
    check("session_context", adapter.capabilities.session_context, True)
    check("run_id(resource 优先)", s["run_id"], "run_x")
    check("case_id", s["case_id"], "case_x")
    check("span 不含 usage，token 保持 0", s["total_tokens"], 0)
    check("is_run_trace(run_turn 根)", adapter.is_run_trace(codex_spans()), True)
    check("is_run_trace(诊断 trace 丢弃)", adapter.is_run_trace(
        [span("d", "fs.read_file", {}, start=1, end=2)]), False)
    check("parse_output(最后一条 agent_message)", adapter.parse_output(CODEX_EXEC_STDOUT), "OTLP_OK")
    check("parse_execution_usage", adapter.parse_execution_usage(CODEX_EXEC_STDOUT), {
        "input_tokens": 16156, "output_tokens": 7, "cache_read_tokens": 12672,
        "cache_write_tokens": 0, "reasoning_tokens": 0,
    })
    check("parse_execution_usage(无事件返回 None)",
          adapter.parse_execution_usage("no events"), None)
    command = adapter.build_command({"input": "只回答：OTLP_OK"}, {
        "otlp_endpoint": "http://127.0.0.1:8080/", "profile": "", "model": "gpt-5.6-sol",
    })
    check("build_command(--json 非交互)", command[1:4], ["exec", "--skip-git-repo-check", "--json"])
    check("build_command(-c 注入完整 /v1/traces)",
          command[4:6], ["-c", "otel.trace_exporter={otlp-http={endpoint='http://127.0.0.1:8080/v1/traces',protocol='binary'}}"])
    check("build_command(模型 -m)", command[6:8], ["-m", "gpt-5.6-sol"])
    check("build_command(无 profile 默认不传)", any(x == "--profile" for x in command), False)

    print("== 自动探测 ==")
    check("DSH 探测", select_adapter(dsh_spans(), {"service.name": "dsh-agent"}).name, "dsh")
    check("Claude 探测", select_adapter(claude_spans(), {"service.name": "claude-code"}).name, "claude_code")
    check("Codex 探测(service)", select_adapter(codex_spans(), {"service.name": "codex_exec"}).name, "codex")
    check("Codex 探测(run_turn)", select_adapter(codex_spans(), {}).name, "codex")

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())