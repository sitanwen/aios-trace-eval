"""多平台 Agent 适配器。"""
from .base import AgentAdapter
from .registry import adapter_capabilities, available_adapters, get_adapter, select_adapter

__all__ = [
    "AgentAdapter",
    "adapter_capabilities",
    "available_adapters",
    "get_adapter",
    "select_adapter",
]
