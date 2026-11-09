"""通用 GenAI 归一化适配器。

仅依据 OpenTelemetry GenAI 标准语义字段（gen_ai.*）归一化，不读取任何
厂商私有字段。任何严格遵循 GenAI 语义约定、通过 OTLP 上报的平台都可由
本适配器直接接入，无需新增代码，例如：

- 通过 opentelemetry-instrumentation-openai 自动埋点、base_url 指向
  DeepSeek 的应用；
- DeepSeek Harness（DSH）dsh-plugin 等输出标准 GenAI span 的运行时。

厂商私有 span 名与属性由各自专用适配器处理，并在标准字段缺失时兜底。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...canonical import CanonicalRun, CanonicalSpan, CanonicalTokens
from ..base import AgentAdapter

# GenAI 操作类型
_LLM_OPERATIONS = {"chat", "generate_content", "text_completion", "invoke_agent"}
_TOOL_OPERATION = "execute_tool"


def _attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    parsed = span.get("_attributes")
    if isinstance(parsed, dict):
        return parsed
    import json

    raw = span.get("attributes_json", "{}")
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return {}


def _number(attrs: Dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in attrs:
            continue
        try:
            return float(attrs[key])
        except (TypeError, ValueError):
            continue
    return None


def _integer(attrs: Dict[str, Any], *keys: str) -> int:
    value = _number(attrs, *keys)
    return int(value) if value is not None else 0


def _text(attrs: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            return str(value)
    return ""


class GenericGenAIAdapter(AgentAdapter):
    """标准 GenAI 语义的平台无关适配器，同时作为自动探测的兜底。"""

    name = "generic"

    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        for span in spans:
            attrs = _attrs(span)
            if "gen_ai.operation.name" in attrs or "gen_ai.usage.input_tokens" in attrs:
                return 0.25
        return 0.1

    def normalize_run(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
        trace_id: str,
    ) -> CanonicalRun:
        tokens = CanonicalTokens()
        model_calls = tool_calls = tool_errors = 0
        runtime_status = "completed"
        model = provider = agent = ""
        ttft_ms = None
        canonical_spans: List[CanonicalSpan] = []

        for span in spans:
            attrs = _attrs(span)
            operation = str(attrs.get("gen_ai.operation.name", ""))
            canonical_spans.append(
                CanonicalSpan(
                    span_id=str(span.get("span_id", "")),
                    parent_span_id=str(span.get("parent_span_id", "")),
                    name=str(span.get("name", "")),
                    kind=str(span.get("kind", "")),
                    started_at_ns=int(span.get("started_at_ns", 0) or 0),
                    ended_at_ns=int(span.get("ended_at_ns", 0) or 0),
                    duration_ms=float(span.get("duration_ms", 0.0) or 0.0),
                    status=str(span.get("status", "unset")),
                    attributes=attrs,
                )
            )

            if operation in _LLM_OPERATIONS:
                model_calls += 1
                tokens.input += _integer(attrs, "gen_ai.usage.input_tokens")
                tokens.output += _integer(attrs, "gen_ai.usage.output_tokens")
                tokens.cache_read += _integer(
                    attrs, "gen_ai.usage.cache_read.input_tokens"
                )
                tokens.cache_write += _integer(
                    attrs, "gen_ai.usage.cache_creation.input_tokens"
                )
                if ttft_ms is None:
                    ttft_ms = _number(
                        attrs,
                        "gen_ai.client.time_to_first_token",
                        "gen_ai.time_to_first_token_ms",
                    )

            if operation == _TOOL_OPERATION:
                tool_calls += 1
                if span.get("status") == "error":
                    tool_errors += 1

            if span.get("status") == "error":
                runtime_status = "error"

            model = model or _text(attrs, "gen_ai.request.model")
            provider = provider or _text(
                attrs, "gen_ai.provider.name", "gen_ai.system"
            )
            agent = agent or _text(attrs, "gen_ai.agent.name")

        tokens.total = (
            tokens.input + tokens.output + tokens.cache_read
            + tokens.cache_write + tokens.reasoning
        )

        started = min((s.started_at_ns for s in canonical_spans), default=0)
        ended = max((s.ended_at_ns for s in canonical_spans), default=0)
        run = CanonicalRun(
            trace_id=trace_id,
            service_name=str(resource.get("service.name", "")),
            run_id=str(resource.get("aios.run.id", "")) or trace_id,
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
        return run
