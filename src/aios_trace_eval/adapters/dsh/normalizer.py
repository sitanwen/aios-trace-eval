"""DSH 平台归一化适配器。

处理 DSH 特有的 span 层级与 token 口径：
- 仅从 chat（LLM）span 累加 token，忽略 invoke_agent（AGENT）聚合 span，
  避免整 turn 用量被重复计算；
- cache_read 是 input 的子集，total = input + output，不重复加缓存；
- 工具错误以 execute_tool span 的 error 状态为准。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...canonical import CanonicalRun, CanonicalSpan, CanonicalTokens
from .. import _spanutil as su
from ..base import AdapterCapabilities, AgentAdapter
from . import fields as F


class DSHAdapter(AgentAdapter):
    """DeepSeek Harness 适配器。"""

    name = "dsh"
    capabilities = AdapterCapabilities(
        execute=True,
        process_metrics=True,
        session_context=True,
        execution_modes=("local_cli", "remote_worker", "observe_only"),
    )

    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        for span in spans:
            a = su.attrs(span)
            if a.get(F.ATTR_TURN_ID) or a.get(F.ATTR_AGENT_ID) == "deepseek-harness":
                return 0.9
            name = span.get("name", "")
            if name.startswith(F.SPAN_STEP_PREFIX) or name == F.SPAN_ENTRY:
                return 0.8
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
        model = provider = agent = session_id = turn_id = ""
        ttft_ms = None
        canonical_spans: List[CanonicalSpan] = []

        for span in spans:
            a = su.attrs(span)
            name = str(span.get("name", ""))
            operation = str(a.get("gen_ai.operation.name", ""))
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

            is_llm = operation == F.OP_LLM_CHAT or name.startswith(F.SPAN_LLM_PREFIX)
            is_tool = (
                operation == F.OP_EXECUTE_TOOL
                or name.startswith(F.SPAN_TOOL_PREFIX)
            )

            # 仅 LLM span 计入 token；invoke_agent 聚合 span 显式跳过
            if is_llm and name != F.SPAN_AGENT:
                model_calls += 1
                # DSH 口径：cache_read/cache_write 是 input_tokens 的子集，不是
                # 并列项，不能单独累加到 tokens.cache_*，否则缓存会被重复计入。
                # input_tokens 已包含缓存，此处只累加 input/output。
                tokens.input += su.integer(a, F.ATTR_USAGE_INPUT)
                tokens.output += su.integer(a, F.ATTR_USAGE_OUTPUT)
                if ttft_ms is None:
                    # gen_ai.response.time_to_first_token 单位为纳秒，换算成毫秒
                    _ttft_ns = su.number(a, F.ATTR_TTFT)
                    if _ttft_ns is not None:
                        ttft_ms = _ttft_ns / 1_000_000

            if is_tool:
                tool_calls += 1
                if span.get("status") == "error":
                    tool_errors += 1

            if span.get("status") == "error":
                runtime_status = "error"

            model = model or su.text(a, "gen_ai.request.model")
            provider = provider or su.text(
                a, "gen_ai.provider.name", "gen_ai.system"
            )
            agent = agent or su.text(a, F.ATTR_AGENT_ID)
            session_id = session_id or su.text(a, F.ATTR_SESSION_ID)
            turn_id = turn_id or su.text(a, F.ATTR_TURN_ID)

        # DSH 口径：cache_read 已含在 input，total = input + output
        tokens.total = tokens.input + tokens.output

        # 过滤时间戳为 0 的 span（OTLP 中 start/end_time_unix_nano 可能为 0），
        # 否则 min(started) 会被 0 拉低、max(ended) 相对可靠但会污染 duration。
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
            service_name=str(resource.get("service.name") or ""),
            run_id=str(resource.get("aios.run.id") or "") or turn_id or trace_id,
            case_id=resource.get("aios.case.id"),
            experiment_id=resource.get("aios.experiment.id"),
            version=resource.get("aios.agent.version"),
            agent=agent or None,
            provider=provider or None,
            model=model or None,
            started_at_ns=started,
            ended_at_ns=ended,
            duration_ms=round((ended - started) / 1_000_000, 3) if started and ended else 0.0,
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
        # headless profile：新建持久化会话，打印最终回答后退出，适用于批量评测
        profile = str(context.get("profile", "headless"))
        command = [str(context.get("executable") or "dsh"), "--profile", profile]
        extra = context.get("extra_args")
        if extra:
            command.extend([str(x) for x in extra])
        command.append(str(case["input"]))
        return command
