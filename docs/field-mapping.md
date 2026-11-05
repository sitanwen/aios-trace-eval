# OpenClaw OTel 字段 → 评测指标 映射

本文档把 OpenClaw 2.0（`v2026.8.x`）`diagnostics-otel` 插件导出的字段，映射到评测框架的指标。
字段名均来自官方源码（`extensions/diagnostics-otel/src/`），改动 OpenClaw 版本前请回查校准。

## 1. Span 名称

| Span 名 | 含义 | 评测用途 |
|---|---|---|
| `openclaw.run` | 一次 agent run 边界 | 任务总耗时 |
| `openclaw.harness.run` | harness 生命周期 | 任务级耗时、结果分类 |
| `openclaw.model.call` | 单次模型调用 | token 消耗、模型耗时、TTFB |
| `openclaw.model.usage` | run 级用量 accounting | 总 token / 成本兜底 |
| `openclaw.context.assembled` | 上下文组装 | token 预算、prompt/历史字符数 |
| `openclaw.message.processed` | 消息处理结果 | outcome（成功/失败） |
| `openclaw.model.failover` | 模型故障转移 | 稳定性归因 |
| `openclaw.tool.loop` | 重复工具调用循环 | 过程层「循环检测」 |

## 2. Token 属性（span attribute）

有两套命名并存，评测框架都读，做兜底：

| 分类 | `openclaw.*` 命名 | `gen_ai.*` 标准语义 | model.call 命名 |
|---|---|---|---|
| input | `openclaw.tokens.input` | `gen_ai.usage.input_tokens` | `openclaw.model_call.usage.input_tokens` |
| output | `openclaw.tokens.output` | `gen_ai.usage.output_tokens` | `openclaw.model_call.usage.output_tokens` |
| cache_read | `openclaw.tokens.cache_read` | `gen_ai.usage.cache_read.input_tokens` | `openclaw.model_call.usage.cache_read_input_tokens` |
| cache_write | `openclaw.tokens.cache_write` | `gen_ai.usage.cache_creation.input_tokens` | `openclaw.model_call.usage.cache_creation_input_tokens` |
| reasoning | — | — | `openclaw.model_call.usage.reasoning_output_tokens` |
| total | `openclaw.tokens.total` | — | `openclaw.model_call.usage.total_tokens` |

## 3. 耗时 / 成本 / 上下文属性

| 属性 | 含义 |
|---|---|
| `openclaw.model_call.time_to_first_byte_ms` | 模型调用 TTFT |
| `openclaw.model_call.request_bytes` / `response_bytes` | 请求/响应字节 |
| `openclaw.context.token_budget` | 上下文 token 预算 |
| `openclaw.context.message_count` | 上下文消息数 |
| `openclaw.outcome` | 处理结果（error/completed/...） |
| `error.type` / `openclaw.errorCategory` | 错误分类 |

## 4. Metric 名称（前缀 `openclaw.`）

| Metric | 类型 | 说明 |
|---|---|---|
| `tokens` | counter | 按 `openclaw.token` 标签分 input/output/cache_read/cache_write/total |
| `cost.usd` | counter | 估算成本（美元） |
| `run.duration_ms` | histogram | run 耗时 |
| `harness.duration_ms` | histogram | harness 耗时 |
| `model_call.duration_ms` | histogram | 模型调用耗时 |
| `model_call.time_to_first_byte_ms` | histogram | TTFT |
| `tool.loop` | counter | 工具循环检测 |
| `tool.execution.duration_ms` | histogram | 工具执行耗时 |
| `context.tokens` | histogram | 上下文窗口 limit/used |
| `memory.rss_bytes` / `memory.heap_used_bytes` | histogram | **内存指标（业界空白项，OpenClaw 已自采）** |
| `gen_ai.client.token.usage` | histogram | GenAI token 用量（按 `gen_ai.token.type`） |
| `gen_ai.client.operation.duration` | histogram | GenAI 操作耗时 |

## 5. 身份属性

| 属性 | 含义 |
|---|---|
| `openclaw.provider` | 模型 provider |
| `openclaw.model` | 模型名 |
| `openclaw.agent` | agent 标识 |
| `openclaw.channel` | 渠道 |
| `gen_ai.system` / `gen_ai.provider.name` | provider（旧/新 GenAI 语义） |
| `gen_ai.request.model` | 模型名（GenAI 语义） |
| `gen_ai.operation.name` | chat / invoke_agent / generate_content / text_completion |

## 6. 评测指标如何从 Trace 计算

| 评测指标 | 计算方式 |
|---|---|
| 任务耗时 | `openclaw.run` 或 `openclaw.harness.run` span 时长 |
| 总 Token | Σ 所有 `openclaw.model.call` 的 usage（input+output+cache+reasoning） |
| 每成功任务 Token | ΣToken / outcome 非 error 的 run 数（失败成本单列） |
| TTFT | 首次 `openclaw.model.call` 的 `time_to_first_byte_ms` |
| 成本 | Σ `cost.usd`（或 `openclaw.model.usage` span 的 cost 属性） |
| 工具循环 | `tool.loop` counter |
| 内存 | `memory.rss_bytes` / `memory.heap_used_bytes` 直方图 |
