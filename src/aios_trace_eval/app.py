"""Unified AIOS Trace Eval web application and OTLP receiver."""
from __future__ import annotations

import os
import asyncio
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import experiment_runner
from .otlp import decode_traces
from .storage import Store
from .metrics import catalog
from .otlp_metrics import decode_metrics
from .local_resource import sample_local_process
from .code_eval import UnsafeExpression, evaluate_expression


ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = Path(os.environ.get("AIOS_DB_PATH", ROOT / "data" / "aios_eval.db"))
WEB_DIR = Path(os.environ.get("AIOS_WEB_DIR", ROOT / "web"))
store = Store(DATA_PATH)

app = FastAPI(title="AIOS Trace Eval", version="0.2.0")
_RUN_STATES: dict[str, dict] = {}
_RUN_LOCK = threading.Lock()


class VerdictInput(BaseModel):
    status: str
    score: Optional[float] = None
    reason: str = ""


class AgentInput(BaseModel):
    name: str
    description: str = ""
    endpoint: str
    protocol: str = "openclaw_gateway"
    auth_type: str = "none"


class DatasetInput(BaseModel):
    name: str
    description: str = ""


class CaseInput(BaseModel):
    name: str
    input: str
    expected_output: str = ""
    assertions: list[dict] = []


class JudgeModelInput(BaseModel):
    name: str
    provider: str
    base_url: str
    model: str
    api_key: str


class JudgeRunInput(BaseModel):
    judge_model_id: str
    case_id: str
    actual_output: str


class EvaluatorInput(BaseModel):
    name: str
    description: str = ""
    evaluator_type: str
    dimension: str
    score_min: float = 0
    score_max: float = 1
    pass_threshold: float = 0.8
    judge_model_id: Optional[str] = None
    prompt_template: str = ""
    config: dict = {}


class ExperimentInput(BaseModel):
    name: str
    agent_id: str
    dataset_id: str
    judge_model_id: Optional[str] = None
    evaluator_ids: list[str] = []


class EvaluationRunInput(BaseModel):
    case_id: str
    actual_output: str
    evaluator_ids: list[str] = []
    experiment_id: Optional[str] = None


class ExperimentStatusInput(BaseModel):
    status: str


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "storage": "sqlite", "otlp": True}


@app.get("/api/health/otlp")
def otlp_health() -> dict:
    health = store.otlp_health()
    latest_ns = max(
        value for value in (
            health.get("last_trace_at_ns"), health.get("last_metric_at_ns")
        ) if value is not None
    ) if any(value is not None for value in health.values()) else None
    recent_window_seconds = 60
    receiving_recently = bool(
        latest_ns and time.time_ns() - int(latest_ns) <= recent_window_seconds * 1_000_000_000
    )
    return {
        "status": "ok",
        "receiver": "ready",
        "storage": "ok",
        "traces_endpoint": "/v1/traces",
        "metrics_endpoint": "/v1/metrics",
        "last_trace_at_ns": health.get("last_trace_at_ns"),
        "last_metric_at_ns": health.get("last_metric_at_ns"),
        "receiving_recently": receiving_recently,
        "recent_window_seconds": recent_window_seconds,
        "checked_at_ns": time.time_ns(),
    }


def _send_otlp_probe(url: str, payload: bytes) -> int:
    probe_request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-protobuf"},
    )
    with urllib.request.urlopen(probe_request, timeout=8) as response:
        response.read()
        return int(response.status)


@app.post("/api/health/otlp/test")
async def test_otlp_end_to_end(request: Request) -> dict:
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.trace.v1.trace_pb2 import Span
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="OTLP protobuf 依赖不可用") from exc

    trace_id_bytes = secrets.token_bytes(16)
    span_id_bytes = secrets.token_bytes(8)
    trace_id = trace_id_bytes.hex()
    started_at_ns = time.time_ns()
    export = ExportTraceServiceRequest()
    resource_spans = export.resource_spans.add()
    service_name = resource_spans.resource.attributes.add()
    service_name.key = "service.name"
    service_name.value.string_value = "aios-otlp-self-test"
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "aios.otlp.self-test"
    span = scope_spans.spans.add()
    span.trace_id = trace_id_bytes
    span.span_id = span_id_bytes
    span.name = "aios.otlp.self_test"
    span.kind = Span.SPAN_KIND_INTERNAL
    span.start_time_unix_nano = started_at_ns
    span.end_time_unix_nano = started_at_ns + 1_000_000
    span.status.code = 1
    marker = span.attributes.add()
    marker.key = "aios.self_test"
    marker.value.bool_value = True

    server_host, server_port = request.scope.get("server") or ("127.0.0.1", 8080)
    if server_host in {"0.0.0.0", "::", "localhost"}:
        server_host = "127.0.0.1"
    if ":" in str(server_host):
        server_host = f"[{server_host}]"
    receiver_url = f"http://{server_host}:{server_port}/v1/traces"
    test_started = time.perf_counter()
    inserted = False
    try:
        response_status = await asyncio.to_thread(
            _send_otlp_probe, receiver_url, export.SerializeToString()
        )
        if response_status != 200:
            raise RuntimeError(f"OTLP 接收端返回 HTTP {response_status}")
        stored = store.get_trace(trace_id)
        inserted = stored is not None
        if not inserted:
            raise RuntimeError("OTLP 请求成功，但测试 Trace 未写入数据库")
        spans = stored.get("spans") or []
        if not any(item.get("span_id") == span_id_bytes.hex() for item in spans):
            raise RuntimeError("测试 Trace 已写入，但 Span 回查失败")
        return {
            "status": "passed",
            "receiver_http": "passed",
            "protobuf_decode": "passed",
            "storage_write": "passed",
            "trace_readback": "passed",
            "latency_ms": round((time.perf_counter() - test_started) * 1000, 1),
            "tested_at_ns": time.time_ns(),
        }
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"OTLP 端到端检测失败：{exc}") from exc
    finally:
        if inserted:
            store.delete_trace(trace_id)


@app.get("/api/overview")
def overview() -> dict:
    return store.overview()


@app.get("/api/metrics/catalog")
def metrics_catalog() -> dict:
    return catalog()


@app.get("/api/traces")
def traces(limit: int = Query(100, ge=1, le=500), status: Optional[str] = None, q: str = "") -> list:
    return store.list_traces(limit=limit, status=status, query=q)


@app.get("/api/traces/{trace_id}")
def trace(trace_id: str) -> dict:
    result = store.get_trace(trace_id)
    if result is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return result


@app.post("/api/traces/{trace_id}/verdict")
def verdict(trace_id: str, body: VerdictInput) -> dict:
    try:
        changed = store.set_verdict(trace_id, body.status, body.score, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="trace not found")
    return {"updated": True}


@app.get("/api/experiments")
def experiments() -> list:
    return store.list_eval_experiments()


@app.get("/api/experiments/{experiment_id}/compare")
def compare(experiment_id: str) -> dict:
    result = store.compare(experiment_id)
    if not result:
        raise HTTPException(status_code=404, detail="experiment not found")
    return result


@app.get("/api/eval-experiments/{experiment_id}")
def eval_experiment_detail(experiment_id: str) -> dict:
    result = store.get_eval_experiment(experiment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return result


@app.post("/api/experiments/{experiment_id}/status")
def update_experiment_status(experiment_id: str, body: ExperimentStatusInput) -> dict:
    if body.status not in {"draft", "running", "completed", "failed"}:
        raise HTTPException(status_code=400, detail="invalid experiment status")
    if store.get_eval_experiment(experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    store.set_experiment_status(experiment_id, body.status)
    return {"experiment_id": experiment_id, "status": body.status}


def _experiment_worker(experiment_id: str, detail: dict, base_url: str,
                       gateway_port: str, openclaw_agent: str) -> None:
    def progress(state: str, completed: int, total: int, current: str,
                 error: str | None) -> None:
        with _RUN_LOCK:
            _RUN_STATES[experiment_id] = {
                "state": state, "total": total, "completed": completed,
                "current_case": current, "error": error,
            }
    try:
        experiment_runner.run_experiment(
            base_url=base_url, experiment=detail["experiment"], cases=detail["cases"],
            gateway_port=gateway_port, agent=openclaw_agent, on_progress=progress,
        )
        store.set_experiment_status(experiment_id, "completed")
        progress("completed", len(detail["cases"]), len(detail["cases"]), "", None)
    except Exception as exc:
        store.set_experiment_status(experiment_id, "failed")
        with _RUN_LOCK:
            previous = _RUN_STATES.get(experiment_id, {})
            _RUN_STATES[experiment_id] = {
                **previous, "state": "failed", "error": str(exc)[:800],
            }


@app.post("/api/experiments/{experiment_id}/run", status_code=202)
def start_experiment_run(experiment_id: str, request: Request) -> dict:
    detail = store.get_eval_experiment(experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if not detail["cases"]:
        raise HTTPException(status_code=400, detail="数据集还没有 Case，无法运行")
    with _RUN_LOCK:
        current = _RUN_STATES.get(experiment_id)
        if current and current.get("state") == "running":
            raise HTTPException(status_code=409, detail="实验正在运行中")
        _RUN_STATES[experiment_id] = {
            "state": "running", "total": len(detail["cases"]), "completed": 0,
            "current_case": "", "error": None,
        }
    store.set_experiment_status(experiment_id, "running")
    base_url = os.environ.get("AIOS_BASE_URL") or str(request.base_url).rstrip("/")
    gateway_port = experiment_runner.gateway_port_from_endpoint(
        detail["experiment"].get("agent_endpoint") or "",
        os.environ.get("OPENCLAW_GATEWAY_PORT", "18790"),
    )
    threading.Thread(
        target=_experiment_worker,
        args=(experiment_id, detail, base_url, gateway_port,
              os.environ.get("OPENCLAW_AGENT", "main")),
        daemon=True,
    ).start()
    return {"experiment_id": experiment_id, "state": "running",
            "total": len(detail["cases"]), "gateway_port": gateway_port}


@app.get("/api/experiments/{experiment_id}/run")
def experiment_run_status(experiment_id: str) -> dict:
    if store.get_eval_experiment(experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    with _RUN_LOCK:
        state = dict(_RUN_STATES[experiment_id]) if experiment_id in _RUN_STATES else None
    return {"experiment_id": experiment_id, **(state or {
        "state": "idle", "total": 0, "completed": 0,
        "current_case": "", "error": None,
    })}


@app.get("/api/agents")
def agents() -> list:
    return store.list_agents()


@app.post("/api/agents", status_code=201)
def create_agent(body: AgentInput) -> dict:
    values = body.model_dump()
    values["agent_id"] = f"agent_{uuid.uuid4().hex[:12]}"
    return store.create_agent(values)


@app.get("/api/judge-models")
def judge_models() -> list:
    return store.list_judge_models()


@app.post("/api/judge-models", status_code=201)
def create_judge_model(body: JudgeModelInput) -> dict:
    values = body.model_dump()
    values.update({
        "judge_model_id": f"judge_{uuid.uuid4().hex[:12]}",
        "status": "configured",
    })
    return store.create_judge_model(values)


@app.post("/api/judge-models/{judge_model_id}/test")
def test_judge_model(judge_model_id: str) -> dict:
    judge = store.get_judge_model_secret(judge_model_id)
    if judge is None:
        raise HTTPException(status_code=404, detail="judge model not found")

    endpoint = judge["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": judge["model"],
        "messages": [{"role": "user", "content": "只回复 OK"}],
        "temperature": 0,
        "max_tokens": 8,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {judge['api_key']}",
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        reply = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if not reply:
            raise ValueError("模型返回成功，但响应中没有 assistant 内容")
        store.record_judge_model_test(judge_model_id, "connected", latency_ms)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "model": result.get("model") or judge["model"],
            "reply": reply[:80],
        }
    except urllib.error.HTTPError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            message = parsed.get("error", {}).get("message") or raw
        except json.JSONDecodeError:
            message = raw
        error = f"HTTP {exc.code}: {str(message)[:300]}"
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        error = str(exc)[:300]

    store.record_judge_model_test(judge_model_id, "error", latency_ms, error)
    raise HTTPException(status_code=502, detail=error)


@app.get("/api/evaluators")
def evaluators() -> list:
    return store.list_evaluators()


def _validate_evaluator(body: EvaluatorInput) -> None:
    if body.evaluator_type not in {"rule", "trajectory_rule", "llm", "code", "human"}:
        raise HTTPException(status_code=400, detail="unsupported evaluator type")
    if body.dimension not in {"result", "process", "efficiency", "risk"}:
        raise HTTPException(status_code=400, detail="unsupported evaluator dimension")
    if body.score_max <= body.score_min:
        raise HTTPException(status_code=400, detail="score_max must be greater than score_min")
    if not body.score_min <= body.pass_threshold <= body.score_max:
        raise HTTPException(status_code=400, detail="pass threshold must be within score range")
    if body.evaluator_type == "code":
        try:
            evaluate_expression(str(body.config.get("expression") or ""), {
                "query": "", "response": "", "reference": "", "trace": {}, "trajectory": []
            })
        except (UnsafeExpression, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"无效的 Code 表达式：{exc}") from exc


def _evaluator_values(body: EvaluatorInput) -> dict:
    values = body.model_dump(exclude={"config"})
    values.update({
        "judge_model_id": body.judge_model_id or None,
        "config_json": json.dumps(body.config, ensure_ascii=False),
    })
    return values


@app.post("/api/evaluators", status_code=201)
def create_evaluator(body: EvaluatorInput) -> dict:
    _validate_evaluator(body)
    values = _evaluator_values(body)
    values.update({
        "evaluator_id": f"evaluator_{uuid.uuid4().hex[:12]}",
        "version": 1,
        "status": "active",
        "is_builtin": 0,
    })
    try:
        return store.create_evaluator(values)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/evaluators/{evaluator_id}")
def update_evaluator(evaluator_id: str, body: EvaluatorInput) -> dict:
    _validate_evaluator(body)
    try:
        result = store.update_evaluator(evaluator_id, _evaluator_values(body))
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="evaluator not found")
    return result


@app.post("/api/traces/{trace_id}/judge")
def judge_trace(trace_id: str, body: JudgeRunInput) -> dict:
    judge = store.get_judge_model_secret(body.judge_model_id)
    case = store.get_case(body.case_id)
    if judge is None:
        raise HTTPException(status_code=404, detail="judge model not found")
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    trace_detail = store.get_trace(trace_id)
    if trace_detail is None:
        raise HTTPException(status_code=404, detail="trace not found")

    prompt = (
        "你是严格的 Agent 评测裁判。根据任务、期望输出和实际输出评分。"
        "只输出一行 JSON，格式为："
        '{"passed":true,"score":1.0,"reason":"简短理由"}。'
        "score 范围为 0 到 1。\n\n"
        f"任务：{case['input']}\n"
        f"期望输出：{case['expected_output']}\n"
        f"实际输出：{body.actual_output}"
    )
    endpoint = judge["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": judge["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {judge['api_key']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            provider_result = json.loads(response.read().decode("utf-8"))
        reply = provider_result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        start, end = reply.find("{"), reply.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Judge 未返回合法 JSON")
        verdict = json.loads(reply[start:end + 1])
        llm_passed = bool(verdict.get("passed"))
        llm_score = min(1.0, max(0.0, float(verdict.get("score", 0))))
        llm_reason = str(verdict.get("reason") or "Judge 未提供理由")[:1000]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Judge HTTP {exc.code}: {raw[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Judge 调用失败: {str(exc)[:300]}") from exc

    rule_failures: list[str] = []
    rule_results: list[dict] = []
    trace_row = trace_detail["trace"]
    operators = {
        "lt": lambda actual, expected: actual < expected,
        "le": lambda actual, expected: actual <= expected,
        "gt": lambda actual, expected: actual > expected,
        "ge": lambda actual, expected: actual >= expected,
        "eq": lambda actual, expected: actual == expected,
    }
    for assertion in case.get("assertions", []):
        assertion_type = assertion.get("type")
        ok = True
        label = assertion_type or "unknown"
        if assertion_type == "output_exact":
            expected = str(assertion.get("value", "")).strip()
            actual = body.actual_output.strip()
            ok = actual == expected
            label = f"output_exact: {actual!r} == {expected!r}"
        elif assertion_type == "trace_rule":
            field = str(assertion.get("field", ""))
            operator = str(assertion.get("operator", ""))
            actual = trace_row.get(field)
            expected = assertion.get("value")
            ok = actual is not None and operator in operators and operators[operator](actual, expected)
            label = f"{field}: {actual} {operator} {expected}"
        elif assertion_type == "json_schema":
            try:
                parsed_output = json.loads(body.actual_output)
                required = assertion.get("required", [])
                ok = isinstance(parsed_output, dict) and all(key in parsed_output for key in required)
            except json.JSONDecodeError:
                ok = False
            label = "json_schema required fields"
        rule_results.append({"assertion": assertion, "passed": ok})
        if not ok:
            rule_failures.append(label)

    passed = llm_passed and not rule_failures
    score = llm_score if passed else 0.0
    reason = llm_reason
    if rule_failures:
        reason += "；确定性门禁失败：" + "；".join(rule_failures)

    changed = store.set_llm_verdict(
        trace_id=trace_id,
        case_id=body.case_id,
        evaluator=judge["name"],
        passed=passed,
        score=score,
        reason=reason,
        evidence={
            "judge_model": provider_result.get("model") or judge["model"],
            "expected_output": case["expected_output"],
            "actual_output": body.actual_output,
            "llm_passed": llm_passed,
            "llm_score": llm_score,
            "rule_results": rule_results,
        },
        input_text=case["input"],
        output_text=body.actual_output,
    )
    if not changed:
        raise HTTPException(status_code=404, detail="trace not found")
    return {
        "passed": passed,
        "score": score,
        "reason": reason,
        "llm_passed": llm_passed,
        "llm_score": llm_score,
        "rule_failures": rule_failures,
    }


def _compare(actual: object, operator: str, expected: object) -> bool:
    operations = {
        "lt": lambda: actual < expected, "le": lambda: actual <= expected,
        "gt": lambda: actual > expected, "ge": lambda: actual >= expected,
        "eq": lambda: actual == expected, "ne": lambda: actual != expected,
    }
    if operator not in operations:
        raise ValueError(f"不支持的运算符：{operator}")
    return bool(operations[operator]())


def _run_llm_evaluator(evaluator: dict, case: dict, actual_output: str, trace_row: dict,
                       default_judge_id: str | None = None) -> tuple[bool, float, str, dict]:
    judge_id = evaluator.get("judge_model_id") or default_judge_id
    if not judge_id:
        raise ValueError("LLM 评估器未绑定 Judge 模型")
    judge = store.get_judge_model_secret(judge_id)
    if not judge:
        raise ValueError("Judge 模型不存在")
    template = evaluator.get("prompt_template") or (
        "你是严格的 Agent 评测器。根据任务、参考答案和实际输出评分，只输出 JSON："
        '{"score":0到1之间的数字,"reason":"简短理由"}。\n任务：{{query}}\n'
        "参考答案：{{reference}}\n实际输出：{{response}}"
    )
    prompt = (template.replace("{{query}}", case["input"])
              .replace("{{reference}}", case["expected_output"])
              .replace("{{response}}", actual_output)
              .replace("{{trajectory}}", json.dumps(trace_row, ensure_ascii=False)))
    request = urllib.request.Request(
        judge["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps({"model": judge["model"], "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0, "max_tokens": 256, "stream": False}).encode("utf-8"),
        method="POST", headers={"Authorization": f"Bearer {judge['api_key']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        provider_result = json.loads(response.read().decode("utf-8"))
    reply = provider_result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Judge 未返回合法 JSON")
    verdict = json.loads(reply[start:end + 1])
    raw_score = float(verdict.get("score", 0))
    score = min(float(evaluator["score_max"]), max(float(evaluator["score_min"]), raw_score))
    return score >= float(evaluator["pass_threshold"]), score, str(verdict.get("reason") or "Judge 未提供理由"), {
        "judge_model": provider_result.get("model") or judge["model"]
    }


@app.post("/api/traces/{trace_id}/evaluate")
def evaluate_trace(trace_id: str, body: EvaluationRunInput) -> dict:
    detail = store.get_trace(trace_id)
    case = store.get_case(body.case_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="trace not found")
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    experiment = store.get_eval_experiment(body.experiment_id) if body.experiment_id else None
    default_judge_id = experiment["experiment"].get("judge_model_id") if experiment else None
    evaluator_ids = body.evaluator_ids or ([item["evaluator_id"] for item in experiment["evaluators"]] if experiment else [])
    if not evaluator_ids:
        raise HTTPException(status_code=400, detail="至少选择一个评估器")
    trace_row = detail["trace"]
    results: list[dict] = []
    for evaluator_id in evaluator_ids:
        evaluator = store.get_evaluator(evaluator_id)
        if evaluator is None:
            raise HTTPException(status_code=404, detail=f"evaluator not found: {evaluator_id}")
        kind, config = evaluator["evaluator_type"], evaluator["config"]
        try:
            evidence: dict = {}
            if kind in {"rule", "trajectory_rule"}:
                if config.get("operator") == "output_exact":
                    ok = body.actual_output.strip() == case["expected_output"].strip()
                    reason = "实际输出与参考答案一致" if ok else "实际输出与参考答案不一致"
                else:
                    field, operator, expected = config.get("field"), config.get("operator"), config.get("value")
                    actual = trace_row.get(field)
                    ok = actual is not None and _compare(actual, operator, expected)
                    reason = f"{field}={actual}，要求 {operator} {expected}"
                    evidence = {"field": field, "actual": actual, "operator": operator, "expected": expected}
                score = float(evaluator["score_max"] if ok else evaluator["score_min"])
            elif kind == "code":
                expression = str(config.get("expression") or "")
                value = evaluate_expression(expression, {
                    "query": case["input"], "response": body.actual_output,
                    "reference": case["expected_output"], "trace": trace_row,
                    "trajectory": detail["spans"],
                })
                score = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float(evaluator["score_max"] if value else evaluator["score_min"])
                ok = score >= float(evaluator["pass_threshold"])
                reason = f"受限表达式结果：{value!r}"
                evidence = {"expression": expression, "value": value}
            elif kind == "llm":
                ok, score, reason, evidence = _run_llm_evaluator(
                    evaluator, case, body.actual_output, trace_row, default_judge_id,
                )
            elif kind == "human":
                continue
            else:
                raise ValueError(f"不支持的评估器类型：{kind}")
        except (ValueError, TypeError, KeyError, UnsafeExpression, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"{evaluator['name']} 执行失败：{str(exc)[:300]}") from exc
        results.append({"evaluator": evaluator["name"], "evaluator_type": kind, "passed": ok,
                        "score": score, "reason": reason, "evidence": evidence})
    if not results:
        raise HTTPException(status_code=400, detail="没有可自动执行的评估器")
    overall_score = round(sum(item["score"] for item in results) / len(results), 4)
    passed = all(item["passed"] for item in results)
    agent_name = experiment["experiment"]["agent_name"] if experiment else None
    store.evaluate_trace(
        trace_id, body.case_id, results, passed, overall_score,
        body.experiment_id, agent_name, case["input"], body.actual_output,
    )
    return {"trace_id": trace_id, "case_id": body.case_id, "passed": passed,
            "score": overall_score, "results": results}


@app.get("/api/datasets")
def datasets() -> list:
    return store.list_datasets()


@app.post("/api/datasets", status_code=201)
def create_dataset(body: DatasetInput) -> dict:
    values = body.model_dump()
    values["dataset_id"] = f"dataset_{uuid.uuid4().hex[:12]}"
    return store.create_dataset(values)


@app.get("/api/datasets/{dataset_id}")
def dataset_detail(dataset_id: str) -> dict:
    result = store.get_dataset(dataset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    return result


@app.post("/api/datasets/{dataset_id}/cases", status_code=201)
def create_case(dataset_id: str, body: CaseInput) -> dict:
    values = body.model_dump(exclude={"assertions"})
    values.update({
        "case_id": f"case_{uuid.uuid4().hex[:12]}",
        "dataset_id": dataset_id,
        "assertions_json": json.dumps(body.assertions, ensure_ascii=False),
    })
    try:
        return store.add_case(values)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/experiments", status_code=201)
def create_experiment(body: ExperimentInput) -> dict:
    values = body.model_dump()
    values["judge_model_id"] = values.get("judge_model_id") or None
    values.update({"experiment_id": f"exp_{uuid.uuid4().hex[:12]}", "status": "draft"})
    try:
        return store.create_eval_experiment(values)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/traces")
async def receive_traces(request: Request) -> Response:
    payload = await request.body()
    try:
        traces_data, spans_data, process_samples = decode_traces(
            payload, request.headers.get("content-type", "application/x-protobuf")
        )
        for item in traces_data:
            store.upsert_trace(item)
        store.upsert_spans(spans_data)
        store.upsert_metric_samples(process_samples)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid OTLP trace payload: {exc}") from exc
    return Response(content=b"", media_type="application/x-protobuf", status_code=200)


@app.post("/v1/metrics")
async def receive_metrics(request: Request) -> Response:
    payload = await request.body()
    try:
        samples = decode_metrics(payload, request.headers.get("content-type", "application/x-protobuf"))
        store.upsert_metric_samples(samples)
        if samples and request.client:
            store.upsert_metric_samples(sample_local_process(
                request.client.host,
                request.client.port,
                samples[0]["service_name"],
            ))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid OTLP metric payload: {exc}") from exc
    return Response(content=b"", media_type="application/x-protobuf", status_code=200)


@app.post("/v1/logs")
async def receive_logs(_: Request) -> Response:
    return Response(content=b"", media_type="application/x-protobuf", status_code=200)


if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str = "") -> FileResponse:
        requested = WEB_DIR / path
        if path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(WEB_DIR / "index.html")
