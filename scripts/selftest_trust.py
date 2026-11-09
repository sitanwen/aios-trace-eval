"""Regression tests for trustworthy ingestion, correlation and evaluation."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "src")
_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["AIOS_DB_PATH"] = str(Path(_TEMP_DIR.name) / "trust.db")

from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from aios_trace_eval.app import _run_llm_evaluator, app, store


def kv(key: str, value: str | int) -> KeyValue:
    encoded = AnyValue(int_value=value) if isinstance(value, int) else AnyValue(string_value=value)
    return KeyValue(key=key, value=encoded)


def trace_payload(
    trace_id: str, span_id: str, start_ns: int, end_ns: int,
    tokens: int, resource: dict[str, str],
) -> bytes:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.extend(kv(key, value) for key, value in resource.items())
    scope_spans = resource_spans.scope_spans.add()
    span = scope_spans.spans.add(
        trace_id=bytes.fromhex(trace_id), span_id=bytes.fromhex(span_id),
        name="openclaw.model.call", start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
    )
    span.attributes.extend([
        kv("openclaw.model_call.usage.total_tokens", tokens),
        kv("openclaw.model", "selftest-model"),
    ])
    return request.SerializeToString()


class JudgeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({
            "model": "judge-model",
            "choices": [{"message": {"content": json.dumps({
                "score": 1, "reason": "ok", "evidence_span_ids": ["span-1"],
            })}}],
        }).encode("utf-8")


def main() -> None:
    with TestClient(app) as client:
        agent = client.post("/api/agents", json={
            "name": "agent", "endpoint": "local", "description": "",
        }).json()
        dataset = client.post("/api/datasets", json={"name": "dataset", "description": ""}).json()
        cases = []
        for name, assertions in (
            ("batched", []),
            ("assertion", [{"type": "output_exact", "value": "yes"}]),
            ("runtime-error", []),
        ):
            cases.append(client.post(f"/api/datasets/{dataset['dataset_id']}/cases", json={
                "name": name, "input": name, "expected_output": "yes", "assertions": assertions,
            }).json())
        experiment = client.post("/api/experiments", json={
            "name": "trust", "agent_id": agent["agent_id"],
            "dataset_id": dataset["dataset_id"],
            "evaluator_ids": ["builtin_token_budget"],
        }).json()
        experiment_id = experiment["experiment_id"]

        trace_one = "31" * 16
        response = client.post("/v1/traces", content=trace_payload(
            trace_one, "01" * 8, 1_000_000_000, 2_000_000_000, 100,
            {
                "service.name": "trust-test", "aios.run.id": "run-batched",
                "aios.case.id": cases[0]["case_id"],
                "aios.experiment.id": experiment_id,
            },
        ), headers={"content-type": "application/x-protobuf"})
        assert response.status_code == 200, response.text
        evaluated = client.post(f"/api/traces/{trace_one}/evaluate", json={
            "case_id": cases[0]["case_id"], "actual_output": "yes",
            "experiment_id": experiment_id,
        })
        assert evaluated.status_code == 200, evaluated.text
        response = client.post("/v1/traces", content=trace_payload(
            trace_one, "02" * 8, 2_000_000_000, 3_000_000_000, 200,
            {"service.name": "trust-test"},
        ), headers={"content-type": "application/x-protobuf"})
        assert response.status_code == 200, response.text
        trace = client.get(f"/api/traces/{trace_one}").json()["trace"]
        assert (trace["total_tokens"], trace["duration_ms"], trace["model_calls"]) == (300, 2000, 2)
        assert trace["case_id"] == cases[0]["case_id"]
        assert trace["experiment_id"] == experiment_id
        assert trace["business_status"] == "passed"

        trace_two = "32" * 16
        response = client.post("/v1/traces", content=trace_payload(
            trace_two, "03" * 8, 4_000_000_000, 5_000_000_000, 10,
            {
                "service.name": "trust-test", "aios.run.id": "run-assertion",
                "aios.case.id": cases[1]["case_id"],
                "aios.experiment.id": experiment_id,
            },
        ), headers={"content-type": "application/x-protobuf"})
        assert response.status_code == 200, response.text
        assertion_result = client.post(f"/api/traces/{trace_two}/evaluate", json={
            "case_id": cases[1]["case_id"], "actual_output": "NO",
            "experiment_id": experiment_id,
        }).json()
        assert assertion_result["passed"] is False
        assert any(
            result["evaluator_type"] == "case_assertion" and not result["passed"]
            for result in assertion_result["results"]
        )

        attempts = (
            (cases[0], "run-batched", "passed", trace_one, 1),
            (cases[1], "run-assertion", "failed", trace_two, 0),
            (cases[2], "run-error", "runtime_error", None, None),
        )
        for case, run_id, status, trace_id, score in attempts:
            created = client.post(f"/api/experiments/{experiment_id}/attempts", json={
                "case_id": case["case_id"], "run_id": run_id,
            })
            assert created.status_code == 201, created.text
            updated = client.patch(f"/api/attempts/{run_id}", json={
                "status": status, "trace_id": trace_id, "score": score,
                "error_type": "CLIError" if status == "runtime_error" else None,
                "error_message": "boom" if status == "runtime_error" else None,
            })
            assert updated.status_code == 200, updated.text
        detail = client.get(f"/api/eval-experiments/{experiment_id}").json()
        assert detail["summary"]["total"] == 3
        assert detail["summary"]["passed"] == 1
        assert detail["summary"]["pass_rate"] == 0.3333
        assert detail["summary"]["errors"] == 1
        assert len(detail["runs"]) == 3

    store.create_judge_model({
        "judge_model_id": "judge", "name": "judge", "provider": "test",
        "base_url": "https://example.invalid", "model": "judge-model",
        "api_key": "secret", "status": "configured",
    })
    prompts: list[str] = []

    def fake_urlopen(request, timeout=0):
        prompts.append(json.loads(request.data)["messages"][0]["content"])
        return JudgeResponse()

    trajectory = [{
        "span_id": "span-1", "parent_span_id": "", "name": "tool",
        "status": "ok", "duration_ms": 1, "attributes": {"tool.args": "x"},
    }]
    with patch("aios_trace_eval.app.urllib.request.urlopen", side_effect=fake_urlopen):
        for evaluator_id in (
            "builtin_task_completion", "builtin_instruction_following", "builtin_safety",
        ):
            evaluator = store.get_evaluator(evaluator_id)
            _run_llm_evaluator(
                evaluator, {"input": "do x", "expected_output": "x"},
                "x", {}, trajectory, "judge",
            )
    assert len(set(prompts)) == 3
    assert all("span-1" in prompt and "tool.args" in prompt for prompt in prompts)
    print("TRUST REGRESSION SELFTEST PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        _TEMP_DIR.cleanup()
