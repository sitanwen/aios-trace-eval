# 多平台适配器架构

## 1. 设计目标

平台在接收端保持平台无关，通过适配器层接入不同 Agent 运行时。核心评测、
存储、聚合与前端只依赖统一规范模型，不识别任何厂商私有字段。新增平台时
仅需新增一个适配器目录，不改动核心流程。

## 2. 数据流

```text
Agent 运行时（DSH / Claude Code / OpenClaw ...）
  内置 OTel 仪表，经 OTLP/HTTP 上报
        │
        ▼
/v1/traces 接收 + protobuf 解码（平台无关，otlp.py）
        │
        ▼
适配器注册表自动探测（adapters/registry.py）
        │
        ▼
平台适配器 normalize_run：私有 span/属性 → CanonicalRun
        │
        ▼
核心评测 / 存储 / 聚合 / 前端（只依赖 CanonicalRun）
```

OTLP 接收与 protobuf 解码对所有平台一致。平台差异仅存在于适配器内部，
集中在三个维度：

- 归一化：私有 span 名与属性 → 统一模型；
- 任务执行：构造该平台的非交互启动命令；
- 资源采样：Local CLI 由 Runner 按根 PID 与全部子进程采样；Gateway 使用服务进程指标。

## 3. 统一规范模型

`canonical.py` 定义 `CanonicalRun` / `CanonicalSpan` / `CanonicalTokens`，
字段与现有 storage 的 trace 汇总口径对齐。`to_trace_summary()` 输出存储层
所需的扁平 dict。

## 4. 适配器契约

`adapters/base.py` 的 `AgentAdapter`：

- `detect(spans, resource)`：返回 0–1 匹配置信度，用于自动探测；
- `is_run_trace(spans)`：排除自诊断、独立 accounting 等非任务 trace；
- `normalize_run(spans, resource, trace_id)`：归一化为 CanonicalRun；
- `build_execution(case, context)`：构造命令、工作目录与输出约定；
- `extract_process_samples(spans, resource)`：进程资源采样（可选）。

Trace 关联按执行模型区分：

- OpenClaw Gateway 不继承启动 CLI 时的资源属性，按执行前后的新增 Trace、时间窗和完成状态关联；
- Claude Code / DSH Local CLI 是 Runner 的直接子进程，优先使用继承的关联属性精确匹配；
- 如果 CLI 插件没有导出关联属性，降级为同 `service.name` 的新增 Trace，并按模型调用、Token 与跨度选择主 Trace。

`run_id` 只作为平台内部 Attempt 标识。Gateway 关联不依赖它，资源曲线也由
Attempt 与最终 Trace 的显式关系完成关联。

## 5. 已支持平台

| 适配器 | 运行时 | 采集方式 | 非交互命令 |
|---|---|---|---|
| `generic` | 任意标准 GenAI 输出 | 标准 `gen_ai.*` | 由具体平台决定 |
| `dsh` | DeepSeek Harness | 插件 Trace + Runner 进程树采样 | `dsh --profile headless` |
| `claude_code` | Claude Code | 内置 OTel Trace + Runner 进程树采样 | `claude --print` |
| `openclaw` | OpenClaw | Gateway OTel | Gateway runner |

## 6. Token 口径差异（归一化必须处理）

两个平台对缓存与聚合的处理不同，不能共用同一求和规则。

**DSH**

- `cache_read` 已包含在 `input_tokens` 内，不是并列项；
- `input + output == total`，reasoning 已计入 output；
- `invoke_agent` span 携带整 turn 聚合 usage，与各 `chat` span 求和会重复，
  因此 token 只从 `chat`（LLM）span 累加，`total = input + output`。

**Claude Code（Anthropic 口径）**

- `cache_read` / `cache_write` 独立于 `input`；
- `total = input + output + cache_read + cache_write`；
- 工具仅以 `claude_code.tool` 计数，子 span 不重复统计。

## 7. 运行模式

- `gateway`：OpenClaw 等持久服务；资源数据代表共享 Gateway 进程。
- `local_cli`：Claude Code / DSH 的推荐评测方式；平台启动进程、采集进程树并取得最终输出。
- `observe_only`：只接收外部 Trace，不允许平台主动执行数据集。

Remote Worker 尚未作为产品能力开放；在协议、鉴权和心跳完成前不在前端展示。
