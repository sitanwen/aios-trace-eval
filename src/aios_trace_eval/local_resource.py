"""Best-effort same-host process sampler for OTLP clients.

Remote OpenClaw deployments must export their own process metrics. When the
Gateway and this receiver share a host, the OTLP TCP connection lets us resolve
the sending process without requiring a platform-specific PID configuration.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
from typing import Any, Dict, List


_lock = threading.Lock()
_processes: Dict[str, Any] = {}
_warmed: set[int] = set()


def sample_local_process(client_host: str, client_port: int, service_name: str) -> List[Dict[str, Any]]:
    try:
        import psutil
        if not ipaddress.ip_address(client_host).is_loopback:
            return []
    except (ImportError, ValueError):
        return []

    with _lock:
        process = _processes.get(service_name)
        if process is None or not process.is_running():
            process = _resolve_process(psutil, client_port)
            if process is None:
                return []
            _processes[service_name] = process

        now_ns = time.time_ns()
        attrs = json.dumps(
            {"aios.resource.source": "same_host_process", "process.pid": process.pid},
            sort_keys=True,
            separators=(",", ":"),
        )
        rss = float(process.memory_info().rss)
        rows = [_sample(service_name, "openclaw.memory.rss_bytes", now_ns, rss, "By", attrs)]
        cpu_percent = process.cpu_percent(interval=None)
        if process.pid in _warmed:
            rows.append(_sample(
                service_name,
                "openclaw.process.cpu_core_ratio",
                now_ns,
                float(cpu_percent) / 100.0,
                "1",
                attrs,
            ))
        else:
            _warmed.add(process.pid)
        return rows


def _resolve_process(psutil: Any, client_port: int) -> Any | None:
    try:
        for connection in psutil.net_connections(kind="tcp"):
            if not connection.pid or not connection.laddr or connection.laddr.port != client_port:
                continue
            return psutil.Process(connection.pid)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return None
    return None


def _sample(
    service_name: str,
    metric_name: str,
    timestamp_ns: int,
    value: float,
    unit: str,
    attributes_json: str,
) -> Dict[str, Any]:
    return {
        "service_name": service_name,
        "metric_name": metric_name,
        "timestamp_ns": timestamp_ns,
        "value": value,
        "count": 1,
        "sum_value": value,
        "min_value": value,
        "max_value": value,
        "unit": unit,
        "attributes_json": attributes_json,
    }
