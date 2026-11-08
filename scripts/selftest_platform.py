"""End-to-end smoke test for OTLP ingestion, storage and platform API."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["AIOS_DB_PATH"] = str(Path(tempfile.gettempdir()) / "aios-trace-eval-selftest.db")
try:
    Path(os.environ["AIOS_DB_PATH"]).unlink()
except FileNotFoundError:
    pass

import sys
sys.path.insert(0, "src")

from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue, AnyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from aios_trace_eval.app import app
from aios_trace_eval.experiment_runner import _openclaw_command


def kv(key: str, value: str | int) -> KeyValue:
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=value))


class FakeJudgeResponse:
    def __init__(self, content: str = "OK") -> None:
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({
            "model": "judge-model",
            "choices": [{"message": {"content": self.content}}],
        }).encode("utf-8")


def test_runner_resolution() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        executable = root / ("openclaw.exe" if os.name == "nt" else "openclaw")
        config = root / "openclaw.json5"
        state = root / "state"
        executable.write_bytes(b"")
        executable.chmod(0o755)
        config.write_text("{}\n", encoding="utf-8")
        state.mkdir()
        environment = {
            "AIOS_OPENCLAW_COMMAND": str(executable),
            "AIOS_OPENCLAW_CONFIG_PATH": str(config),
            "AIOS_OPENCLAW_STATE_DIR": str(state),
        }
        command, _cwd = _openclaw_command(
            "ping", "selftest-session", "main", 30, environment
        )
        assert command[0] == str(executable.resolve())
        assert command[1:3] == ["agent", "--local"]
        assert environment["OPENCLAW_CONFIG_PATH"] == str(config.resolve())
        assert environment["OPENCLAW_STATE_DIR"] == str(state.resolve())


def main() -> None:
    test_runner_resolution()
    request = ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    rs.resource.attributes.append(kv("service.name", "openclaw-eval"))
    ss = rs.scope_spans.add()
    trace_id = bytes.fromhex("01" * 16)
    root = ss.spans.add(
        trace_id=trace_id,
        span_id=bytes.fromhex("02" * 8),
        name="openclaw.run",
        start_time_unix_nano=1_000_000_000,
        end_time_unix_nano=4_000_000_000,
        status=Status(code=Status.STATUS_CODE_OK),
    )
    root.attributes.extend([kv("openclaw.agent", "main"), kv("aios.case.id", "case-smoke")])
    model = ss.spans.add(
        trace_id=trace_id,
        span_id=bytes.fromhex("03" * 8),
        parent_span_id=bytes.fromhex("02" * 8),
        name="openclaw.model.call",
        start_time_unix_nano=1_200_000_000,
        end_time_unix_nano=2_800_000_000,
        status=Status(code=Status.STATUS_CODE_OK),
    )
    model.attributes.extend([
        kv("openclaw.model", "test-model"),
        kv("openclaw.provider", "test-provider"),
        kv("openclaw.model_call.usage.input_tokens", 800),
        kv("openclaw.model_call.usage.output_tokens", 200),
        kv("openclaw.model_call.usage.total_tokens", 1000),
    ])
    tool = ss.spans.add(
        trace_id=trace_id,
        span_id=bytes.fromhex("04" * 8),
        parent_span_id=bytes.fromhex("02" * 8),
        name="openclaw.tool.execution",
        start_time_unix_nano=2_900_000_000,
        end_time_unix_nano=3_400_000_000,
        status=Status(code=Status.STATUS_CODE_OK),
    )
    tool.attributes.extend([kv("gen_ai.operation.name", "execute_tool"), kv("gen_ai.tool.name", "echo")])

    metric_request = ExportMetricsServiceRequest()
    rm = metric_request.resource_metrics.add()
    rm.resource.attributes.append(kv("service.name", "openclaw-eval"))
    sm = rm.scope_metrics.add()
    for name, unit, value in (
        ("openclaw.memory.rss_bytes", "By", 134_217_728),
        ("openclaw.memory.heap_used_bytes", "By", 67_108_864),
        ("openclaw.liveness.cpu_core_ratio", "1", 0.42),
        ("openclaw.liveness.event_loop_utilization", "1", 0.17),
    ):
        metric = sm.metrics.add(name=name, unit=unit)
        point = metric.histogram.data_points.add(
            start_time_unix_nano=1_000_000_000,
            time_unix_nano=3_000_000_000,
            count=1,
            sum=value,
            min=value,
            max=value,
        )
        if name == "openclaw.memory.rss_bytes":
            for timestamp, outside_value in (
                (500_000_000, 536_870_912),
                (4_500_000_000, 805_306_368),
            ):
                outside = metric.histogram.data_points.add(
                    start_time_unix_nano=timestamp,
                    time_unix_nano=timestamp,
                    count=1,
                    sum=outside_value,
                    min=outside_value,
                    max=outside_value,
                )

    with TestClient(app) as client:
        agent = client.post("/api/agents", json={
            "name": "selftest-agent", "endpoint": "http://127.0.0.1:18789", "description": ""
        })
        assert agent.status_code == 201, agent.text
        dataset = client.post("/api/datasets", json={"name": "selftest-dataset", "description": ""})
        assert dataset.status_code == 201, dataset.text
        case = client.post(f"/api/datasets/{dataset.json()['dataset_id']}/cases", json={
            "name": "basic", "input": "ping", "expected_output": "pong"
        })
        assert case.status_code == 201, case.text
        judge = client.post("/api/judge-models", json={
            "name": "selftest-judge",
            "provider": "openai-compatible",
            "base_url": "https://example.invalid/v1",
            "model": "judge-model",
            "api_key": "selftest-secret-key",
        })
        assert judge.status_code == 201, judge.text
        assert "api_key" not in judge.json()
        assert judge.json()["has_api_key"] is True
        with patch("aios_trace_eval.app.urllib.request.urlopen", return_value=FakeJudgeResponse()):
            connection = client.post(f"/api/judge-models/{judge.json()['judge_model_id']}/test")
        assert connection.status_code == 200, connection.text
        assert connection.json()["ok"] is True
        builtin_evaluators = client.get("/api/evaluators").json()
        assert len(builtin_evaluators) >= 8
        custom_evaluator = client.post("/api/evaluators", json={
            "name": "token gate",
            "description": "selftest",
            "evaluator_type": "rule",
            "dimension": "efficiency",
            "score_min": 0,
            "score_max": 1,
            "pass_threshold": 1,
            "config": {"field": "total_tokens", "operator": "le", "value": 2000},
        })
        assert custom_evaluator.status_code == 201, custom_evaluator.text
        experiment = client.post("/api/experiments", json={
            "name": "selftest-experiment",
            "agent_id": agent.json()["agent_id"],
            "dataset_id": dataset.json()["dataset_id"],
            "judge_model_id": judge.json()["judge_model_id"],
            "evaluator_ids": [custom_evaluator.json()["evaluator_id"]],
        })
        assert experiment.status_code == 201, experiment.text
        assert len(client.get("/api/agents").json()) == 1
        assert len(client.get("/api/datasets").json()) == 1
        judges = client.get("/api/judge-models").json()
        assert len(judges) == 1 and judges[0]["has_api_key"] == 1
        assert judges[0]["status"] == "connected"
        assert "api_key" not in judges[0]
        experiments = client.get("/api/experiments").json()
        assert len(experiments) == 1
        assert experiments[0]["judge_model_name"] == "selftest-judge"
        assert experiments[0]["evaluator_count"] == 1
        run_state = client.get(f"/api/experiments/{experiment.json()['experiment_id']}/run")
        assert run_state.status_code == 200, run_state.text
        assert run_state.json()["state"] == "idle"
        response = client.post("/v1/traces", content=request.SerializeToString(), headers={"content-type": "application/x-protobuf"})
        assert response.status_code == 200, response.text
        metric_response = client.post(
            "/v1/metrics",
            content=metric_request.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
        )
        assert metric_response.status_code == 200, metric_response.text
        traces = client.get("/api/traces").json()
        assert len(traces) == 1
        assert traces[0]["total_tokens"] == 1000
        assert traces[0]["tool_calls"] == 1
        detail = client.get(f"/api/traces/{traces[0]['trace_id']}").json()
        assert len(detail["spans"]) == 3
        assert detail["runtime_metrics"]["rss_bytes"]["peak"] == 134_217_728
        assert len(detail["runtime_metrics"]["rss_series"]["points"]) == 1
        assert detail["runtime_metrics"]["rss_series"]["points"][0]["offset_ms"] == 2000
        assert detail["runtime_metrics"]["sample_scope"] == "trace_interval"
        assert detail["runtime_metrics"]["heap_used_bytes"]["peak"] == 67_108_864
        assert detail["runtime_metrics"]["cpu_core_ratio"]["peak"] == 0.42
        assert len(detail["runtime_metrics"]["cpu_series"]["points"]) == 1
        with patch(
            "aios_trace_eval.app.urllib.request.urlopen",
            return_value=FakeJudgeResponse('{"passed":true,"score":1.0,"reason":"matches"}'),
        ):
            judged = client.post(f"/api/traces/{traces[0]['trace_id']}/judge", json={
                "judge_model_id": judge.json()["judge_model_id"],
                "case_id": case.json()["case_id"],
                "actual_output": "pong",
            })
        assert judged.status_code == 200, judged.text
        assert judged.json()["passed"] is True
        linked_trace = client.get("/api/traces").json()[0]
        assert linked_trace["case_name"] == "basic"
        assert linked_trace["dataset_name"] == "selftest-dataset"
        linked_detail = client.get(f"/api/traces/{traces[0]['trace_id']}").json()["trace"]
        assert linked_detail["dataset_id"] == dataset.json()["dataset_id"]
        assert linked_detail["input_text"] == "ping"
        assert linked_detail["output_text"] == "pong"
        verdict = client.post(f"/api/traces/{traces[0]['trace_id']}/verdict", json={"status": "passed", "score": 1, "reason": "selftest"})
        assert verdict.status_code == 200
        overview = client.get("/api/overview").json()
        assert overview["totals"]["tokens_per_success"] == 1000
        assert client.get("/").status_code == 200
    print("PLATFORM SELFTEST PASSED")


if __name__ == "__main__":
    main()
