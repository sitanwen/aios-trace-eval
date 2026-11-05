"""AIOS Trace Eval —— 基于 OpenTelemetry GenAI 的 OpenClaw 评测框架。

两个核心点：
  - 可观测：直接接收 OpenClaw diagnostics-otel 的 OTLP/HTTP 数据。
  - 可评测：离线读取 Trace，聚合 token 消耗 / 任务耗时 / 成本 / 每成功任务 Token 等效率指标。
"""

__version__ = "0.1.0"

from .extractor import RunMetrics, extract_run_metrics, aggregate
from .evalset import EvalCase, EvalSet, TerminalAssertion, RuleAssertion
from .runner import Runner, RunResult, PlatformClient
from .judge import judge_case, evaluate_set, build_report, write_report

__all__ = [
    "__version__",
    "RunMetrics",
    "extract_run_metrics",
    "aggregate",
    "EvalCase",
    "EvalSet",
    "TerminalAssertion",
    "RuleAssertion",
    "Runner",
    "RunResult",
    "PlatformClient",
    "judge_case",
    "evaluate_set",
    "build_report",
    "write_report",
]
