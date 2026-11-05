"""
OpenClaw 2.0 (v2026.8.x) diagnostics-otel 插件导出的字段映射常量。

这些字段名是从 OpenClaw 官方源码逐行确认的（一手来源）：
  extensions/diagnostics-otel/src/service-constants.ts
  extensions/diagnostics-otel/src/service-genai-attributes.ts
  extensions/diagnostics-otel/src/service-recorders-model.ts
  extensions/diagnostics-otel/src/service-recorders-usage.ts
  extensions/diagnostics-otel/src/service-recorders-harness.ts
  extensions/diagnostics-otel/src/service-metrics.ts

评测框架的指标提取器依赖这些字段，改动 OpenClaw 版本前务必回查源码校准。
"""

# ---------------------------------------------------------------------------
# Span 名称（trace 里的 span.name）
# ---------------------------------------------------------------------------
SPAN_MODEL_CALL = "openclaw.model.call"        # 单次模型调用（默认语义，未开 gen_ai_latest）
SPAN_MODEL_USAGE = "openclaw.model.usage"      # run 级用量 accounting span
SPAN_RUN = "openclaw.run"                       # 一次 agent run 的边界
SPAN_HARNESS_RUN = "openclaw.harness.run"       # harness 生命周期（任务级耗时）
SPAN_MESSAGE_PROCESSED = "openclaw.message.processed"  # 消息处理结果（含 outcome）
SPAN_CONTEXT_ASSEMBLED = "openclaw.context.assembled"  # 上下文组装（含 token_budget/字符数）
SPAN_MODEL_FAILOVER = "openclaw.model.failover" # 模型故障转移
SPAN_TOOL_LOOP = "openclaw.tool.loop"           # 工具循环（detected repetitive tool-call loop）

# ---------------------------------------------------------------------------
# Token 相关属性（span 上的 attribute key）
# 有两套并存：openclaw.* 私有命名 + gen_ai.* 标准 GenAI 语义，都读，做兜底。
# ---------------------------------------------------------------------------
TOKEN_ATTRS_OPENCLAW = {
    "input": "openclaw.tokens.input",
    "output": "openclaw.tokens.output",
    "cache_read": "openclaw.tokens.cache_read",
    "cache_write": "openclaw.tokens.cache_write",
    "total": "openclaw.tokens.total",
}

TOKEN_ATTRS_GENAI = {
    "input": "gen_ai.usage.input_tokens",
    "output": "gen_ai.usage.output_tokens",
    "cache_read": "gen_ai.usage.cache_read.input_tokens",
    "cache_write": "gen_ai.usage.cache_creation.input_tokens",
}

# model.call span 上的另一套用量属性（见 service-genai-attributes.ts）
MODEL_CALL_USAGE_ATTRS = {
    "input": "openclaw.model_call.usage.input_tokens",
    "output": "openclaw.model_call.usage.output_tokens",
    "cache_read": "openclaw.model_call.usage.cache_read_input_tokens",
    "cache_write": "openclaw.model_call.usage.cache_creation_input_tokens",
    "reasoning": "openclaw.model_call.usage.reasoning_output_tokens",
    "prompt": "openclaw.model_call.usage.prompt_tokens",
    "total": "openclaw.model_call.usage.total_tokens",
}

# ---------------------------------------------------------------------------
# 身份 / 成本 / 耗时 / 错误属性
# ---------------------------------------------------------------------------
ATTR_PROVIDER = "openclaw.provider"
ATTR_MODEL = "openclaw.model"
ATTR_AGENT = "openclaw.agent"
ATTR_CHANNEL = "openclaw.channel"
ATTR_GENAI_SYSTEM = "gen_ai.system"              # provider（旧语义）
ATTR_GENAI_PROVIDER = "gen_ai.provider.name"     # provider（新语义，gen_ai_latest）
ATTR_GENAI_MODEL = "gen_ai.request.model"        # 模型名
ATTR_GENAI_OPERATION = "gen_ai.operation.name"   # chat / invoke_agent / generate_content ...

# 耗时（模型调用 span 上）
ATTR_TTFB_MS = "openclaw.model_call.time_to_first_byte_ms"
ATTR_REQUEST_BYTES = "openclaw.model_call.request_bytes"
ATTR_RESPONSE_BYTES = "openclaw.model_call.response_bytes"

# 结果与错误
ATTR_OUTCOME = "openclaw.outcome"                # 取值示例：error / completed / ...
ATTR_ERROR_TYPE = "error.type"
ATTR_ERROR_CATEGORY = "openclaw.errorCategory"
ATTR_FAILURE_KIND = "openclaw.failureKind"
ATTR_HARNESS_RESULT = "openclaw.harness.result_classification"

# 上下文组装（context.assembled span）
ATTR_CTX_TOKEN_BUDGET = "openclaw.context.token_budget"
ATTR_CTX_MESSAGE_COUNT = "openclaw.context.message_count"
ATTR_CTX_HISTORY_CHARS = "openclaw.context.history_text_chars"
ATTR_CTX_PROMPT_CHARS = "openclaw.context.prompt_chars"
ATTR_CTX_SYSTEM_PROMPT_CHARS = "openclaw.context.system_prompt_chars"

# ---------------------------------------------------------------------------
# Metric 名称（OTLP metrics 里的 name，前缀 openclaw.）
# 评测框架主要读 trace，但 metrics 可用来做跨任务聚合/看板。
# ---------------------------------------------------------------------------
METRIC_TOKENS = "openclaw.tokens"                       # counter，按 openclaw.token=input/output/cache_read/...
METRIC_COST_USD = "openclaw.cost.usd"                   # counter，成本金额
METRIC_RUN_DURATION = "openclaw.run.duration_ms"        # histogram，run 耗时
METRIC_HARNESS_DURATION = "openclaw.harness.duration_ms"  # histogram，harness 耗时
METRIC_MODEL_CALL_DURATION = "openclaw.model_call.duration_ms"
METRIC_TTFB = "openclaw.model_call.time_to_first_byte_ms"
METRIC_TOOL_LOOP = "openclaw.tool.loop"                 # counter，工具循环检测
METRIC_TOOL_EXEC_DURATION = "openclaw.tool.execution.duration_ms"
METRIC_CONTEXT_TOKENS = "openclaw.context.tokens"       # histogram，上下文窗口 limit/used
METRIC_MEMORY_RSS = "openclaw.memory.rss_bytes"         # 内存 RSS（业界空白项，OpenClaw 已自采）
METRIC_MEMORY_HEAP_USED = "openclaw.memory.heap_used_bytes"
METRIC_GENAI_TOKEN_USAGE = "openclaw.gen_ai.client.token.usage"  # histogram，按 gen_ai.token.type
METRIC_GENAI_OP_DURATION = "openclaw.gen_ai.client.operation.duration"

# token 类型的 metric 标签值
TOKEN_TYPE_LABEL = "openclaw.token"
TOKEN_TYPE_INPUT = "input"
TOKEN_TYPE_OUTPUT = "output"
TOKEN_TYPE_CACHE_READ = "cache_read"
TOKEN_TYPE_CACHE_WRITE = "cache_write"
TOKEN_TYPE_TOTAL = "total"

# ---------------------------------------------------------------------------
# 运行入口说明（评测 Runner 必须注意）
# ---------------------------------------------------------------------------
# 1) `openclaw agent exec` 不上报遥测 —— 评测必须走 `openclaw agent --local` 或经 Gateway。
# 2) span 默认不带 session key / gen_ai.conversation.id，多轮/长程任务需自行用 W3C
#    traceparent 关联，否则无法把多次 run 归到同一会话。
# 3) captureContent 默认 false，不发 prompt / 工具输入输出原文；评测“工具参数正确率”
#    需开启 captureContent: true（隐私代价见 config/openclaw-otel.json5）。
