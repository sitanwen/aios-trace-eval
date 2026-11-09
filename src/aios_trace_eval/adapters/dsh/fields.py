"""DeepSeek Harness（DSH）遥测字段常量。

数据来源：@loongsuite/dsh-plugin（npm 包 dsh-plugin-loongsuite），该插件在
DSH 进程内把 session、agent-loop、LLM、tool 生命周期事件转为 OTel GenAI
span，经 OTLP/HTTP protobuf 导出。

Span 层级（每个 turn 一条 trace）：
    enter_ai_application_system (ENTRY)
    └── invoke_agent (AGENT，携带整 turn 聚合 usage)
        └── react step (STEP，×N)
            ├── chat <model> (LLM)
            └── execute_tool <tool> (TOOL)
"""
from __future__ import annotations

# Span 名称（span.name 前缀；LLM/TOOL 名含模型或工具后缀，按前缀判定）
SPAN_ENTRY = "enter_ai_application_system"
SPAN_AGENT = "invoke_agent"
SPAN_STEP_PREFIX = "react step"
SPAN_LLM_PREFIX = "chat"
SPAN_TOOL_PREFIX = "execute_tool"

# GenAI 操作名（gen_ai.operation.name）
OP_LLM_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"

# 会话与身份属性
ATTR_SESSION_ID = "gen_ai.session.id"
ATTR_TURN_ID = "gen_ai.turn.id"
ATTR_AGENT_ID = "gen_ai.agent.id"

# 标准 token 用量属性
ATTR_USAGE_INPUT = "gen_ai.usage.input_tokens"
ATTR_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
ATTR_USAGE_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
ATTR_USAGE_CACHE_WRITE = "gen_ai.usage.cache_creation.input_tokens"
ATTR_TTFT = "gen_ai.response.time_to_first_token"

# DSH token 口径（与其他 provider 可能不同，归一化时必须遵守）：
# 1. cache_read 已包含在 input_tokens 内，不是并列项；
# 2. input_tokens + output_tokens == total_tokens，reasoning 已计入 output；
# 3. invoke_agent（AGENT）span 携带整 turn 聚合 usage，与各 chat（LLM）span
#    求和会重复，因此总量只从 LLM span 累加，忽略 AGENT span 的 usage。
