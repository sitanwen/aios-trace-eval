"""Decode OTLP metrics into timestamped process-level samples."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from .otlp import _attrs


def decode_metrics(payload: bytes, content_type: str) -> List[Dict[str, Any]]:
    try:
        from google.protobuf.json_format import Parse
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
    except ImportError as exc:
        raise RuntimeError("Install opentelemetry-proto and protobuf to receive OTLP") from exc

    request = ExportMetricsServiceRequest()
    if "json" in content_type:
        Parse(payload.decode("utf-8"), request)
    else:
        request.ParseFromString(payload)

    samples: List[Dict[str, Any]] = []
    for resource_metrics in request.resource_metrics:
        resource = _attrs(resource_metrics.resource.attributes)
        service_name = str(resource.get("service.name", "openclaw"))
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                data_kind = metric.WhichOneof("data")
                data = getattr(metric, data_kind) if data_kind else None
                if data is None:
                    continue
                points: Iterable[Any] = getattr(data, "data_points", ())
                for point in points:
                    sample = _normalize_point(service_name, metric, data_kind, point)
                    if sample is not None:
                        samples.append(sample)
    return samples


def _normalize_point(service_name: str, metric: Any, kind: str, point: Any) -> Dict[str, Any] | None:
    count = None
    sum_value = min_value = max_value = None
    if kind in {"gauge", "sum"}:
        number_kind = point.WhichOneof("value")
        if not number_kind:
            return None
        value = float(getattr(point, number_kind))
    elif kind == "histogram":
        count = int(point.count)
        sum_value = float(point.sum)
        if not count:
            return None
        min_value = float(point.min) if point.HasField("min") else None
        max_value = float(point.max) if point.HasField("max") else None
        value = sum_value / count
    else:
        return None

    attrs = _attrs(point.attributes)
    return {
        "service_name": service_name,
        "metric_name": metric.name,
        "timestamp_ns": int(point.time_unix_nano or point.start_time_unix_nano),
        "value": value,
        "count": count,
        "sum_value": sum_value,
        "min_value": min_value,
        "max_value": max_value,
        "unit": metric.unit or "",
        "attributes_json": json.dumps(attrs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
