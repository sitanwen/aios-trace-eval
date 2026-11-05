"""OpenClaw evaluation runner with exact trace-id correlation.

The runner never searches for the latest trace. A caller must inject the same
W3C trace id into the OpenClaw Gateway request and pass it here for retrieval.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .evalset import EvalCase
from .extractor import RunMetrics, extract_run_metrics


@dataclass
class RunResult:
    case_id: str
    attempt: int
    trace_id: Optional[str] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    metrics: Optional[RunMetrics] = None
    trace: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class PlatformClient:
    base_url: str = "http://127.0.0.1:8080"

    def trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/traces/{trace_id}", timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise


@dataclass
class Runner:
    openclaw_bin: str = "openclaw"
    platform: Optional[PlatformClient] = None
    poll_interval_s: float = 0.5
    poll_timeout_s: float = 60.0

    def run_case(self, case: EvalCase, attempt: int = 0, trace_id: Optional[str] = None) -> RunResult:
        rr = RunResult(case_id=case.id, attempt=attempt, trace_id=trace_id)
        try:
            process = subprocess.run(
                [self.openclaw_bin, "agent", "--local", "--message", case.query],
                capture_output=True,
                text=True,
                timeout=600,
            )
            rr.exit_code, rr.stdout, rr.stderr = process.returncode, process.stdout, process.stderr
        except subprocess.TimeoutExpired as exc:
            rr.error = f"timeout: {exc}"
            return rr
        except FileNotFoundError as exc:
            rr.error = f"openclaw not found: {exc}"
            return rr

        if not trace_id:
            rr.error = (
                "trace_id is required for evaluation correlation; execute through the Gateway "
                "and inject a request-scoped W3C traceparent"
            )
            return rr
        detail = self._wait_for_trace(trace_id)
        if detail is None:
            rr.error = f"trace {trace_id} not received by AIOS within timeout"
            return rr
        rr.trace = _platform_detail_to_trace(detail)
        rr.metrics = extract_run_metrics(rr.trace)
        return rr

    def _wait_for_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        if self.platform is None:
            return None
        deadline = time.time() + self.poll_timeout_s
        while time.time() < deadline:
            detail = self.platform.trace(trace_id)
            if detail is not None:
                return detail
            time.sleep(self.poll_interval_s)
        return None


def _platform_detail_to_trace(detail: Dict[str, Any]) -> Dict[str, Any]:
    trace = detail["trace"]
    return {
        "traceID": trace["trace_id"],
        "spans": [
            {
                "operationName": span["name"],
                "tags": span.get("attributes", {}),
                "duration": span["duration_ms"] * 1000,
            }
            for span in detail.get("spans", [])
        ],
    }
