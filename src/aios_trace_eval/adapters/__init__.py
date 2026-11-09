"""多平台 Agent 适配器。"""
from .base import AgentAdapter
from .registry import available_adapters, get_adapter, select_adapter

__all__ = [
    "AgentAdapter",
    "available_adapters",
    "get_adapter",
    "select_adapter",
]
