"""评测集定义：输入 + 期望终态 + 验收断言 三元组。

沿用评测体系共识：一条用例 = 任务输入 + 环境快照 + 验收断言，三者冻结，
与版本无关，保证多次运行、跨版本对比时的可复现性。

断言分三类，判定优先级从高到低（见 judge.py）：
  - terminal_state：环境终态断言（判定依据是执行后的系统状态，不是输出文本）
  - rule：轨迹规则（如 token 上限、耗时上限、工具循环次数）
  - llm_judge：开放产出的模型判分（最后手段，结果不参与门禁）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class TerminalAssertion:
    """环境终态断言。check 为可执行的回调或一条命令字符串。

    判定依据是「执行后的系统状态」，而非 agent 输出的文本——这是评测与业务之间
    唯一的硬连接。check 必须幂等、可超时、失败可定位。
    """
    name: str
    check: str            # 例如一条 shell 命令，退出码 0 = 通过
    timeout_s: float = 30.0


@dataclass
class RuleAssertion:
    """轨迹/效率规则断言，作用于提取出的 RunMetrics。"""
    name: str
    field: str            # RunMetrics 的字段名，如 "duration_ms" / "tokens.total"
    operator: str         # "lt" | "le" | "gt" | "ge" | "eq"
    value: float
    # 可选说明，如「失败任务不参与本规则」等
    note: str = ""


@dataclass
class EvalCase:
    """一条评测用例。"""
    id: str
    query: str                       # 任务输入
    description: str = ""
    # 期望终态：对执行路径与目标状态的描述（长程任务下替代“参考答案文本”）
    expected_state: str = ""
    # 环境快照（如需要的 fixture 文件、依赖镜像摘要）
    fixtures: List[str] = field(default_factory=list)
    # 断言
    terminal_assertions: List[TerminalAssertion] = field(default_factory=list)
    rule_assertions: List[RuleAssertion] = field(default_factory=list)
    # 是否用 LLM 判分（开放产出才用，需人工校准，不参与门禁）
    llm_judge: bool = False
    # 重复运行次数（Pass@k / Pass^k 需要多次试验）
    repeats: int = 1
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvalSet:
    """评测集：一组用例。"""
    name: str
    cases: List[EvalCase] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "cases": [c.to_dict() for c in self.cases]}
