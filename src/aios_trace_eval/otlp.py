"""OTLP/HTTP protobuf decoding and OpenClaw GenAI trace normalization."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple


def _hex(value: bytes) -> str:
    return value.hex()


def _any_value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return value.bool_value
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        return value.double_value
    if kind == "bytes_value":
        return value.bytes_value.hex()
    if kind == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {item.key: _any_value(item.value) for item in value.kvlist_value.values}
    return None


def _attrs(values: Iterable[Any]) -> Dict[str, Any]:
    return {item.key: _any_value(item.value) for item in values}


def decode_traces(payload: bytes, content_type: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        from google.protobuf.json_format import Parse
    except ImportError as exc:
        raise RuntimeError("Install opentelemetry-proto and protobuf to receive OTLP") from exc

    request = ExportTraceServiceRequest()
    if "json" in content_type:
        Parse(payload.decode("utf-8"), request)
    else:
        request.ParseFromString(payload)

    normalized_spans: List[Dict[str, Any]] = []
    resources: Dict[str, Dict[str, Any]] = {}
    for resource_spans in request.resource_spans:
        resource = _attrs(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                trace_id = _hex(span.trace_id)
                attrs = _attrs(span.attributes)
                resources[trace_id] = resource
                normalized_spans.append({
                    "span_id": _hex(span.span_id),
                    "trace_id": trace_id,
                    "parent_span_id": _hex(span.parent_span_id),
                    "name": span.name,
                    "kind": str(span.kind),
                    "started_at_ns": span.start_time_unix_nano,
                    "ended_at_ns": span.end_time_unix_nano,
                    "duration_ms": max(0, (span.end_time_unix_nano - span.start_time_unix_nano) / 1_000_000),
                    "status": _status_name(span.status.code),
                    "attributes_json": json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
                    "_attributes": attrs,
                })

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for span in normalized_spans:
        grouped[span["trace_id"]].append(span)

    process_samples: List[Dict[str, Any]] = []
    for span in normalized_spans:
        attrs = span["_attributes"]
        resource = resources.get(span["trace_id"], {})
        for attribute, metric_name, unit in (
            ("openclaw.phase.cpu_core_ratio", "openclaw.phase.cpu_core_ratio", "1"),
            ("openclaw.phase.cpu_total_ms", "openclaw.phase.cpu_total_ms", "ms"),
            ("openclaw.liveness.cpu_core_ratio", "openclaw.liveness.cpu_core_ratio", "1"),
            ("openclaw.liveness.event_loop_utilization", "openclaw.liveness.event_loop_utilization", "1"),
        ):
            value = _number(attrs, attribute)
            if value is None:
                continue
            sample_attrs = {
                key: value for key, value in attrs.items()
                if key in {"openclaw.phase", "openclaw.liveness.reason"}
            }
            process_samples.append({
                "service_name": str(resource.get("service.name", "openclaw")),
                "metric_name": metric_name,
                "timestamp_ns": span["ended_at_ns"],
                "value": value,
                "count": 1,
                "sum_value": value,
                "min_value": value,
                "max_value": value,
                "unit": unit,
                "attributes_json": json.dumps(sample_attrs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            })

    run_groups = {
        trace_id: spans for trace_id, spans in grouped.items()
        if _is_run_trace(spans)
    }
    traces = [summarize_trace(trace_id, spans, resources.get(trace_id, {})) for trace_id, spans in run_groups.items()]
    normalized_spans = [span for span in normalized_spans if span["trace_id"] in run_groups]
    for span in normalized_spans:
        span.pop("_attributes", None)
    return traces, normalized_spans, process_samples


def _is_run_trace(spans: List[Dict[str, Any]]) -> bool:
    """Exclude exporter self-diagnostics and detached usage accounting.

    OpenClaw 8.2 emits diagnostic phases and ``openclaw.model.usage`` as
    independent trace IDs. They are telemetry about the runtime, not Agent
    Runs. Request traces already contain the same usage on model.call spans.
    """
    names = {span["name"] for span in spans}
    if names and all(name.startswith("openclaw.diagnostic.") for name in names):
        return False
    if names == {"openclaw.model.usage"}:
        return False
    return True


def summarize_trace(trace_id: str, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> Dict[str, Any]:
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
    run_id = case_id = experiment_id = version = None

    for span in spans:
        attrs = span["_attributes"]
        name = span["name"]
        operation = str(attrs.get("gen_ai.operation.name", ""))
        if name == "openclaw.model.call" or operation in {"chat", "generate_content", "text_completion", "invoke_agent"}:
            model_calls += 1
            input_tokens += _integer(attrs, "openclaw.model_call.usage.input_tokens", "gen_ai.usage.input_tokens")
            output_tokens += _integer(attrs, "openclaw.model_call.usage.output_tokens", "gen_ai.usage.output_tokens")
            cache_read += _integer(attrs, "openclaw.model_call.usage.cache_read_input_tokens", "gen_ai.usage.cache_read.input_tokens")
            cache_write += _integer(attrs, "openclaw.model_call.usage.cache_creation_input_tokens", "gen_ai.usage.cache_creation.input_tokens")
            reasoning += _integer(attrs, "openclaw.model_call.usage.reasoning_output_tokens")
            total += _integer(attrs, "openclaw.model_call.usage.total_tokens")
            if ttft_ms is None:
                ttft_ms = _number(attrs, "openclaw.model_call.time_to_first_byte_ms")
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

        model = model or _text(attrs, "openclaw.model", "gen_ai.request.model")
        provider = provider or _text(attrs, "openclaw.provider", "gen_ai.provider.name", "gen_ai.system")
        agent = agent or _text(attrs, "openclaw.agent", "gen_ai.agent.name")
        run_id = run_id or attrs.get("aios.run.id")
        case_id = case_id or attrs.get("aios.case.id")
        experiment_id = experiment_id or attrs.get("aios.experiment.id")
        version = version or attrs.get("aios.agent.version")
        if cost_usd is None:
            cost_usd = _number(attrs, "openclaw.cost.usd", "openclaw.cost_usd")

    # Usage accounting span is the fallback only when request spans have no usage.
    if not (input_tokens or output_tokens or total):
        for span in spans:
            if span["name"] != "openclaw.model.usage":
                continue
            attrs = span["_attributes"]
            input_tokens += _integer(attrs, "openclaw.tokens.input", "gen_ai.usage.input_tokens")
            output_tokens += _integer(attrs, "openclaw.tokens.output", "gen_ai.usage.output_tokens")
            cache_read += _integer(attrs, "openclaw.tokens.cache_read", "gen_ai.usage.cache_read.input_tokens")
            cache_write += _integer(attrs, "openclaw.tokens.cache_write", "gen_ai.usage.cache_creation.input_tokens")
            total += _integer(attrs, "openclaw.tokens.total")

    total = total or input_tokens + output_tokens + cache_read + cache_write + reasoning
    return {
        "trace_id": trace_id,
        "run_id": str(run_id) if run_id else trace_id,
        "service_name": str(resource.get("service.name", "openclaw")),
        "case_id": str(case_id) if case_id else None,
        "experiment_id": str(experiment_id) if experiment_id else None,
        "version": str(version) if version else None,
        "agent": agent or None,
        "provider": provider or None,
        "model": model or None,
        "started_at_ns": started,
        "ended_at_ns": ended,
        "duration_ms": round((ended - started) / 1_000_000, 3),
        "runtime_status": runtime_status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "cost_usd": cost_usd,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_loops": loops,
        "failovers": failovers,
        "ttft_ms": ttft_ms,
    }


def _status_name(code: int) -> str:
    return {0: "unset", 1: "ok", 2: "error"}.get(code, "unset")


def _integer(attrs: Dict[str, Any], *keys: str) -> int:
    value = _number(attrs, *keys)
    return int(value) if value is not None else 0


def _number(attrs: Dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in attrs:
            continue
        try:
            return float(attrs[key])
        except (TypeError, ValueError):
            pass
    return None


def _text(attrs: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            return str(value)
    return ""
