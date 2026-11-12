"""Hermes Agent（Nous Research）归一化适配器。

识别 hermes.session / hermes.turn / chat <model> / hermes.tool.<name> 层级。
Hermes 的 canonical usage 中 input_tokens 不含缓存，cache_read/cache_write
独立存储，reasoning 已计入 output，因此：

    total = input + output + cache_read + cache_write

插件（hermes-plugin-otel-tracing）会读取标准 OTEL_SERVICE_NAME 与
OTEL_RESOURCE_ATTRIBUTES，因此 Local CLI 模式下可以拿到 aios.run.id 做精确关联。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from ...canonical import CanonicalRun, CanonicalSpan, CanonicalTokens
from .. import _spanutil as su
from ..base import AdapterCapabilities, AgentAdapter
from . import fields as F

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class HermesAdapter(AgentAdapter):
    """Hermes Agent 适配器。"""

    name = "hermes"
    capabilities = AdapterCapabilities(
        execute=True,
        process_metrics=True,
        execution_modes=("local_cli", "observe_only"),
    )

    @classmethod
    def detect(cls, spans: List[Dict[str, Any]], resource: Dict[str, Any]) -> float:
        for span in spans:
            name = str(span.get("name") or "")
            a = su.attrs(span)
            if name in {F.SPAN_SESSION, F.SPAN_TURN} or name.startswith(F.SPAN_TOOL_PREFIX):
                return 0.98
            if a.get(F.ATTR_SPAN_KIND) in {F.KIND_LLM, F.KIND_TOOL}:
                return 0.9
            if name.startswith(F.SPAN_LLM_PREFIX) and a.get(F.ATTR_SYSTEM) == F.DEFAULT_SERVICE_NAME:
                return 0.9
        return 0.0

    def normalize_run(
        self,
        spans: List[Dict[str, Any]],
        resource: Dict[str, Any],
        trace_id: str,
    ) -> CanonicalRun:
        tokens = CanonicalTokens()
        model_calls = tool_calls = tool_errors = 0
        runtime_status = "completed"
        model = provider = agent = session_id = ""
        canonical_spans: List[CanonicalSpan] = []

        for span in spans:
            a = su.attrs(span)
            name = str(span.get("name", ""))
            kind = str(a.get(F.ATTR_SPAN_KIND, ""))
            operation = str(a.get(F.ATTR_OPERATION, ""))
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

            is_llm = kind == F.KIND_LLM or operation == F.OP_CHAT or name.startswith(F.SPAN_LLM_PREFIX)
            is_tool = kind == F.KIND_TOOL or name.startswith(F.SPAN_TOOL_PREFIX)

            if is_llm:
                model_calls += 1
                # input 是未命中缓存的输入；cache 与 reasoning 口径见 fields.py
                tokens.input += su.integer(a, F.ATTR_INPUT)
                tokens.output += su.integer(a, F.ATTR_OUTPUT)
                tokens.cache_read += su.integer(a, F.ATTR_CACHE_READ, F.ATTR_CACHE_READ_STANDARD)
                tokens.cache_write += su.integer(a, F.ATTR_CACHE_WRITE, F.ATTR_CACHE_WRITE_STANDARD)
                tokens.reasoning += su.integer(a, F.ATTR_REASONING)

            if is_tool:
                tool_calls += 1
                if span.get("status") == "error":
                    tool_errors += 1

            # 工具失败不等于运行失败：平台单独统计 tool_errors，run 仍可完成。
            if span.get("status") == "error" and not is_tool:
                runtime_status = "error"

            model = model or su.text(
                a, F.ATTR_REQUEST_MODEL, F.ATTR_RESPONSE_MODEL
            )
            if not model and name.startswith(F.SPAN_LLM_PREFIX):
                model = name[len(F.SPAN_LLM_PREFIX):].strip()
            provider = provider or su.text(a, F.ATTR_PROVIDER)
            if not provider:
                system = su.text(a, F.ATTR_SYSTEM)
                provider = "" if system == F.DEFAULT_SERVICE_NAME else system
            agent = agent or su.text(a, "gen_ai.agent.name", "hermes.agent.name")
            session_id = session_id or su.text(a, F.ATTR_SESSION_ID)

        # reasoning 已包含在 output 中，不再单独加到 total
        tokens.total = tokens.input + tokens.output + tokens.cache_read + tokens.cache_write

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
            service_name=str(resource.get("service.name") or F.DEFAULT_SERVICE_NAME),
            run_id=str(resource.get("aios.run.id") or "") or session_id or trace_id,
            case_id=resource.get("aios.case.id"),
            experiment_id=resource.get("aios.experiment.id"),
            version=resource.get("aios.agent.version"),
            agent=agent or "hermes",
            provider=provider or None,
            model=model or None,
            started_at_ns=started,
            ended_at_ns=ended,
            duration_ms=round((ended - started) / 1_000_000, 3) if ended else 0.0,
            runtime_status=runtime_status,
            tokens=tokens,
            model_calls=model_calls,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            spans=canonical_spans,
        )

    def build_command(
        self,
        case: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[str]:
        command = [str(context.get("executable") or "hermes")]
        # Runner 给非 OpenClaw 适配器的默认 profile 是 headless；Hermes 没有
        # 同名 profile，只有用户显式配置时才加 --profile。
        profile = str(context.get("profile") or "").strip()
        if profile and profile != "headless":
            command.extend(["--profile", profile])
        # 0.19 与 0.22 都支持 -Q/-q；--oneshot/--format 是较新版本才有，
        # 不用它们以保持兼容。非 TTY 下 -q 回答后自动退出。
        command.extend(["chat", "-Q"])
        model = context.get("model")
        if model:
            command.extend(["--model", str(model)])
        extra = context.get("extra_args")
        if extra:
            command.extend([str(x) for x in extra])
        command.extend(["-q", str(case["input"])])
        return command

    def parse_output(self, stdout: str, stderr: str = "") -> str:
        """提取最终回答，过滤 -Q 仍会输出的告警与会话信息。"""
        cleaned = _ANSI.sub("", stdout)
        # 如果用户通过 extra_args 启用了 stream-json，优先取 result 事件
        result_text = ""
        for line in cleaned.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                value = event.get("text")
                if isinstance(value, str):
                    result_text = value
        if result_text:
            return result_text.strip()

        lines = []
        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("session_id:", "Session:")):
                continue
            if "tirith security scanner" in stripped or re.match(r"^\s{2,}⚠", line):
                continue
            lines.append(line.rstrip())
        return "\n".join(lines).strip()
