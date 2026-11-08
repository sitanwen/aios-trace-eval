"""Execute every case in an evaluation experiment against OpenClaw."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse


ProgressFn = Callable[[str, int, int, str, Optional[str]], None]


def _request(base_url: str, path: str, method: str = "GET",
             payload: Dict[str, Any] | None = None, timeout: float = 90.0) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body[:400]}") from exc


def gateway_port_from_endpoint(endpoint: str, default: str) -> str:
    try:
        return str(urlparse(endpoint or "").port or default)
    except ValueError:
        return default


def _openclaw_command(prompt: str, session_id: str, agent: str, timeout_s: int) -> List[str]:
    executable = shutil.which("openclaw.cmd") if os.name == "nt" else shutil.which("openclaw")
    if not executable:
        raise RuntimeError("PATH 中找不到 openclaw，请确认 OpenClaw 已安装")
    command = [executable, "agent", "--agent", agent, "--session-id", session_id,
               "--message", prompt, "--json", "--timeout", str(timeout_s)]
    if os.name == "nt":
        node = shutil.which("node")
        entry = Path(executable).parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if not node or not entry.exists():
            raise RuntimeError("找不到 OpenClaw 的 Node.js 入口")
        command = [node, str(entry), *command[1:]]
    return command


def _extract_output(envelope: Dict[str, Any]) -> str:
    try:
        return envelope["result"]["payloads"][0]["text"]
    except (KeyError, IndexError, TypeError):
        pass
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if isinstance(result, dict):
        for key in ("text", "output", "content", "message"):
            if isinstance(result.get(key), str):
                return result[key]
    if isinstance(envelope, dict) and isinstance(envelope.get("text"), str):
        return envelope["text"]
    return json.dumps(envelope, ensure_ascii=False)[:2000]


def _run_one_case(base_url: str, experiment_id: str, case: Dict[str, Any], *,
                  gateway_port: str, agent: str, timeout_s: int,
                  poll_timeout_s: float) -> Dict[str, Any]:
    before = {item["trace_id"] for item in _request(base_url, "/api/traces?limit=500")}
    session_id = f"eval-{experiment_id[-6:]}-{uuid.uuid4().hex[:10]}"
    environment = os.environ.copy()
    environment["OPENCLAW_GATEWAY_PORT"] = gateway_port
    completed = subprocess.run(
        _openclaw_command(case["input"], session_id, agent, timeout_s),
        env=environment, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout_s + 30,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout[-800:] or "openclaw 退出码非 0")
    output = _extract_output(json.loads(completed.stdout))

    trace = None
    deadline = time.time() + poll_timeout_s
    while time.time() < deadline:
        fresh = [item for item in _request(base_url, "/api/traces?limit=500")
                 if item["trace_id"] not in before]
        for candidate in fresh:
            current = _request(base_url, f"/api/traces/{candidate['trace_id']}")["trace"]
            if current["runtime_status"] == "completed" and (current.get("total_tokens") or 0) > 0:
                trace = current
                break
        if trace:
            break
        time.sleep(1)
    if trace is None:
        raise RuntimeError(f"{int(poll_timeout_s)} 秒内没有收到对应 OTLP Trace")

    result = _request(base_url, f"/api/traces/{trace['trace_id']}/evaluate", "POST", {
        "case_id": case["case_id"], "actual_output": output, "experiment_id": experiment_id,
    })
    return {"case": case.get("name"), "trace_id": trace["trace_id"],
            "passed": result.get("passed"), "score": result.get("score")}


def run_experiment(*, base_url: str, experiment: Dict[str, Any], cases: List[Dict[str, Any]],
                   gateway_port: str, agent: str = "main", timeout_s: int = 120,
                   poll_timeout_s: float = 30.0,
                   on_progress: Optional[ProgressFn] = None) -> List[Dict[str, Any]]:
    experiment_id, total = experiment["experiment_id"], len(cases)
    results: List[Dict[str, Any]] = []
    errors: List[str] = []
    for index, case in enumerate(cases):
        name = case.get("name") or case.get("case_id") or f"case-{index + 1}"
        if on_progress:
            on_progress("running", index, total, name, None)
        try:
            results.append(_run_one_case(
                base_url, experiment_id, case, gateway_port=gateway_port,
                agent=agent, timeout_s=timeout_s, poll_timeout_s=poll_timeout_s,
            ))
        except Exception as exc:  # continue so one case does not hide all other results
            errors.append(f"{name}: {str(exc)[:300]}")
        if on_progress:
            on_progress("running", index + 1, total, name, None)
    if errors:
        raise RuntimeError("；".join(errors[:5]))
    return results
