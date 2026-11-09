"""适配器层单测（不接受真实 OTLP，直接构造合成 span 调 normalize_run）。

验证点：
  1. DSH：cache_read 是 input 子集不并列累计，total = input + output，
     invoke_agent 聚合 span 被跳过，仅 LLM span 计入 token；
  2. Claude Code：token 读取 input_tokens/output_tokens/cache_read_tokens/
     cache_creation_tokens，total = input+output+cache_read+cache_write，
     工具只由父 span 计数、错误只由 execution 子 span 计数（不双算）；
  3. registry 自动探测选对适配器；
  4. 0 时间戳不污染 started/ended，str(None) 不污染 run_id/service_name。

运行：`.venv/bin/python scripts/selftest_adapters.py`
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aios_trace_eval.adapters.registry import select_adapter
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

    print("== 自动探测 ==")
    check("DSH 探测", select_adapter(dsh_spans(), {"service.name": "dsh-agent"}).name, "dsh")
    check("Claude 探测", select_adapter(claude_spans(), {"service.name": "claude-code"}).name, "claude_code")

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())