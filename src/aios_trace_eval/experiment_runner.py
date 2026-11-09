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
from urllib.parse import quote, urlparse


ProgressFn = Callable[[str, int, int, str, Optional[str]], None]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENCLAW_ROOT = PROJECT_ROOT / "work" / "openclaw-8.2"


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


def _existing_path(value: str, label: str, *, directory: bool) -> Path:
    path = Path(value).expanduser().resolve()
    exists = path.is_dir() if directory else path.is_file()
    if not exists:
        kind = "目录" if directory else "文件"
        raise RuntimeError(f"{label}{kind}不存在：{path}")
    return path


def _apply_openclaw_paths(
    environment: Dict[str, str], *, default_config: Path | None = None,
    default_state: Path | None = None,
) -> None:
    config_value = (
        environment.get("AIOS_OPENCLAW_CONFIG_PATH")
        or environment.get("OPENCLAW_CONFIG_PATH")
    )
    state_value = (
        environment.get("AIOS_OPENCLAW_STATE_DIR")
        or environment.get("OPENCLAW_STATE_DIR")
    )
    if not config_value and default_config is not None and default_config.is_file():
        config_value = str(default_config)
    if not state_value and default_state is not None and default_state.is_dir():
        state_value = str(default_state)

    if config_value:
        environment["OPENCLAW_CONFIG_PATH"] = str(
            _existing_path(config_value, "OpenClaw 配置", directory=False)
        )
    if not state_value:
        raise RuntimeError(
            "未配置独立的 OpenClaw 状态目录。请设置 AIOS_OPENCLAW_STATE_DIR "
            "（或 OPENCLAW_STATE_DIR），不要让旧版 OpenClaw 自动读取其他版本的全局数据库"
        )
    environment["OPENCLAW_STATE_DIR"] = str(
        _existing_path(state_value, "OpenClaw state ", directory=True)
    )


def _runtime_from_executable(executable: str) -> List[str]:
    resolved = shutil.which(executable) or executable
    path = Path(resolved).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"OpenClaw 可执行文件不存在：{path}")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise RuntimeError(f"OpenClaw 文件没有执行权限：{path}")
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        node = shutil.which("node")
        entry = path.parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if not node or not entry.is_file():
            raise RuntimeError(
                "无法解析 Windows OpenClaw 命令入口；请确认 Node.js 可用，"
                "或设置 AIOS_OPENCLAW_ROOT 指向 OpenClaw 安装目录"
            )
        return [node, str(entry)]
    return [str(path)]


def _openclaw_runtime(environment: Dict[str, str]) -> tuple[List[str], Path]:
    configured_command = environment.get("AIOS_OPENCLAW_COMMAND")
    configured_root = environment.get("AIOS_OPENCLAW_ROOT")

    if configured_command:
        _apply_openclaw_paths(environment)
        return _runtime_from_executable(configured_command), PROJECT_ROOT

    root = (
        Path(configured_root).expanduser().resolve()
        if configured_root else DEFAULT_OPENCLAW_ROOT
    )
    bundled_entry = root / "node_modules" / "openclaw" / "openclaw.mjs"
    if bundled_entry.is_file():
        node = shutil.which("node")
        if not node:
            raise RuntimeError("PATH 中找不到 Node.js，无法启动 OpenClaw")
        _apply_openclaw_paths(
            environment,
            default_config=root / "openclaw-deepseek.json5",
            default_state=root / "state",
        )
        return [node, str(bundled_entry)], root

    if configured_root:
        raise RuntimeError(
            f"AIOS_OPENCLAW_ROOT 中找不到 node_modules/openclaw/openclaw.mjs：{root}"
        )

    executable = shutil.which("openclaw.cmd") if os.name == "nt" else shutil.which("openclaw")
    if not executable:
        raise RuntimeError(
            "找不到 OpenClaw。请设置 AIOS_OPENCLAW_COMMAND、AIOS_OPENCLAW_ROOT，"
            "或将 openclaw 加入 PATH"
        )
    _apply_openclaw_paths(environment)
    return _runtime_from_executable(executable), PROJECT_ROOT


def _openclaw_command(
    prompt: str, session_id: str, agent: str, timeout_s: int,
    environment: Dict[str, str],
) -> tuple[List[str], Path]:
    runtime, cwd = _openclaw_runtime(environment)
    command = [*runtime, "agent"]
    mode = environment.get("AIOS_OPENCLAW_MODE", "local").strip().lower()
    if mode not in {"local", "gateway"}:
        raise RuntimeError("AIOS_OPENCLAW_MODE 只支持 local 或 gateway")
    if mode == "local":
        command.append("--local")
    command.extend([
        "--agent", agent, "--session-id", session_id,
        "--message", prompt, "--json", "--timeout", str(timeout_s),
    ])
    return command, cwd


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


def _set_run_resource_attributes(
    environment: Dict[str, str], run_id: str, case_id: str, experiment_id: str,
) -> None:
    existing = [
        item for item in environment.get("OTEL_RESOURCE_ATTRIBUTES", "").split(",")
        if item and not item.startswith(("aios.run.id=", "aios.case.id=", "aios.experiment.id="))
    ]
    existing.extend([
        f"aios.run.id={quote(run_id, safe='')}",
        f"aios.case.id={quote(case_id, safe='')}",
        f"aios.experiment.id={quote(experiment_id, safe='')}",
    ])
    environment["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(existing)


def _run_one_case(base_url: str, experiment_id: str, case: Dict[str, Any], *,
                  gateway_port: str, agent: str, timeout_s: int,
                  poll_timeout_s: float) -> Dict[str, Any]:
    run_id = f"run_{uuid.uuid4().hex}"
    _request(base_url, f"/api/experiments/{experiment_id}/attempts", "POST", {
        "case_id": case["case_id"], "run_id": run_id,
    })
    session_id = f"eval-{experiment_id[-6:]}-{uuid.uuid4().hex[:10]}"
    environment = os.environ.copy()
    environment["OPENCLAW_GATEWAY_PORT"] = gateway_port
    _set_run_resource_attributes(environment, run_id, case["case_id"], experiment_id)
    try:
        command, cwd = _openclaw_command(
            case["input"], session_id, agent, timeout_s, environment
        )
        completed = subprocess.run(
            command, cwd=cwd,
            env=environment, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s + 30,
        )
        if completed.returncode:
            raise RuntimeError(
                completed.stderr.strip() or completed.stdout[-800:] or "openclaw 退出码非 0"
            )
        output = _extract_output(json.loads(completed.stdout))
    except Exception as exc:
        _request(base_url, f"/api/attempts/{run_id}", "PATCH", {
            "status": "runtime_error", "error_type": type(exc).__name__,
            "error_message": str(exc),
        })
        raise

    _request(base_url, f"/api/attempts/{run_id}", "PATCH", {
        "status": "awaiting_trace", "actual_output": output,
    })

    trace = None
    deadline = time.time() + poll_timeout_s
    while time.time() < deadline:
        try:
            current = _request(base_url, f"/api/traces/by-run-id/{quote(run_id, safe='')}")["trace"]
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            current = None
        if current and current["runtime_status"] == "completed" and (current.get("total_tokens") or 0) > 0:
            trace = current
            break
        if trace:
            break
        time.sleep(1)
    if trace is None:
        message = f"{int(poll_timeout_s)} 秒内没有收到 run_id={run_id} 对应的 OTLP Trace"
        _request(base_url, f"/api/attempts/{run_id}", "PATCH", {
            "status": "telemetry_missing", "error_type": "TelemetryMissing",
            "error_message": message,
        })
        raise RuntimeError(message)

    _request(base_url, f"/api/attempts/{run_id}", "PATCH", {
        "status": "evaluating", "trace_id": trace["trace_id"], "actual_output": output,
    })
    try:
        result = _request(base_url, f"/api/traces/{trace['trace_id']}/evaluate", "POST", {
            "case_id": case["case_id"], "actual_output": output, "experiment_id": experiment_id,
        })
    except Exception as exc:
        _request(base_url, f"/api/attempts/{run_id}", "PATCH", {
            "status": "evaluation_error", "trace_id": trace["trace_id"],
            "actual_output": output, "error_type": type(exc).__name__,
            "error_message": str(exc),
        })
        raise
    _request(base_url, f"/api/attempts/{run_id}", "PATCH", {
        "status": "passed" if result.get("passed") else "failed",
        "trace_id": trace["trace_id"], "actual_output": output,
        "score": result.get("score"),
    })
    return {"case": case.get("name"), "trace_id": trace["trace_id"],
            "run_id": run_id, "passed": result.get("passed"), "score": result.get("score")}


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
