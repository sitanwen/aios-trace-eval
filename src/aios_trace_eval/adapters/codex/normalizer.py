"""Codex CLI 平台归一化适配器。

处理 Codex 原生 [otel] trace 的特有形态：
- 一次 codex exec 导出几十条 trace，只有含 ``run_turn`` 根 span 的是任务主链路，
  其余按非任务 trace 丢弃；
- 模型调用以 ``stream_request`` 计数（传输层子 span 不重复计）；
- 工具调用以 ``codex.tool.phase=dispatch`` 计数（unified_exec 子 span 不重复计）；
- 原生 span 不携带 usage，token 由 Runner 从 ``codex exec --json`` 的
  ``turn.completed`` 事件解析后经 ``parse_execution_usage`` 回填（见
  experiment_runner）；仅观测模式无法采集 token。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...canonical import CanonicalRun, CanonicalSpan, CanonicalTokens
from .. import _spanutil as su
from ..base import AdapterCapabilities, AgentAdapter
from . import fields as F


def _jsonl_events(stdout: str) -> List[Dict[str, Any]]:
    """解析 codex exec --json 的 JSONL 事件流，忽略非 JSON 行。"""
    events: List[Dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


class CodexAdapter(AgentAdapter):
    """Codex CLI 适配器。"""

    name = "codex"
    capabilities = AdapterCapabilities(
        execute=True,
        process_metrics=True,
        session_context=True,
        execution_modes=("local_cli", "observe_only"),
    )

    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        service = str(resource.get("service.name") or "").lower()
        if service in F.SERVICE_NAMES:
            return 0.95
        for span in spans:
            if str(span.get("name") or "") == F.SPAN_RUN_TURN:
                return 0.85
        return 0.0

    def is_run_trace(self, spans: List[Dict[str, Any]]) -> bool:
        # thread/start、session_loop、transport_worker、codex.exec 等诊断 trace
        # 没有 run_turn 根 span，全部丢弃。
        return any(str(span.get("name") or "") == F.SPAN_RUN_TURN for span in spans)

    def normalize_run(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
        trace_id: str,
    ) -> CanonicalRun:
        tokens = CanonicalTokens()
        model_calls = tool_calls = tool_errors = 0
        runtime_status = "completed"
        model = provider = conversation_id = turn_id = ""
        canonical_spans: List[CanonicalSpan] = []

        span_by_id = {str(item.get("span_id", "")): item for item in spans}

        def is_tool_span(span: Dict[str, Any]) -> bool:
            """dispatch span 及其执行子 span 都属于工具环节。"""
            current: Optional[Dict[str, Any]] = span
            seen = set()
            while current is not None:
                span_id = str(current.get("span_id", ""))
                if span_id in seen:
                    break
                seen.add(span_id)
                if su.attrs(current).get(F.ATTR_TOOL_PHASE) == F.TOOL_PHASE_DISPATCH:
                    return True
                current = span_by_id.get(str(current.get("parent_span_id", "")))
            return False

        for span in spans:
            a = su.attrs(span)
            name = str(span.get("name", ""))
            canonical_spans.append(
                CanonicalSpan(
                    span_id=str(span.get("span_id", "")),
                    parent_span_id=str(span.get("parent_span_id", "")),
                    name=name,
                    kind=str(span.get("kind", "")),
                    started_at_ns=int(span.get("started_at_ns", 0) or 0),
                    ended_at_ns=int(span.get("ended_at_ns", 0) or 0),
                    duration_ms=float(span.get("duration_ms", 0.0) or 0.0),
                    status=str(span.get("status", "unset")),
                    attributes=a,
                )
            )

            # 模型调用：每个 stream_request 一次；传输层子 span（如
            # responses_websocket.stream_request）是它的子节点，不重复计数。
            if name == F.SPAN_MODEL_REQUEST:
                model_calls += 1

            # 工具调用：只计 dispatch；unified_exec.exec_command 是执行子 span。
            is_dispatch_tool = a.get(F.ATTR_TOOL_PHASE) == F.TOOL_PHASE_DISPATCH
            tool_span = is_tool_span(span)
            if is_dispatch_tool:
                tool_calls += 1
                if span.get("status") == "error":
                    tool_errors += 1

            # 工具失败不等于运行失败：否则 Runner 会因 runtime_status!=completed
            # 找不到这条 Trace，把可评分的运行误判为 telemetry_missing。
            if span.get("status") == "error" and not tool_span:
                runtime_status = "error"

            if name == F.SPAN_RUN_TURN:
                conversation_id = su.text(a, F.ATTR_CONVERSATION_ID)
                turn_id = su.text(a, F.ATTR_TURN_ID)
            model = model or su.text(a, F.ATTR_MODEL)
            provider = provider or su.text(a, F.ATTR_PROVIDER)

        # usage 不在 span 上：Local CLI 由 Runner 补全，此处保持 0。
        tokens.total = 0

        started = min(
            (s.started_at_ns for s in canonical_spans if s.started_at_ns > 0),
            default=0,
        )
        ended = max(
            (s.ended_at_ns for s in canonical_spans if s.ended_at_ns > 0),
            default=0,
        )
        return CanonicalRun(
            trace_id=trace_id,
            service_name=str(resource.get("service.name") or ""),
            run_id=str(resource.get("aios.run.id") or "") or conversation_id or trace_id,
            case_id=resource.get("aios.case.id"),
            experiment_id=resource.get("aios.experiment.id"),
            version=resource.get("aios.agent.version"),
            agent="codex",
            provider=provider or None,
            model=model or None,
            started_at_ns=started,
            ended_at_ns=ended,
            duration_ms=round((ended - started) / 1_000_000, 3) if started and ended else 0.0,
            runtime_status=runtime_status,
            tokens=tokens,
            model_calls=model_calls,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            ttft_ms=None,
            spans=canonical_spans,
        )

    def build_command(
        self,
        case: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[str]:
        # 非交互评测：codex exec；--json 供 parse_output / parse_execution_usage 消费。
        command = [str(context.get("executable") or "codex"), "exec", "--skip-git-repo-check", "--json"]
        endpoint = str(context.get("otlp_endpoint") or "").rstrip("/")
        if endpoint:
            # TOML 单引号字面量：双引号会被 Windows .cmd 包装层剥离。
            command.extend([
                "-c",
                f"otel.trace_exporter={{otlp-http={{endpoint='{endpoint}/v1/traces',protocol='binary'}}}}",
            ])
        profile = str(context.get("profile") or "")
        if profile:
            command.extend(["--profile", profile])
        model = context.get("model")
        if model:
            command.extend(["-m", str(model)])
        extra = context.get("extra_args")
        if extra:
            command.extend([str(x) for x in extra])
        command.append(str(case["input"]))
        return command

    def parse_output(self, stdout: str, stderr: str = "") -> str:
        """提取最后一条 agent_message 事件作为 Agent 最终回答。"""
        answer = ""
        for event in _jsonl_events(stdout):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                answer = str(item["text"])
        return answer or stdout.strip()

    def parse_execution_usage(self, stdout: str) -> Optional[Dict[str, int]]:
        """从 turn.completed 事件提取 token 用量（原生 trace 不含 usage）。"""
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        }
        found = False
        for event in _jsonl_events(stdout):
            if event.get("type") != "turn.completed":
                continue
            info = event.get("usage") or {}
            usage["input_tokens"] += int(info.get("input_tokens") or 0)
            usage["output_tokens"] += int(info.get("output_tokens") or 0)
            usage["cache_read_tokens"] += int(info.get("cached_input_tokens") or 0)
            usage["cache_write_tokens"] += int(info.get("cache_write_input_tokens") or 0)
            usage["reasoning_tokens"] += int(info.get("reasoning_output_tokens") or 0)
            found = True
        return usage if found else None
