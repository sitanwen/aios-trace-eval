"""判定器与报告。

判定优先级固定（从高到低）：
  1. terminal_state：环境终态断言（能由系统状态回答的，绝不交给模型判分）
  2. rule：轨迹/效率规则（token 上限、耗时上限、工具循环次数等）
  3. llm_judge：开放产出判分（最后手段，结果不参与门禁）

分层门禁：安全/数据准确性类一票否决，效率类设阈值，不混成一个总分。
"""
from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .evalset import EvalCase
from .extractor import RunMetrics, aggregate
from .runner import RunResult


_OPS = {
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "eq": operator.eq,
}


def _resolve_field(metrics: RunMetrics, field: str) -> Any:
    """解析规则断言里的字段路径，如 'duration_ms'、'tokens.total'、'model_call_count'。"""
    parts = field.split(".")
    obj: Any = metrics
    for p in parts:
        if isinstance(obj, dict):
            obj = obj.get(p)
        else:
            obj = getattr(obj, p, None)
        if obj is None:
            return None
    return obj


@dataclass
class CaseVerdict:
    case_id: str
    passed: bool
    # 分层结果
    terminal: Dict[str, bool] = field(default_factory=dict)   # 终态断言 name -> 通过?
    rules: Dict[str, bool] = field(default_factory=dict)       # 规则断言 name -> 通过?
    llm_score: Optional[float] = None                          # 判分（不参与门禁）
    fail_reasons: List[str] = field(default_factory=list)


def judge_case(case: EvalCase, result: RunResult) -> CaseVerdict:
    """判定单条用例的某次运行结果。"""
    v = CaseVerdict(case_id=case.id, passed=True)
    m = result.metrics

    # 1) 终态断言：环境状态（本框架在隔离沙箱里由外部校验器执行；这里给接口）
    for ta in case.terminal_assertions:
        ok = _run_terminal_check(ta)
        v.terminal[ta.name] = ok
        if not ok:
            v.fail_reasons.append(f"terminal[{ta.name}] failed")
            v.passed = False

    # 2) 规则断言：作用于 RunMetrics
    if m is not None:
        for ra in case.rule_assertions:
            actual = _resolve_field(m, ra.field)
            ok = False
            if actual is not None:
                try:
                    ok = _OPS[ra.operator](float(actual), ra.value)
                except (ValueError, TypeError):
                    ok = False
            v.rules[ra.name] = ok
            if not ok:
                v.fail_reasons.append(
                    f"rule[{ra.name}] {ra.field}={actual} {ra.operator} {ra.value} -> fail"
                )
                v.passed = False
    else:
        # 没拿到 metrics 视为失败（无法判定效率）
        v.fail_reasons.append("no metrics extracted")
        v.passed = False

    # 3) LLM 判分：不参与 passed，仅记录
    if case.llm_judge:
        v.llm_score = None  # 占位；实际判分需固定裁判模型版本，这里留接口

    return v


def _run_terminal_check(ta: Any) -> bool:
    """执行环境终态断言。check 是一条 shell 命令，退出码 0 = 通过。"""
    import subprocess
    try:
        proc = subprocess.run(ta.check, shell=True, timeout=ta.timeout_s)
        return proc.returncode == 0
    except Exception:
        return False


def evaluate_set(case: EvalCase, results: List[RunResult]) -> Dict[str, Any]:
    """对同一用例的多次运行（Pass@k）做汇总。"""
    verdicts = [judge_case(case, r) for r in results]
    n = len(verdicts)
    n_pass = sum(1 for v in verdicts if v.passed)
    return {
        "case_id": case.id,
        "attempts": n,
        "passes": n_pass,
        "pass_k": round(n_pass / n, 4) if n else 0.0,
        "pass_hat_k": 1.0 if n_pass == n else 0.0,  # Pass^k：全部通过才记成功
        "verdicts": [v.__dict__ for v in verdicts],
    }


def build_report(
    evalset_name: str,
    case_summaries: List[Dict[str, Any]],
    all_metrics: List[RunMetrics],
) -> Dict[str, Any]:
    """生成评测报告（结构化 JSON，可再转 Markdown）。"""
    agg = aggregate(all_metrics)
    return {
        "evalset": evalset_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": case_summaries,
        "overall": {
            "cases_passed": sum(1 for c in case_summaries if c["pass_hat_k"] == 1.0),
            "cases_total": len(case_summaries),
        },
        "efficiency": agg,
    }


def write_report(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
