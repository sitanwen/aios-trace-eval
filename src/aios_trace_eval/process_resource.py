"""Cross-platform process-tree resource sampling for executable adapters."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List


class ProcessTreeSampler:
    """Sample a launched CLI and all of its descendants until it exits."""

    def __init__(
        self,
        pid: int,
        *,
        service_name: str,
        run_id: str,
        runtime_type: str,
        interval_seconds: float = 0.5,
    ) -> None:
        self.pid = pid
        self.service_name = service_name
        self.run_id = run_id
        self.runtime_type = runtime_type
        self.interval_seconds = max(0.1, interval_seconds)
        self.samples: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._processes: Dict[int, Any] = {}

    def start(self) -> None:
        self._sample()
        self._thread.start()

    def stop(self) -> List[Dict[str, Any]]:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self._sample()
        return self.samples

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        try:
            import psutil
        except ImportError:
            return
        try:
            root = psutil.Process(self.pid)
            discovered = {process.pid: process for process in [root, *root.children(recursive=True)]}
        except psutil.Error:
            return

        rss = 0.0
        cpu_percent = 0.0
        alive = 0
        for pid, discovered_process in discovered.items():
            try:
                process = self._processes.get(pid)
                if process is None:
                    process = discovered_process
                    process.cpu_percent(interval=None)
                    self._processes[pid] = process
                if not process.is_running():
                    continue
                alive += 1
                rss += float(process.memory_info().rss)
                cpu_percent += float(process.cpu_percent(interval=None))
            except psutil.Error:
                continue

        if not alive:
            return
        timestamp_ns = time.time_ns()
        attributes = json.dumps({
            "aios.resource.source": "runner_process_tree",
            "aios.resource.scope": "process_tree",
            "aios.run.id": self.run_id,
            "aios.runtime.type": self.runtime_type,
            "process.root.pid": self.pid,
            "process.count": alive,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.samples.extend([
            _sample(self.service_name, self.run_id, "process.memory.rss", timestamp_ns,
                    rss, "By", attributes),
            _sample(self.service_name, self.run_id, "process.cpu.utilization", timestamp_ns,
                    cpu_percent / 100.0, "1", attributes),
        ])


def _sample(
    service_name: str,
    run_id: str,
    metric_name: str,
    timestamp_ns: int,
    value: float,
    unit: str,
    attributes_json: str,
) -> Dict[str, Any]:
    return {
        "service_name": service_name,
        "run_id": run_id,
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
