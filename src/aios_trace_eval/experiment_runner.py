"""Execute evaluation cases through platform-specific agent adapters."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, urlparse

from .adapters.registry import get_adapter
from .process_resource import ProcessTreeSampler


ProgressFn = Callable[[str, int, int, str, Optional[str]], None]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENCLAW_ROOT = PROJECT_ROOT / "work" / "openclaw-8.2"


def _execute_cli(
    base_url: str,
    command: List[str],
    *,
    cwd: Optional[str],
    environment: Dict[str, str],
    timeout_s: int,
    service_name: str,
    run_id: str,
    runtime_type: str,
) -> subprocess.CompletedProcess[str]:
    """Run one CLI and collect its complete process tree at a fixed cadence."""
    executable = command[0]
    explicit_executable = Path(executable).expanduser() if any(
        separator in executable for separator in ("/", "\\")
    ) else None
    if explicit_executable is not None:
        if not explicit_executable.is_file():
            raise RuntimeError(f"{runtime_type} 可执行文件不存在：{explicit_executable}")
        command[0] = str(explicit_executable.resolve())
    elif shutil.which(executable, path=environment.get("PATH")) is None:
        raise RuntimeError(
            f"找不到 {runtime_type} CLI：{executable}。请安装后加入 PATH，或在 Agent 配置中填写可执行文件。"
        )
    if cwd and not Path(cwd).expanduser().is_dir():
        raise RuntimeError(f"Agent 工作目录不存在：{cwd}")
    process = subprocess.Popen(
        command, cwd=cwd, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    interval = float(environment.get("AIOS_RESOURCE_SAMPLE_SECONDS", "0.5"))
    sampler = ProcessTreeSampler(
        process.pid,
        service_name=service_name,
        run_id=run_id,
        runtime_type=runtime_type,
        interval_seconds=interval,
    )
    sampler.start()
    try:
        stdout, stderr = process.communicate(timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise TimeoutError(f"{runtime_type} 执行超过 {timeout_s + 30} 秒")
    finally:
        samples = sampler.stop()
        if samples:
            try:
                _request(base_url, "/api/resource-samples", "POST", {"samples": samples})
            except Exception:
                # Resource telemetry must not turn a valid agent answer into a runtime failure.
                pass
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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


def _adapter_name_for_protocol(protocol: Optional[str]) -> str:
    """把 agent 的 protocol 字段映射到适配器名，执行时据此选择 build_command。"""
    name = (protocol or "").strip().lower()
    if name in {"", "openclaw", "openclaw_gateway"}:
        return "openclaw"
    if name in {"claude_code", "dsh", "hermes", "generic"}:
        return name
    return "openclaw"


def _dsh_api_key(environment: Dict[str, str]) -> Optional[str]:
    """从 DSH 凭据文件读取 DEEPSEEK_API_KEY（已在环境里则直接用）。"""
    existing = environment.get("DEEPSEEK_API_KEY")
    if existing:
        return existing
    home = Path(environment.get("DSH_HOME") or (Path.home() / ".dsh"))
    cred = home / ".credentials.yaml"
    if not cred.is_file():
        return None
    text = cred.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # 可选依赖，没装则走下面的行解析
        data = yaml.safe_load(text) or {}
        value = (data.get("refs") or {}).get("DEEPSEEK_API_KEY")
        if value:
            return str(value)
    except Exception:
        pass
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("DEEPSEEK_API_KEY:"):
            return stripped.split(":", 1)[1].strip().strip("'\"") or None
    return None


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
    *, include_run_id: bool = False,
) -> None:
    existing = [
        item for item in environment.get("OTEL_RESOURCE_ATTRIBUTES", "").split(",")
        if item and not item.startswith(("aios.run.id=", "aios.case.id=", "aios.experiment.id="))
    ]
    attributes = [
        f"aios.case.id={quote(case_id, safe='')}",
        f"aios.experiment.id={quote(experiment_id, safe='')}",
    ]
    # Local CLI children inherit this reliably. A persistent Gateway does not.
    if include_run_id:
        attributes.insert(0, f"aios.run.id={quote(run_id, safe='')}")
    existing.extend(attributes)
    environment["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(existing)


def _select_new_trace(
    base_url: str,
    before: set[str],
    *,
    service_name: str = "",
    started_at_ns: int = 0,
) -> Optional[Dict[str, Any]]:
    """Select the primary completed Trace without relying on propagated run_id."""
    candidates = []
    for item in _request(base_url, "/api/traces?limit=200"):
        if item["trace_id"] in before:
            continue
        if service_name and item.get("service_name") != service_name:
            continue
        if started_at_ns and (item.get("started_at_ns") or 0) < started_at_ns - 5_000_000_000:
            continue
        if item.get("runtime_status") != "completed":
            continue
        if (item.get("total_tokens") or 0) <= 0 or (item.get("duration_ms") or 0) <= 0:
            continue
        candidates.append(item)
    if not candidates:
        return None
    # A CLI invocation may export several Trace records. The run Trace normally
    # contains the most model calls/tokens and has the widest duration.
    return max(candidates, key=lambda item: (
        item.get("model_calls") or 0,
        item.get("total_tokens") or 0,
        item.get("duration_ms") or 0,
        item.get("ended_at_ns") or 0,
    ))


def _run_one_case(base_url: str, experiment_id: str, case: Dict[str, Any], *,
                  gateway_port: str, agent: str, timeout_s: int,
                  poll_timeout_s: float, run_batch_id: Optional[str] = None,
                  session_id: Optional[str] = None,
                  adapter_name: str = "openclaw",
                  agent_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    agent_config = agent_config or {}
    run_id = f"run_{uuid.uuid4().hex}"
    _request(base_url, f"/api/experiments/{experiment_id}/attempts", "POST", {
        "case_id": case["case_id"], "run_id": run_id, "run_batch_id": run_batch_id,
    })
    session_id = session_id or f"eval-{experiment_id[-6:]}-{uuid.uuid4().hex[:10]}"
    environment = os.environ.copy()
    _set_run_resource_attributes(
        environment, run_id, case["case_id"], experiment_id,
        include_run_id=adapter_name in {"claude_code", "dsh", "hermes"},
    )
    before = {item["trace_id"] for item in _request(base_url, "/api/traces?limit=200")}
    invocation_started_ns = time.time_ns()
    try:
        if adapter_name == "openclaw":
            environment["OPENCLAW_GATEWAY_PORT"] = gateway_port
            command, cwd = _openclaw_command(
                case["input"], session_id, agent, timeout_s, environment
            )
            parse_json = True
        else:
            adapter = get_adapter(adapter_name)
            execution_mode = agent_config.get("execution_mode") or "local_cli"
            if execution_mode != "local_cli":
                raise RuntimeError(
                    f"{adapter_name} 当前配置为 {execution_mode}；平台主动运行实验需要 local_cli"
                )
            configured_args = agent_config.get("extra_args")
            if isinstance(configured_args, str):
                configured_args = shlex.split(configured_args)
            context = {
                "session_id": session_id,
                "timeout": timeout_s,
                "model": agent_config.get("model") or environment.get("AIOS_AGENT_MODEL"),
                "profile": agent_config.get("profile") or environment.get("AIOS_AGENT_PROFILE")
                           or ("headless" if adapter_name == "dsh" else ""),
                "extra_args": configured_args or
                              shlex.split(environment.get("AIOS_AGENT_EXTRA_ARGS") or ""),
                "executable": agent_config.get("executable"),
                "working_directory": agent_config.get("working_directory"),
            }
            plan = adapter.build_execution(case, context)
            command, cwd = plan.command, plan.cwd
            parse_json = False
            environment.setdefault("OTEL_TRACES_EXPORTER", "otlp")
            environment.setdefault("OTEL_METRICS_EXPORTER", "otlp")
            environment.setdefault("OTEL_LOGS_EXPORTER", "otlp")
            environment.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
            environment.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", base_url.rstrip("/"))
            if adapter_name == "claude_code":
                environment.setdefault("OTEL_SERVICE_NAME", "claude-code")
                environment.setdefault("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
                environment.setdefault("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "1")
            elif adapter_name == "hermes":
                environment.setdefault("OTEL_SERVICE_NAME", "hermes-agent")
                # hermes-plugin-otel-tracing 的开关与导出方式
                environment.setdefault("HERMES_OTEL_ENABLED", "true")
                environment.setdefault("HERMES_OTEL_EXPORTER", "otlp")
                environment.setdefault("HERMES_OTEL_CAPTURE_CONTENT", "false")
            elif adapter_name == "dsh":
                environment.setdefault("OTEL_SERVICE_NAME", "dsh-agent")
                # DSH 的 deepseek-official 路由必须从环境拿到 API Key
                api_key = _dsh_api_key(environment)
                if api_key:
                    environment["DEEPSEEK_API_KEY"] = api_key
                # DSH(Node) 不支持 SOCKS 代理，移除 all_proxy 免得报错
                environment.pop("all_proxy", None)
                environment.pop("ALL_PROXY", None)
        if adapter_name == "openclaw":
            completed = subprocess.run(
                command, cwd=cwd,
                env=environment, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_s + 30,
            )
        else:
            completed = _execute_cli(
                base_url, command, cwd=cwd, environment=environment,
                timeout_s=timeout_s,
                service_name=environment["OTEL_SERVICE_NAME"],
                run_id=run_id, runtime_type=adapter_name,
            )
        if completed.returncode:
            raise RuntimeError(
                completed.stderr.strip() or completed.stdout[-800:] or f"{adapter_name} 退出码非 0"
            )
        output = _extract_output(json.loads(completed.stdout)) if parse_json \
            else adapter.parse_output(completed.stdout, completed.stderr)
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
    candidate_id = None
    candidate_since = None
    while time.time() < deadline:
        if adapter_name in {"claude_code", "dsh", "hermes"}:
            try:
                exact = _request(base_url, f"/api/traces/by-run-id/{run_id}")["trace"]
                if exact.get("runtime_status") == "completed" \
                        and (exact.get("total_tokens") or 0) > 0:
                    trace = exact
                    break
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    raise
        service_name = "" if adapter_name == "openclaw" else environment.get("OTEL_SERVICE_NAME", "")
        candidate = _select_new_trace(
            base_url, before, service_name=service_name,
            started_at_ns=invocation_started_ns,
        )
        if candidate:
            if candidate["trace_id"] != candidate_id:
                candidate_id = candidate["trace_id"]
                candidate_since = time.monotonic()
            elif candidate_since is not None and time.monotonic() - candidate_since >= 1.0:
                trace = _request(base_url, f"/api/traces/{candidate_id}")["trace"]
                break
        time.sleep(0.5)
    if trace is None:
        message = f"{int(poll_timeout_s)} 秒内没有收到对应 OTLP Trace"
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
                   run_batch_id: Optional[str] = None,
                   shared_session: bool = False,
                   protocol: Optional[str] = None,
                   agent_config: Optional[Dict[str, Any]] = None,
                   on_progress: Optional[ProgressFn] = None) -> List[Dict[str, Any]]:
    experiment_id, total = experiment["experiment_id"], len(cases)
    adapter_name = _adapter_name_for_protocol(protocol)
    shared_id = f"eval-{experiment_id[-6:]}-{uuid.uuid4().hex[:10]}" if shared_session else None
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
                run_batch_id=run_batch_id, session_id=shared_id,
                adapter_name=adapter_name,
                agent_config=agent_config,
            ))
        except Exception as exc:  # continue so one case does not hide all other results
            errors.append(f"{name}: {str(exc)[:300]}")
        if on_progress:
            on_progress("running", index + 1, total, name, None)
    if errors:
        raise RuntimeError("；".join(errors[:5]))
    return results
