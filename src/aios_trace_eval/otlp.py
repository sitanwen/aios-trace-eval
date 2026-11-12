"""OTLP/HTTP protobuf decoding and OpenClaw GenAI trace normalization."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .adapters.registry import select_adapter


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


def decode_traces(payload: bytes, content_type: str,
                  trace_exists: Optional[Callable[[str], bool]] = None
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
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

    run_trace_ids = set()
    traces = []
    for trace_id, spans in grouped.items():
        resource = resources.get(trace_id, {})
        adapter = select_adapter(spans, resource)
        # 一条 trace 可能分多个 OTLP 批次到达；首个批次通过 is_run_trace 后，
        # 后续批次可能不含判定特征 span（如 codex 的 run_turn 只在首批），
        # 已入库的 trace 必须继续接收，否则会丢 span。
        known = trace_exists is not None and trace_exists(trace_id)
        if not adapter.is_run_trace(spans) and not known:
            continue
        run_trace_ids.add(trace_id)
        traces.append(normalize_trace_summary(spans, resource, trace_id))
    normalized_spans = [span for span in normalized_spans if span["trace_id"] in run_trace_ids]
    for span in normalized_spans:
        span.pop("_attributes", None)
    return traces, normalized_spans, process_samples


def normalize_trace_summary(spans: List[Dict[str, Any]], resource: Dict[str, Any], trace_id: str) -> Dict[str, Any]:
    """用适配器注册表归一化一条 trace，返回 storage.upsert_trace 期望的扁平 dict。"""
    adapter = select_adapter(spans, resource)
    summary = adapter.normalize_run(spans, resource, trace_id).to_trace_summary()
    # 采集来源由上游声明（resource 属性），平台入口可用请求头覆盖。
    summary["ingest_source"] = str(resource.get("aios.ingest.source") or "otlp")
    return summary


def _is_run_trace(spans: List[Dict[str, Any]]) -> bool:
    """兼容保留：现委托给 OpenClaw 适配器，与迁移前行为一致。"""
    from .adapters.openclaw.normalizer import OpenClawAdapter
    return OpenClawAdapter().is_run_trace(spans)


def summarize_trace(trace_id: str, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> Dict[str, Any]:
    """兼容保留：现委托给适配器注册表（对 OpenClaw trace 行为一致）。"""
    return normalize_trace_summary(spans, resource, trace_id)


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
