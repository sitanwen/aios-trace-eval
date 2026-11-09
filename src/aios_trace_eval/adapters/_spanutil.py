"""适配器共享的 span 读取工具。

统一处理 storage 行（含 _attributes）与原始 OTLP dict 两种来源，避免各
适配器重复实现属性解析与类型转换。
"""
from __future__ import annotations

import json
from typing import Any, Dict


def attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    parsed = span.get("_attributes")
    if isinstance(parsed, dict):
        return parsed
    raw = span.get("attributes_json", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}


def number(attrs: Dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in attrs:
            continue
        try:
            return float(attrs[key])
        except (TypeError, ValueError):
            continue
    return None


def integer(attrs: Dict[str, Any], *keys: str) -> int:
    value = number(attrs, *keys)
    return int(value) if value is not None else 0


def text(attrs: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            return str(value)
    return ""
