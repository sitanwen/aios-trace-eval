"""Codex CLI 遥测字段常量。

数据来源：Codex CLI 原生 [otel] 导出（config.toml 的 ``otel.trace_exporter``，
``protocol="binary"`` 即 http/protobuf）。0.167.0 实测校准；Codex 官方说明 span
名会随版本变化，升级后需重新校准。

Codex 一次 ``codex exec`` 会导出几十条 trace，只有任务主链路是有效 run trace：

    run_turn（TRACE 根，携带 conversation.id / turn.id）
    ├── run_sampling_request → try_run_sampling_request
    │   └── stream_request（每次模型请求一个，不带 usage 属性）
    │       └── model_client.stream_<transport>
    │           └── <transport>.stream_request
    ├── exec_command（工具 dispatch，携带 codex.tool.phase=dispatch）
    │   └── unified_exec.exec_command（实际执行子 span，不重复计数）
    └── run_turn_stop_hooks

其余 thread/start、session_loop、transport_worker、codex.exec、fs.* 等诊断 trace
一律按非任务 trace 丢弃。
"""
from __future__ import annotations

# 任务主 trace 的根 span；同时也是 run trace 的判定特征
SPAN_RUN_TURN = "run_turn"

# 每次模型请求一个；传输层子 span（responses_websocket.stream_request 等）是它的子节点
SPAN_MODEL_REQUEST = "stream_request"

# 工具调用标记：codex.tool.phase=dispatch 的 span 计一次工具调用
ATTR_TOOL_PHASE = "codex.tool.phase"
TOOL_PHASE_DISPATCH = "dispatch"

# 会话与身份属性（挂在 run_turn 根 span 上）
ATTR_CONVERSATION_ID = "conversation.id"
ATTR_TURN_ID = "turn.id"

# 模型 / 提供商属性（挂在 get_model_info、built_tools、model_client.* 等 span 上）
ATTR_MODEL = "model"
ATTR_PROVIDER = "provider"

# headless（codex exec）运行的默认 service.name
SERVICE_NAME_EXEC = "codex_exec"
SERVICE_NAMES = {"codex", "codex_exec"}

# Token 口径（codex exec --json 的 turn.completed.usage；原生 span 不含 usage）：
# 1. usage 不在任何 trace span 上，Local CLI 模式由 Runner 从执行结果补全，
#    仅观测模式无法采集 token；
# 2. cached_input_tokens 是 input_tokens 的子集，与 DSH 相同不并列累计；
# 3. reasoning_output_tokens 已计入 output_tokens；
# 4. total = input + output。

# 命令注入的 OTLP 配置使用 TOML 单引号字面量：双引号会被 Windows 的 .cmd
# 包装层剥离，导致 Codex 收到非法 TOML。
