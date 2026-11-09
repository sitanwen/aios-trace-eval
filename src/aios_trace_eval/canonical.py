"""平台无关的统一规范模型（Canonical Model）。

所有平台适配器都把各自的 OTel Trace 翻译成这里的数据结构，平台核心的
评测、存储、聚合与前端只依赖该模型，不识别任何厂商私有字段。

设计目标：
- 与现有 storage 期望的 trace 汇总字段保持一致，降低迁移成本；
- 保留完整 span 列表与原始属性，供轨迹评估与证据回溯；
- 厂商私有属性仅在对应 adapter 内读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CanonicalTokens:
    """一次 run 的 token 用量，按语义分类。"""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total: int = 0

    def add(self, other: "CanonicalTokens") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other
        self.cache_write += other
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.total += other.total


@dataclass
class CanonicalSpan:
    """归一化单 span，属性以原始 dict 保留。"""

    span_id: str
    parent_span_id: str = ""
    name: str = ""
    kind: str = ""
    started_at_ns: int = 0
    ended_at_ns: int = 0
    duration_ms: float = 0.0
    status: str = "unset"
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalRun:
    """一次 agent run 的平台无关表示，是核心流程的唯一输入口径。"""

    trace_id: str
    service_name: str = ""
    run_id: str = ""
    case_id: Optional[str] = None
    experiment_id: Optional[str] = None
    version: Optional[str] = None
    agent: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    started_at_ns: int = 0
    ended_at_ns: int = 0
    duration_ms: float = 0.0
    runtime_status: "runtime_status"
    runtime_status: str = "completed"
    tokens: CanonicalTokens = field(default_factory=CanonicalTokens)
    cost_usd: Optional[float] = None
    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_loops: int = 0
    failovers: int = 0
    ttft_ms: Optional[float] = None
    spans: List[CanonicalSpan] = field(default_factory=list)

    def to_trace_summary(self) -> Dict[str, Any]:
        """映射到 storage 期望的扁平 dict。"""
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id or self.trace_id,
            "service_name": self.service_name,
            "case_id": self.case_id,
            "experiment_id": self.experiment_id,
            "version": self.version,
            "agent": self.agent,
            "provider": self.provider,
            "model": self.model,
            "started_at_ns": self.started_at_ns,
            "ended_at_ns": self.ended_at_ns,
            "duration_ms": self.duration_ms,
            "runtime_status": self.runtime_status,
            "input_tokens": self.tokens.input,
            "output_tokens": self.tokens.output,
            "cache_read_tokens": self.tokens.cache_read,
            "cache_write_tokens": self.tokens.cache_write,
            "reasoning_tokens": self.tokens.reasoning,
            "total_tokens": self.tokens.total,
            "cost_usd": self.cost_usd,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "tool_loops": self.tool_loops,
            "failovers": self.failovers,
            "ttft_ms": self.ttft_ms,
        }
