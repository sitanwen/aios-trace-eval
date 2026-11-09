"""Agent 平台适配器抽象契约。

一个平台适配器封装该平台与通用核心之间的全部差异，集中在三个维度：

1. 归一化（normalize_run）：平台私有 span/属性 → CanonicalRun；
2. 任务执行（build_command）：如何启动该平台的 agent 执行一个 case；
3. 资源采样（extract_process_samples）：平台自报的进程级 CPU/内存指标。

接收端的 OTLP/HTTP 与 protobuf 解码对所有平台一致，不在适配器范围内。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..canonical import CanonicalRun


@dataclass(frozen=True)
class AdapterCapabilities:
    """Product-facing capabilities exposed by an adapter."""

    observe: bool = True
    execute: bool = False
    process_metrics: bool = False
    execution_modes: tuple[str, ...] = ("observe_only",)


@dataclass
class ExecutionPlan:
    """Everything the runner needs to execute one case safely."""

    command: List[str]
    cwd: Optional[str] = None
    environment: Dict[str, str] = field(default_factory=dict)
    output_format: str = "text"
    resource_scope: str = "process_tree"


class AgentAdapter(ABC):
    """所有平台适配器的基类。"""

    #: 适配器唯一标识，用于注册表查找与手动指定。
    name: str = ""
    capabilities = AdapterCapabilities()

    #: 归一化时优先读取的标准 GenAI 语义字段，厂商私有字段仅作兜底。
    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} 必须定义 name")

    # -- 自动探测 ---------------------------------------------------------
    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        """根据 span 名与属性特征返回匹配置信度，取值 0.0–1.0。

        通用适配器返回低置信度作为兜底；平台私有适配器命中特征字段时
        返回更高置信度。无法确定时返回 0.0。
        """
        return 0.0

    # -- Trace 有效性判定 -------------------------------------------------
    def is_run_trace(self, spans: List[Dict[str, Any]]) -> bool:
        """判断一组 span 是否对应一次真实 agent run。

        用于排除导出器自诊断、独立 usage accounting 等非任务 trace。
        默认全部视为有效，平台可覆写。
        """
        return True

    # -- 归一化（必须实现）-----------------------------------------------
    @abstractmethod
    def normalize_run(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
        trace_id: str,
    ) -> CanonicalRun:
        """把同一 trace_id 下的 span 列表归一化为 CanonicalRun。"""

    # -- 任务执行（仅可执行型平台需要）-----------------------------------
    def build_command(
        self,
        case: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[str]:
        """构造启动该平台 agent 执行单个 case 的命令行参数。

        context 携带 session_id、agent、超时、运行模式等运行参数。
        仅观测型平台（无本地可执行体）不需要实现。
        """
        raise NotImplementedError(f"{self.name} 不支持本地任务执行")

    def build_execution(
        self,
        case: Dict[str, Any],
        context: Dict[str, Any],
    ) -> ExecutionPlan:
        """Build a complete plan while preserving the legacy command API."""
        return ExecutionPlan(
            command=self.build_command(case, context),
            cwd=context.get("working_directory") or None,
        )

    def parse_output(self, stdout: str, stderr: str = "") -> str:
        """Extract the final agent response from CLI output."""
        return stdout.strip()

    # -- 资源采样（可选）-------------------------------------------------
    def extract_process_samples(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """从 span 属性提取平台自报的进程级 CPU/内存采样。

        默认不提取；同主机场景的通用 psutil 采样由 local_resource 负责。
        """
        return []
