"""适配器注册表与自动探测。

维护已支持平台的适配器实例，提供两种选择方式：
- get_adapter(name)：按名称手动指定；
- select_adapter(spans, resource)：依据各适配器 detect 置信度自动选择，
  无法确定时回落到通用 GenAI 适配器。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .base import AgentAdapter
from .claude_code.normalizer import ClaudeCodeAdapter
from .dsh.normalizer import DSHAdapter
from .generic.normalizer import GenericGenAIAdapter
from .hermes.normalizer import HermesAdapter
from .openclaw.normalizer import OpenClawAdapter


def _build_registry() -> Dict[str, AgentAdapter]:
    instances: List[AgentAdapter] = [
        OpenClawAdapter(),
        DSHAdapter(),
        ClaudeCodeAdapter(),
        HermesAdapter(),
        GenericGenAIAdapter(),
    ]
    return {adapter.name: adapter for adapter in instances}


_REGISTRY = _build_registry()


def available_adapters() -> List[str]:
    return sorted(_REGISTRY.keys())


def adapter_capabilities() -> List[Dict[str, Any]]:
    """列出各平台的能力矩阵，供前端展示与接入选型。"""
    return [{"name": adapter.name, **asdict(adapter.capabilities)}
            for adapter in _REGISTRY.values()]


def get_adapter(name: str) -> AgentAdapter:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"未知适配器 '{name}'，已支持：{', '.join(available_adapters())}"
        ) from exc


def select_adapter(
    spans: List[Dict[str, Any]],
    resource: Dict[str, Any],
    forced: Optional[str] = None,
) -> AgentAdapter:
    """选择适配器。forced 非空时手动指定，否则按置信度自动探测。"""
    if forced:
        return get_adapter(forced)

    best: Optional[AgentAdapter] = None
    best_score = -1.0
    for adapter in _REGISTRY.values():
        score = adapter.detect(spans, resource)
        if score > best_score:
            best, best_score = adapter, score
    assert best is not None
    return best
