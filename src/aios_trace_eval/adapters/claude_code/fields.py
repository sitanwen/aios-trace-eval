"""Claude Code 遥测字段常量。

数据来源：Claude Code 官方文档 Monitoring / Agent SDK Observability。Claude
Code 内置 OTel 仪表，通过环境变量启用，可导出 traces、metrics、log events
到任意 OTLP 后端。

启用开关：
    CLAUDE_CODE_ENABLE_TELEMETRY=1          # 遥测总开关
    CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1   # 分布式 trace（beta）
    OTEL_TRACES_EXPORTER=otlp
    OTEL_METRICS_EXPORTER=otlp
    OTEL_LOGS_EXPORTER=otlp

Span 层级（单个 turn）：
    claude_code.interaction
    ├── claude_code.llm_request (×N)
    ├── claude_code.tool
    │   ├── claude_code.tool.blocked_on_user
    │   └── claude_code.tool.execution
    └── claude_code.hook
"""
from __future__ import annotations

# Span 名称
SPAN_INTERACTION = "claude_code.interaction"
SPAN_LLM_REQUEST = "claude_code.llm_request"
SPAN_TOOL = "claude_code.tool"
SPAN_TOOL_BLOCKED = "claude_code.tool.blocked_on_user"
SPAN_TOOL_EXECUTION = "claude_code.tool.execution"
SPAN_HOOK = "claude_code.hook"

# 默认 service.name
DEFAULT_SERVICE_NAME = "claude-code"

# 身份属性
ATTR_SESSION_ID = "session.id"

# 标准 token 用量属性（Claude Code 遵循 OTel GenAI 语义）
ATTR_USAGE_INPUT = "gen_ai.usage.input_tokens"
ATTR_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
ATTR_USAGE_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
ATTR_USAGE_CACHE_WRITE = "gen_ai.usage.cache_creation.input_tokens"

# 遥测环境变量开关
ENV_ENABLE_TELEMETRY = "CLAUDE_CODE_ENABLE_TELEMETRY"
ENV_ENHANCED_BETA = "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"
