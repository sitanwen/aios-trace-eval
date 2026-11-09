"""向运行中的服务 POST 一条合成 Claude Code OTLP trace，验证多平台适配器接入。

前提：服务已启动（默认 http://127.0.0.1:8080）。
运行：`.venv/bin/python scripts/post_synthetic_trace.py`
推送后，打开「运行记录」应能看到一条 model=claude-sonnet-4、token 正确归一的 trace。
"""
from __future__ import annotations

import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status
from opentelemetry.proto.common.v1.common_pb2 import KeyValue, AnyValue, InstrumentationScope
from opentelemetry.proto.resource.v1.resource_pb2 import Resource


def kv(key: str, value) -> KeyValue:
    any_value = AnyValue()
    if isinstance(value, int):
        any_value.int_value = value
    elif isinstance(value, str):
        any_value.string_value = value
    elif isinstance(value, float):
        any_value.double_value = value
    return KeyValue(key=key, value=any_value)


def span(name: str, attrs: dict, sid: bytes, start_ns: int, end_ns: int,
         code: int = 1, parent: bytes = b"") -> Span:
    return Span(
        trace_id=b"\x11" * 16,
        span_id=sid,
        parent_span_id=parent or b"\x00" * 8,
        name=name,
        kind=1,  # INTERNAL
        start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
        attributes=[kv(k, v) for k, v in attrs.items()],
        status=Status(code=code),
    )


def build_request() -> bytes:
    spans = [
        span("claude_code.interaction", {}, b"\x00" * 8, 100, 9000),
        span("claude_code.llm_request", {
            "input_tokens": 1000, "output_tokens": 200,
            "cache_read_tokens": 300, "cache_creation_tokens": 50,
            "model": "claude-sonnet-4", "agent_id": "main", "session.id": "s1",
        }, b"\x01" * 8, 200, 5000, parent=b"\x00" * 8),
        span("claude_code.tool", {"tool_name": "read_file"},
             b"\x02" * 8, 3000, 4000, parent=b"\x00" * 8),
        span("claude_code.tool.execution", {"tool_name": "read_file"},
             b"\x03" * 8, 3200, 3900, code=2, parent=b"\x02" * 8),
    ]
    request = ExportTraceServiceRequest(resource_spans=[
        ResourceSpans(
            resource=Resource(attributes=[kv("service.name", "claude-code")]),
            scope_spans=[ScopeSpans(scope=InstrumentationScope(name="synthetic"), spans=spans)],
        )
    ])
    return request.SerializeToString()


def main() -> int:
    base = os.environ.get("AIOS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    payload = build_request()
    req = urllib.request.Request(
        base + "/v1/traces", data=payload, method="POST",
        headers={"Content-Type": "application/x-protobuf"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"POST {base}/v1/traces -> HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}")
        return 1
    print("已推送合成 Claude Code trace（1 个 interaction + 1 个 llm_request + 1 个 tool + 1 个 tool.execution[error]）")
    print("去「运行记录」看 model=claude-sonnet-4、total_tokens=1550 的那条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())