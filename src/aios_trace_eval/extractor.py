"""指标提取器：从一条 OTel Trace 聚合出评测效率指标。

数据源：平台规范化后的 trace，兼容历史离线 trace 数据。
一条 trace ≈ 一次 agent run，内部包含多个 span：
  openclaw.run (root)
    ├── openclaw.model.call (×N)   —— 每次模型调用的 token/耗时/TTFB
    ├── openclaw.model.usage       —— run 级用量汇总
    ├── openclaw.harness.run       —— 任务级耗时
    ├── openclaw.context.assembled —— 上下文组装
    └── openclaw.message.processed —— 处理结果(含 outcome)

关键设计：token 以「每次 model.call 的 usage」为准（provider 逐次回报），
当 model.call 未上报 usage 时回退到 run 级 openclaw.model.usage 汇总 span。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.total += other.total


@dataclass
class RunMetrics:
    """一次 run 的完整效率指标。"""
    trace_id: str = ""
    # 身份
    model: str = ""
    provider: str = ""
    agent: str = ""
    # 耗时（毫秒）
    duration_ms: Optional[float] = None      # run / harness 总耗时
    ttft_ms: Optional[float] = None          # 首次模型调用的 TTFB
    # token
    tokens: TokenUsage = field(default_factory=TokenUsage)
    # 成本（USD）
    cost_usd: Optional[float] = None
    # 调用统计
    model_call_count: int = 0
    tool_call_count: int = 0
    tool_loop_count: int = 0
    failover_count: int = 0
    # 结果
    outcome: str = "unknown"
    error_type: str = ""
    # 上下文
    context_token_budget: Optional[int] = None
    context_message_count: Optional[int] = None


def _num(attrs: Dict[str, Any], *keys: str) -> Optional[float]:
    """按优先级从多个候选属性 key 取数值。"""
    for k in keys:
        v = attrs.get(k)
        if v is None:
            continue
        try:
            f = float(v)
            return f
        except (TypeError, ValueError):
            continue
    return None


def _str(attrs: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = attrs.get(k)
        if v is not None:
            return str(v)
    return ""


def _extract_usage_from_span(span: Dict[str, Any]) -> TokenUsage:
    """从单个 span 的属性里抽取 token 用量，两套命名都读。"""
    attrs = span.get("tags", span.get("attributes", {}))
    if not isinstance(attrs, dict):
        attrs = {}

    def g(*keys: str) -> int:
        for k in keys:
            if k in attrs:
                try:
                    return int(float(attrs[k]))
                except (TypeError, ValueError):
                    continue
        return 0

    u = TokenUsage()
    u.input = g(
        "openclaw.tokens.input",
        "gen_ai.usage.input_tokens",
        "openclaw.model_call.usage.input_tokens",
    )
    u.output = g(
        "openclaw.tokens.output",
        "gen_ai.usage.output_tokens",
        "openclaw.model_call.usage.output_tokens",
    )
    u.cache_read = g(
        "openclaw.tokens.cache_read",
        "gen_ai.usage.cache_read.input_tokens",
        "openclaw.model_call.usage.cache_read_input_tokens",
    )
    u.cache_write = g(
        "openclaw.tokens.cache_write",
        "gen_ai.usage.cache_creation.input_tokens",
        "openclaw.model_call.usage.cache_creation_input_tokens",
    )
    u.reasoning = g("openclaw.model_call.usage.reasoning_output_tokens")
    u.total = g(
        "openclaw.tokens.total",
        "openclaw.model_call.usage.total_tokens",
    )
    return u


def extract_run_metrics(trace: Dict[str, Any]) -> RunMetrics:
    """从一条 trace 提取 run 级指标。

    trace 结构兼容 Jaeger API（spans[].operationName / .tags / .duration）
    与 OTLP 原始 JSON（spans[].name / .attributes / .startTimeUnixNano/endTimeUnixNano）。
    """
    m = RunMetrics()
    spans = trace.get("spans", []) if isinstance(trace, dict) else []
    if not spans:
        return m

    m.trace_id = str(trace.get("traceID", trace.get("traceId", "")))

    # 记录每个 model.call 是否上报了 usage，用于决定是否回退到 model.usage 汇总
    any_call_usage = False

    for span in spans:
        name = span.get("operationName", span.get("name", ""))
        attrs = span.get("tags", span.get("attributes", {}))
        if not isinstance(attrs, dict):
            attrs = {}

        # 时长：Jaeger 用 duration(微秒)，OTLP 用 start/end(纳秒)
        dur_ms = _span_duration_ms(span)

        if name == "openclaw.model.call":
            m.model_call_count += 1
            u = _extract_usage_from_span(span)
            # 只有 provider 回报了非零 usage 才算“有上报”，避免把全零汇总误当真实值
            if u.input or u.output or u.total:
                m.tokens.add(u)
                any_call_usage = True
            # 记录首个非空 TTFB
            if m.ttft_ms is None:
                ttfb = _num(attrs, "openclaw.model_call.time_to_first_byte_ms")
                if ttfb is not None:
                    m.ttft_ms = ttfb

        elif name == "openclaw.model.usage":
            # run 级 accounting span：成本 + 总 token（兜底用）
            if m.cost_usd is None:
                # cost 通常作为独立 metric 上报，span 上不一定有；这里兼容可能的 cost 属性
                c = _num(attrs, "openclaw.cost.usd", "openclaw.cost_usd")
                if c is not None:
                    m.cost_usd = c
            if not any_call_usage:
                m.tokens.add(_extract_usage_from_span(span))

        elif name in ("openclaw.run", "openclaw.harness.run"):
            if dur_ms is not None and (m.duration_ms is None or dur_ms > (m.duration_ms or 0)):
                m.duration_ms = dur_ms

        elif name == "openclaw.context.assembled":
            budget = _num(attrs, "openclaw.context.token_budget")
            if budget is not None:
                m.context_token_budget = int(budget)
            cnt = _num(attrs, "openclaw.context.message_count")
            if cnt is not None:
                m.context_message_count = int(cnt)

        elif name == "openclaw.model.failover":
            m.failover_count += 1

        elif name == "openclaw.message.processed":
            outcome = _str(attrs, "openclaw.outcome")
            if outcome:
                m.outcome = outcome
            err = _str(attrs, "error.type", "openclaw.errorCategory")
            if err:
                m.error_type = err

        # 工具调用：OpenClaw 用 tool.loop 计数 + tool.execution 指标；span 里若出现 tool 相关也计入
        if name == "openclaw.tool.loop":
            m.tool_loop_count += 1
        if name == "openclaw.tool.execution" or attrs.get("gen_ai.operation.name") == "execute_tool":
            m.tool_call_count += 1

        # 身份信息（取第一个非空即可，通常 root/model span 携带）
        if not m.model:
            m.model = _str(attrs, "openclaw.model", "gen_ai.request.model")
        if not m.provider:
            m.provider = _str(attrs, "openclaw.provider", "gen_ai.system", "gen_ai.provider.name")
        if not m.agent:
            m.agent = _str(attrs, "openclaw.agent")

    return m


def _span_duration_ms(span: Dict[str, Any]) -> Optional[float]:
    # Jaeger：duration 单位微秒
    dur = span.get("duration")
    if dur is not None:
        try:
            return float(dur) / 1000.0
        except (TypeError, ValueError):
            pass
    # OTLP：纳秒时间戳
    start = span.get("startTimeUnixNano")
    end = span.get("endTimeUnixNano")
    if start is not None and end is not None:
        try:
            return (int(end) - int(start)) / 1_000_000.0
        except (TypeError, ValueError):
            pass
    return None


def aggregate(metrics: List[RunMetrics]) -> Dict[str, Any]:
    """跨多条 run 聚合，产出评测报告级的汇总指标。

    特别实现「每成功任务 Token」——这是四家商业平台全缺、而本框架的核心差异指标：
    分母是成功任务数（outcome 非 error 的 run），失败任务成本单独列出，不被均摊。
    """
    n = len(metrics)
    if n == 0:
        return {"runs": 0}

    successful = [m for m in metrics if m.outcome not in ("error", "unknown")]
    failed = [m for m in metrics if m.outcome == "error"]
    n_succ = len(successful)
    n_fail = len(failed)

    def sum_tokens(attr: str) -> int:
        return int(sum(getattr(m.tokens, attr) for m in metrics))

    def sum_tokens_succ(attr: str) -> int:
        return int(sum(getattr(m.tokens, attr) for m in successful))

    total_tokens = sum_tokens("total") or (sum_tokens("input") + sum_tokens("output"))

    durations = [m.duration_ms for m in metrics if m.duration_ms is not None]

    result: Dict[str, Any] = {
        "runs": n,
        "successful_runs": n_succ,
        "failed_runs": n_fail,
        "success_rate": round(n_succ / n, 4) if n else 0.0,
        # token 总量（分类）
        "tokens": {
            "input": sum_tokens("input"),
            "output": sum_tokens("output"),
            "cache_read": sum_tokens("cache_read"),
            "cache_write": sum_tokens("cache_write"),
            "reasoning": sum_tokens("reasoning"),
            "total": total_tokens,
        },
        # 核心差异指标：每成功任务 Token（失败成本不混入）
        "tokens_per_successful_run": {
            "input": round(sum_tokens_succ("input") / n_succ, 2) if n_succ else None,
            "output": round(sum_tokens_succ("output") / n_succ, 2) if n_succ else None,
            "total": round((sum_tokens_succ("total") or (sum_tokens_succ("input") + sum_tokens_succ("output"))) / n_succ, 2) if n_succ else None,
        },
        # 耗时（毫秒）
        "duration_ms": {
            "avg": round(sum(durations) / len(durations), 2) if durations else None,
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "p99": _percentile(durations, 0.99),
        },
        "ttft_ms_avg": round(sum(m.ttft_ms for m in metrics if m.ttft_ms is not None) / max(1, len([m for m in metrics if m.ttft_ms is not None])), 2) if any(m.ttft_ms is not None for m in metrics) else None,
        # 成本
        "total_cost_usd": round(sum(m.cost_usd for m in metrics if m.cost_usd is not None), 6) if any(m.cost_usd is not None for m in metrics) else None,
        # 调用统计
        "model_call_count_total": sum(m.model_call_count for m in metrics),
        "model_call_count_avg": round(sum(m.model_call_count for m in metrics) / n, 2) if n else 0,
        "tool_loop_count_total": sum(m.tool_loop_count for m in metrics),
        "failover_count_total": sum(m.failover_count for m in metrics),
    }
    return result


def _percentile(sorted_values: List[float], p: float) -> Optional[float]:
    if not sorted_values:
        return None
    s = sorted(sorted_values)
    idx = (len(s) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 2)
