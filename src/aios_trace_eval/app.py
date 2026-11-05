"""Unified AIOS Trace Eval web application and OTLP receiver."""
from __future__ import annotations

import os
import json
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

from .otlp import decode_traces
from .storage import Store
from .metrics import catalog
from .code_eval import UnsafeExpression, evaluate_expression


ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = Path(os.environ.get("AIOS_DB_PATH", ROOT / "data" / "aios_eval.db"))
WEB_DIR = Path(os.environ.get("AIOS_WEB_DIR", ROOT / "web"))
store = Store(DATA_PATH)

app = FastAPI(title="AIOS Trace Eval", version="0.2.0")


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


@app.post("/api/evaluators", status_code=201)
def create_evaluator(body: EvaluatorInput) -> dict:
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
    values = body.model_dump(exclude={"config"})
    values.update({
        "evaluator_id": f"evaluator_{uuid.uuid4().hex[:12]}",
        "judge_model_id": body.judge_model_id or None,
        "config_json": json.dumps(body.config, ensure_ascii=False),
        "version": 1,
        "status": "active",
        "is_builtin": 0,
    })
    try:
        return store.create_evaluator(values)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


def _run_llm_evaluator(evaluator: dict, case: dict, actual_output: str, trace_row: dict) -> tuple[bool, float, str, dict]:
    judge_id = evaluator.get("judge_model_id")
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
                ok, score, reason, evidence = _run_llm_evaluator(evaluator, case, body.actual_output, trace_row)
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
    store.evaluate_trace(trace_id, body.case_id, results, passed, overall_score, body.experiment_id, agent_name)
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
        traces_data, spans_data = decode_traces(payload, request.headers.get("content-type", "application/x-protobuf"))
        for item in traces_data:
            store.upsert_trace(item)
        store.upsert_spans(spans_data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid OTLP trace payload: {exc}") from exc
    return Response(content=b"", media_type="application/x-protobuf", status_code=200)


@app.post("/v1/metrics")
async def receive_metrics(_: Request) -> Response:
    # Run-level metrics are derived from trace attributes to avoid double counting.
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
