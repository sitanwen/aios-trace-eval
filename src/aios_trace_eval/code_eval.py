"""Restricted expression evaluator used by the Code evaluator MVP.

This is deliberately not a general Python sandbox.  It accepts one expression,
walks its AST, and evaluates only a small allow-list of data and pure helpers.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class UnsafeExpression(ValueError):
    """Raised when an expression uses syntax outside the allow-list."""


def normalize_number(value: Any) -> str:
    """Extract and canonicalize the last numeric answer from model text."""
    matches = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", str(value))
    if not matches:
        return ""
    try:
        number = Decimal(matches[-1].replace(",", ""))
    except InvalidOperation:
        return ""
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


_FUNCTIONS = {
    "normalize_number": normalize_number,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "abs": abs,
    "min": min,
    "max": max,
}
_NAMES = {"query", "response", "reference", "trace", "trajectory", *list(_FUNCTIONS)}
_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Call, ast.Subscript,
    ast.List, ast.Tuple, ast.Dict, ast.Compare, ast.BoolOp, ast.UnaryOp,
    ast.BinOp, ast.IfExp, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)


def evaluate_expression(expression: str, context: Mapping[str, Any]) -> Any:
    if not expression or len(expression) > 1000:
        raise UnsafeExpression("表达式不能为空且不得超过 1000 字符")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"表达式语法错误：{exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeExpression(f"不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in _NAMES:
            raise UnsafeExpression(f"不允许的变量或函数：{node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                raise UnsafeExpression("只允许调用白名单中的纯函数")
    values = {key: context.get(key) for key in ("query", "response", "reference", "trace", "trajectory")}
    values.update(_FUNCTIONS)
    return eval(compile(tree, "<code-evaluator>", "eval"), {"__builtins__": {}}, values)
