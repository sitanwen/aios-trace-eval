"""OpenClaw 平台归一化适配器。

把原 otlp.py 中 OpenClaw 专用的 summarize_trace / _is_run_trace 逻辑迁入此处，
行为与迁移前完全一致：

- 读取 openclaw.* 私有字段 + gen_ai.* 标准字段；
- model.call / tool.execution / tool.loop / model.failover 计数；
- token 口径：total = total_tokens 或 input+output+cache_read+cache_write+reasoning；
- usage 缺失时回退到 openclaw.model.usage 独立 accounting span；
- 排除 openclaw.diagnostic.* 自诊断与独立 openclaw.model.usage trace。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...canonical import CanonicalRun, CanonicalTokens
from .. import _spanutil as su
from ..base import AdapterCapabilities, AgentAdapter

_LLM_OPERATIONS = {"chat", "generate_content", "text_completion", "invoke_agent"}


class OpenClawAdapter(AgentAdapter):
    """OpenClaw（2026.8.2）适配器。"""

    name = "openclaw"
    capabilities = AdapterCapabilities(
        execute=True,
        process_metrics=True,
        execution_modes=("gateway", "observe_only"),
    )

    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        for span in spans:
            if str(span.get("name") or "").startswith("openclaw."):
                return 0.95
        # 无私有 span 名时不强抢，交给 generic（标准 gen_ai.*）
        return 0.0

    def is_run_trace(self, spans: List[Dict[str, Any]]) -> bool:
        names = {span.get("name") for span in spans}
        if names and all(str(name).startswith("openclaw.diagnostic.") for name in names):
            return False
        if names == {"openclaw.model.usage"}:
            return False
        return True

    def normalize_run(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
        trace_id: str,
    ) -> CanonicalRun:
        started = min(span["started_at_ns"] for span in spans)
        ended = max(span["ended_at_ns"] for span in spans)
        model_calls = 0
        tool_calls = 0
        tool_errors = 0
        loops = 0
        failovers = 0
        input_tokens = output_tokens = cache_read = cache_write = reasoning = total = 0
        model = provider = agent = ""
        runtime_status = "completed"
        cost_usd = None
        ttft_ms = None
        run_id = resource.get("aios.run.id")
        case_id = resource.get("aios.case.id")
        experiment_id = resource.get("aios.experiment.id")
        version = resource.get("aios.agent.version")

        for span in spans:
            attrs = su.attrs(span)
            name = span.get("name", "")
            operation = str(attrs.get("gen_ai.operation.name", ""))
            if name == "openclaw.model.call" or operation in _LLM_OPERATIONS:
                model_calls += 1
                input_tokens += su.integer(attrs, "openclaw.model_call.usage.input_tokens", "gen_ai.usage.input_tokens")
                output_tokens += su.integer(attrs, "openclaw.model_call.usage.output_tokens", "gen_ai.usage.output_tokens")
                cache_read += su.integer(attrs, "openclaw.model_call.usage.cache_read_input_tokens", "gen_ai.usage.cache_read.input_tokens")
                cache_write += su.integer(attrs, "openclaw.model_call.usage.cache_creation_input_tokens", "gen_ai.usage.cache_creation.input_tokens")
                reasoning += su.integer(attrs, "openclaw.model_call.usage.reasoning_output_tokens")
                total += su.integer(attrs, "openclaw.model_call.usage.total_tokens")
                if ttft_ms is None:
                    ttft_ms = su.number(attrs, "openclaw.model_call.time_to_first_byte_ms")
            if name == "openclaw.tool.execution" or operation == "execute_tool":
                tool_calls += 1
                if span["status"] == "error" or attrs.get("openclaw.outcome") in {"error", "blocked"}:
                    tool_errors += 1
            if name == "openclaw.tool.loop":
                loops += 1
            if name == "openclaw.model.failover":
                failovers += 1
            if span["status"] == "error" or attrs.get("openclaw.outcome") == "error":
                runtime_status = "error"

            model = model or su.text(attrs, "openclaw.model", "gen_ai.request.model")
            provider = provider or su.text(attrs, "openclaw.provider", "gen_ai.provider.name", "gen_ai.system")
            agent = agent or su.text(attrs, "openclaw.agent", "gen_ai.agent.name")
            run_id = run_id or attrs.get("aios.run.id")
            case_id = case_id or attrs.get("aios.case.id")
            experiment_id = experiment_id or attrs.get("aios.experiment.id")
            version = version or attrs.get("aios.agent.version")
            if cost_usd is None:
                cost_usd = su.number(attrs, "openclaw.cost.usd", "openclaw.cost_usd")

        # Usage accounting span 是回退：仅当请求 span 没有 usage 时使用。
        if not (input_tokens or output_tokens or total):
            for span in spans:
                if span.get("name") != "openclaw.model.usage":
                    continue
                attrs = su.attrs(span)
                input_tokens += su.integer(attrs, "openclaw.tokens.input", "gen_ai.usage.input_tokens")
                output_tokens += su.integer(attrs, "openclaw.tokens.output", "gen_ai.usage.output_tokens")
                cache_read += su.integer(attrs, "openclaw.tokens.cache_read", "gen_ai.usage.cache_read.input_tokens")
                cache_write += su.integer(attrs, "openclaw.tokens.cache_write", "gen_ai.usage.cache_creation.input_tokens")
                total += su.integer(attrs, "openclaw.tokens.total")

        total = total or input_tokens + output_tokens + cache_read + cache_write + reasoning

        return CanonicalRun(
            trace_id=trace_id,
            service_name=str(resource.get("service.name", "openclaw")),
            run_id=str(run_id) if run_id else trace_id,
            case_id=str(case_id) if case_id else None,
            experiment_id=str(experiment_id) if experiment_id else None,
            version=str(version) if version else None,
            agent=agent or None,
            provider=provider or None,
            model=model or None,
            started_at_ns=started,
            ended_at_ns=ended,
            duration_ms=round((ended - started) / 1_000_000, 3),
            runtime_status=runtime_status,
            tokens=CanonicalTokens(
                input=input_tokens,
                output=output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
                reasoning=reasoning,
                total=total,
            ),
            cost_usd=cost_usd,
            model_calls=model_calls,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            tool_loops=loops,
            failovers=failovers,
            ttft_ms=ttft_ms,
        )
