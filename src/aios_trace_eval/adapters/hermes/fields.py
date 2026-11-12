"""Hermes Agent（Nous Research）遥测字段常量。

数据来源：hermes-plugin-otel-tracing。该插件通过 Hermes 的 8 个生命周期
hook 把一次会话转成 OTel span，经 OTLP/HTTP 导出：

    hermes.session (AGENT)
    └── hermes.turn (CHAIN)
        ├── chat <model> (LLM)
        └── hermes.tool.<name> (TOOL)

Token 口径（Hermes 的 canonical usage）：
1. input_tokens 是未命中缓存的输入，cache_read/cache_write 独立存储；
2. reasoning 已包含在 output 中，不重复累加；
3. total = input + output + cache_read + cache_write。
"""
from __future__ import annotations

SPAN_SESSION = "hermes.session"
SPAN_TURN = "hermes.turn"
SPAN_TOOL_PREFIX = "hermes.tool."
SPAN_LLM_PREFIX = "chat "

ATTR_SPAN_KIND = "hermes.span.kind"
KIND_LLM = "LLM"
KIND_TOOL = "TOOL"

ATTR_SESSION_ID = "hermes.session.id"
ATTR_TOOL_NAME = "hermes.tool.name"
ATTR_PROVIDER = "hermes.provider"
ATTR_CACHE_READ = "hermes.cache.read_tokens"
ATTR_CACHE_WRITE = "hermes.cache.write_tokens"
ATTR_REASONING = "hermes.reasoning_tokens"

ATTR_OPERATION = "gen_ai.operation.name"
OP_CHAT = "chat"
ATTR_SYSTEM = "gen_ai.system"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_INPUT = "gen_ai.usage.input_tokens"
ATTR_OUTPUT = "gen_ai.usage.output_tokens"
ATTR_CACHE_READ_STANDARD = "gen_ai.usage.cache_read.input_tokens"
ATTR_CACHE_WRITE_STANDARD = "gen_ai.usage.cache_creation.input_tokens"

DEFAULT_SERVICE_NAME = "hermes-agent"
