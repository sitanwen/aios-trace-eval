"""Claude Code 平台归一化适配器。

依据 claude_code.* 私有 span 名识别交互、模型请求与工具调用，token 从
llm_request span 的 input_tokens / output_tokens / cache_read_tokens /
cache_creation_tokens 属性读取（Claude Code 不设置 gen_ai.usage.*）。Anthropic
口径下 cache_read/cache_write 独立于 input，
total = input + output + cache_read + cache_write，与 DSH 不同。

工具仅以 claude_code.tool 计数，blocked_on_user/execution 为其子 span，
不重复统计。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...canonical import CanonicalRun, CanonicalSpan, CanonicalTokens
from .. import _spanutil as su
from ..base import AdapterCapabilities, AgentAdapter
from . import fields as F


class ClaudeCodeAdapter(AgentAdapter):
    """Claude Code 适配器。"""

    name = "claude_code"
    capabilities = AdapterCapabilities(
        execute=True,
        process_metrics=True,
        session_context=True,
        execution_modes=("local_cli", "remote_worker", "observe_only"),
    )

    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        for span in spans:
            name = str(span.get("name") or "")
            if name.startswith("claude_code."):
                return 0.9
            if su.attrs(span).get(F.ATTR_SESSION_ID) and resource.get(
                "service.name"
            ) == F.DEFAULT_SERVICE_NAME:
                return 0.7
        return 0.0

    def normalize_run(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
        trace_id: str,
    ) -> CanonicalRun:
        tokens = CanonicalTokens()
        model_calls = tool_calls = tool_errors = 0
        runtime_status = "completed"
        model = provider = agent = session_id = ""
        ttft_ms = None
        canonical_spans: List[CanonicalSpan] = []

        for span in spans:
            a = su.attrs(span)
            name = str(span.get("name", ""))
            canonical_spans.append(
                CanonicalSpan(
                    span_id=str(span.get("span_id", "")),
                    parent_span_id=str(span.get("parent_span_id", "")),
                    name=name,
                    kind=str(span.get("kind", "")),
                    started_at_ns=int(span.get("started_at_ns", 0) or 0),
                    ended_at_ns=int(span.get("ended_at_ns", 0) or 0),
                    duration_ms=float(span.get("duration_ms", 0.0) or 0.0),
                    status=str(span.get("status", "unset")),
                    attributes=a,
                )
            )

            if name == F.SPAN_LLM_REQUEST:
                model_calls += 1
                tokens.input += su.integer(a, F.ATTR_USAGE_INPUT)
                tokens.output += su.integer(a, F.ATTR_USAGE_OUTPUT)
                tokens.cache_read += su.integer(a, F.ATTR_USAGE_CACHE_READ)
                tokens.cache_write += su.integer(a, F.ATTR_USAGE_CACHE_WRITE)
                if ttft_ms is None:
                    ttft_ms = su.number(a, "ttft_ms", "first_content_ms")

            # 工具仅以父 span 计数；blocked_on_user/execution 为子 span，不重复统计。
            # 执行结果（success/error）只挂在 execution 子 span 上，因此 tool_errors
            # 只从 execution 子 span 累加，避免父、子双算。
            if name == F.SPAN_TOOL:
                tool_calls += 1
            elif name == F.SPAN_TOOL_EXECUTION and span.get("status") == "error":
                tool_errors += 1

            if span.get("status") == "error":
                runtime_status = "error"

            model = model or su.text(a, "model", "gen_ai.request.model")
            provider = provider or su.text(
                a, "gen_ai.provider.name", "gen_ai.system"
            )
            agent = agent or su.text(a, F.ATTR_AGENT_ID)
            session_id = session_id or su.text(a, F.ATTR_SESSION_ID)

        # Anthropic 口径：缓存项独立于 input，total 为各分类之和
        tokens.total = (
            tokens.input + tokens.output + tokens.cache_read + tokens.cache_write
        )

        started = min(
            (s.started_at_ns for s in canonical_spans if s.started_at_ns > 0),
            default=0,
        )
        ended = max(
            (s.ended_at_ns for s in canonical_spans if s.ended_at_ns > 0),
            default=0,
        )
        return CanonicalRun(
            trace_id=trace_id,
            service_name=str(resource.get("service.name", F.DEFAULT_SERVICE_NAME)),
            run_id=str(resource.get("aios.run.id", "")) or session_id or trace_id,
            case_id=resource.get("aios.case.id"),
            experiment_id=resource.get("aios.experiment.id"),
            version=resource.get("aios.agent.version"),
            agent=agent or None,
            provider=provider or None,
            model=model or None,
            started_at_ns=started,
            ended_at_ns=ended,
            duration_ms=round((ended - started) / 1_000_000, 3) if ended else 0.0,
            runtime_status=runtime_status,
            tokens=tokens,
            model_calls=model_calls,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            ttft_ms=ttft_ms,
            spans=canonical_spans,
        )

    def build_command(
        self,
        case: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[str]:
        # 非交互评测：claude -p/--print；内容与遥测由环境变量控制
        command = [str(context.get("executable") or "claude"), "--print"]
        model = context.get("model")
        if model:
            command.extend(["--model", str(model)])
        extra = context.get("extra_args")
        if extra:
            command.extend([str(x) for x in extra])
        command.append(str(case["input"]))
        return command
