"""Create and run a five-case GSM8K experiment through the real OpenClaw Gateway."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8080"
SOURCE = ROOT / "work" / "datasets" / "gsm8k-test.jsonl"
SOURCE_URL = "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
SOURCE_SHA256 = "3730d312f6e3440559ace48831e51066acaca737f6eabec99bccb9e4b3c39d14"
CONFIG = ROOT / "work" / "openclaw-8.2" / "openclaw-deepseek.json5"
STATE = ROOT / "work" / "openclaw-8.2" / "state"


def request(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def find_or_create(items_path: str, name: str, create_path: str, payload: dict):
    for item in request(items_path):
        if item["name"] == name:
            return item
    return request(create_path, "POST", payload)


def load_cases(limit: int) -> list[dict]:
    cases = []
    with SOURCE.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= limit:
                break
            item = json.loads(line)
            reference = item["answer"].rsplit("####", 1)[-1].strip().replace(",", "")
            cases.append({"name": f"GSM8K test #{index + 1}", "input": item["question"],
                          "expected_output": reference, "assertions": []})
    return cases


def ensure_source() -> None:
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
            SOURCE.write_bytes(response.read())
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        raise RuntimeError(f"GSM8K 文件校验失败：{digest}")


def setup(limit: int) -> tuple[dict, list[dict]]:
    ensure_source()
    dataset_name = f"GSM8K 官方测试集 · {limit}题"
    dataset = find_or_create("/api/datasets", dataset_name, "/api/datasets", {
        "name": dataset_name,
        "description": "OpenAI GSM8K 官方 test.jsonl 的前 5 条固定子集；MIT License；仅用于本地真实回归。",
    })
    detail = request(f"/api/datasets/{dataset['dataset_id']}")
    if not detail["cases"]:
        for case in load_cases(limit):
            request(f"/api/datasets/{dataset['dataset_id']}/cases", "POST", case)
        detail = request(f"/api/datasets/{dataset['dataset_id']}")

    correctness = find_or_create("/api/evaluators", "数值答案一致性", "/api/evaluators", {
        "name": "数值答案一致性", "description": "提取输出与参考答案中的最后一个数字后进行确定性比对。",
        "evaluator_type": "code", "dimension": "result", "score_min": 0, "score_max": 1,
        "pass_threshold": 1, "config": {"expression": "normalize_number(response) == normalize_number(reference)"},
    })
    token = find_or_create("/api/evaluators", "OpenClaw 40K Token 预算", "/api/evaluators", {
        "name": "OpenClaw 40K Token 预算", "description": "包含 OpenClaw 系统提示词的单样本总 Token 上限。",
        "evaluator_type": "rule", "dimension": "efficiency", "score_min": 0, "score_max": 1,
        "pass_threshold": 1, "config": {"field": "total_tokens", "operator": "le", "value": 40000},
    })
    latency = find_or_create("/api/evaluators", "OpenClaw 30秒时延预算", "/api/evaluators", {
        "name": "OpenClaw 30秒时延预算", "description": "端到端 Trace 耗时不得超过 30 秒。",
        "evaluator_type": "rule", "dimension": "efficiency", "score_min": 0, "score_max": 1,
        "pass_threshold": 1, "config": {"field": "duration_ms", "operator": "le", "value": 30000},
    })
    agents = request("/api/agents")
    agent = next((item for item in agents if item["name"] == "本地 OpenClaw"), None)
    if not agent:
        agent = request("/api/agents", "POST", {"name": "本地 OpenClaw", "description": "OpenClaw 2026.8.2 + DeepSeek",
                                                  "endpoint": "ws://127.0.0.1:18790", "protocol": "openclaw_gateway"})
    exp_name = f"GSM8K {limit}题 · OpenClaw 8.2 实跑"
    experiments = request("/api/experiments")
    experiment = next((item for item in experiments if item["name"] == exp_name), None)
    if not experiment:
        experiment = request("/api/experiments", "POST", {
            "name": exp_name, "agent_id": agent["agent_id"], "dataset_id": dataset["dataset_id"],
            "evaluator_ids": [correctness["evaluator_id"], token["evaluator_id"], latency["evaluator_id"], "builtin_tool_success"],
        })
    return experiment, detail["cases"]


def run_case(experiment: dict, case: dict, index: int) -> dict:
    before = {item["trace_id"] for item in request("/api/traces?limit=100")}
    prompt = case["input"] + "\n\nReturn only the final numeric answer."
    env = os.environ.copy()
    gateway_token = env.get("OPENCLAW_GATEWAY_TOKEN")
    if not gateway_token:
        raise RuntimeError("请先设置 OPENCLAW_GATEWAY_TOKEN")
    env.update({"OPENCLAW_STATE_DIR": str(STATE), "OPENCLAW_CONFIG_PATH": str(CONFIG),
                "OPENCLAW_GATEWAY_PORT": env.get("OPENCLAW_GATEWAY_PORT", "18790")})
    session_id = f"gsm8k-{index + 1}-{uuid.uuid4().hex[:8]}"
    executable = shutil.which("openclaw.cmd") if os.name == "nt" else shutil.which("openclaw")
    if not executable:
        raise RuntimeError("PATH 中找不到 openclaw")
    command = [executable, "agent", "--agent", "main", "--session-id", session_id,
               "--message", prompt, "--json", "--timeout", "120"]
    if os.name == "nt":
        node = shutil.which("node")
        entry = Path(executable).parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if not node or not entry.exists():
            raise RuntimeError("找不到 OpenClaw 的 Node.js 入口")
        command = [node, str(entry), *command[1:]]
    completed = subprocess.run(
        command,
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout[-1000:])
    envelope = json.loads(completed.stdout)
    output = envelope["result"]["payloads"][0]["text"]
    trace = None
    trace_id = None
    for _ in range(30):
        fresh = [item for item in request("/api/traces?limit=100") if item["trace_id"] not in before]
        if fresh:
            trace_id = fresh[0]["trace_id"]
            current = request(f"/api/traces/{trace_id}")["trace"]
            if current["runtime_status"] == "completed" and current["total_tokens"] > 0 and current["duration_ms"] > 0:
                trace = current
                break
        time.sleep(1)
    if trace is None:
        raise RuntimeError("OpenClaw 已返回，但 30 秒内没有收到对应 OTLP Trace")
    result = request(f"/api/traces/{trace['trace_id']}/evaluate", "POST", {
        "case_id": case["case_id"], "actual_output": output, "experiment_id": experiment["experiment_id"],
    })
    return {"case": case["name"], "output": output, "trace_id": trace["trace_id"],
            "tokens": trace["total_tokens"], "duration_ms": trace["duration_ms"],
            "passed": result["passed"], "score": result["score"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    experiment, cases = setup(args.limit)
    request(f"/api/experiments/{experiment['experiment_id']}/status", "POST", {"status": "running"})
    existing = request(f"/api/eval-experiments/{experiment['experiment_id']}")
    completed_cases = {run["case_id"] for run in existing["runs"]}
    results = []
    for index, case in enumerate(cases[:args.limit]):
        if case["case_id"] in completed_cases:
            continue
        result = run_case(experiment, case, index)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    request(f"/api/experiments/{experiment['experiment_id']}/status", "POST", {"status": "completed"})
    print(json.dumps(request(f"/api/eval-experiments/{experiment['experiment_id']}")["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
