"""Run an existing AIOS Trace Eval experiment against its dataset through the real
OpenClaw Gateway.

Unlike run_gsm8k_openclaw.py (GSM8K-specific), this runner:
  - uses an EXISTING experiment/dataset instead of creating new ones
  - sends each case's original input as-is (no GSM8K answer-suffix)
  - lets you point at the right gateway port / agent

Usage:
  export OPENCLAW_GATEWAY_TOKEN="<token>"
  export PYTHONPATH=src
  python scripts/run_dataset_experiment.py --experiment-id exp_xxx --gateway-port 18789
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("AIOS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
CONFIG = Path(os.environ["OPENCLAW_CONFIG_PATH"]).resolve() if os.environ.get("OPENCLAW_CONFIG_PATH") else None
STATE = Path(os.environ["OPENCLAW_STATE_DIR"]).resolve() if os.environ.get("OPENCLAW_STATE_DIR") else None


def request(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body[:500]}") from exc


def resolve_experiment(experiment_id: str) -> tuple[dict, list[dict]]:
    detail = request(f"/api/eval-experiments/{experiment_id}")
    return detail["experiment"], detail["cases"]


def openclaw_command(prompt: str, session_id: str, agent: str, timeout_s: int) -> list[str]:
    executable = shutil.which("openclaw.cmd") if os.name == "nt" else shutil.which("openclaw")
    if not executable:
        raise RuntimeError("PATH 中找不到 openclaw")
    command = [executable, "agent", "--agent", agent, "--session-id", session_id,
               "--message", prompt, "--json", "--timeout", str(timeout_s)]
    if os.name == "nt":
        node = shutil.which("node")
        entry = Path(executable).parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if not node or not entry.exists():
            raise RuntimeError("找不到 OpenClaw 的 Node.js 入口")
        command = [node, str(entry), *command[1:]]
    return command


def run_case(experiment_id: str, case: dict, index: int, oa_agent: str, gateway_port: str, timeout_s: int) -> dict:
    before = {item["trace_id"] for item in request("/api/traces?limit=200")}
    env = os.environ.copy()
    if not env.get("OPENCLAW_GATEWAY_TOKEN"):
        raise RuntimeError("请先设置 OPENCLAW_GATEWAY_TOKEN")
    env["OPENCLAW_GATEWAY_PORT"] = gateway_port
    if CONFIG is not None:
        env["OPENCLAW_CONFIG_PATH"] = str(CONFIG)
    if STATE is not None:
        env["OPENCLAW_STATE_DIR"] = str(STATE)

    session_id = f"eval-{index + 1}-{uuid.uuid4().hex[:8]}"
    command = openclaw_command(case["input"], session_id, oa_agent, timeout_s)
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout_s + 30)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout[-1000:])
    envelope = json.loads(completed.stdout)
    output = envelope["result"]["payloads"][0]["text"]

    trace = None
    for _ in range(30):
        fresh = [item for item in request("/api/traces?limit=200") if item["trace_id"] not in before]
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
        "case_id": case["case_id"], "actual_output": output, "experiment_id": experiment_id,
    })
    return {"case": case["name"], "output": output, "trace_id": trace["trace_id"],
            "tokens": trace["total_tokens"], "duration_ms": trace["duration_ms"],
            "passed": result["passed"], "score": result["score"]}


def main() -> None:
    global API, CONFIG, STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-id", required=True, help="已存在的评测实验 ID")
    parser.add_argument("--api-url", default=API, help="AIOS Trace Eval API 地址")
    parser.add_argument("--gateway-port", default=os.environ.get("OPENCLAW_GATEWAY_PORT", "18790"))
    parser.add_argument("--openclaw-agent", default="main", help="OpenClaw 内的 agent 名")
    parser.add_argument("--timeout", type=int, default=120, help="单条用例超时（秒）")
    parser.add_argument("--openclaw-config", help="可选：指定 OpenClaw 配置文件")
    parser.add_argument("--openclaw-state-dir", help="可选：指定 OpenClaw 状态目录")
    args = parser.parse_args()

    API = args.api_url.rstrip("/")
    if args.openclaw_config:
        CONFIG = Path(args.openclaw_config).expanduser().resolve()
    if args.openclaw_state_dir:
        STATE = Path(args.openclaw_state_dir).expanduser().resolve()

    experiment, cases = resolve_experiment(args.experiment_id)
    print(f"实验：{experiment['name']}  用例数：{len(cases)}  评估器：{experiment.get('evaluator_count', 0)}")

    request(f"/api/experiments/{args.experiment_id}/status", "POST", {"status": "running"})
    results = []
    try:
        for index, case in enumerate(cases):
            print(f"[{index + 1}/{len(cases)}] {case['name']} ...", flush=True)
            result = run_case(args.experiment_id, case, index, args.openclaw_agent,
                              args.gateway_port, args.timeout)
            results.append(result)
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"    {mark} score={result['score']} tokens={result['tokens']} "
                  f"duration={result['duration_ms']}ms trace={result['trace_id'][:12]}…", flush=True)
        request(f"/api/experiments/{args.experiment_id}/status", "POST", {"status": "completed"})
    except Exception:
        request(f"/api/experiments/{args.experiment_id}/status", "POST", {"status": "failed"})
        raise

    summary = request(f"/api/eval-experiments/{args.experiment_id}")["summary"]
    print("\n实验汇总：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
