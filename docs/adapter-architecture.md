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
- 资源采样：平台自报的进程级 CPU/内存（可选）。

## 3. 统一规范模型

`canonical.py` 定义 `CanonicalRun` / `CanonicalSpan` / `CanonicalTokens`，
字段与现有 storage 的 trace 汇总口径对齐。`to_trace_summary()` 输出存储层
所需的扁平 dict。

## 4. 适配器契约

`adapters/base.py` 的 `AgentAdapter`：

- `detect(spans, resource)`：返回 0–1 匹配置信度，用于自动探测；
- `is_run_trace(spans)`：排除自诊断、独立 accounting 等非任务 trace；
- `normalize_run(spans, resource, trace_id)`：归一化为 CanonicalRun；
- `build_command(case, context)`：构造非交互执行命令；
- `extract_process_samples(spans, resource)`：进程资源采样（可选）。

## 5. 已支持平台

| 适配器 | 运行时 | 采集方式 | 非交互命令 |
|---|---|---|---|
| `generic` | 任意标准 GenAI 输出 | 标准 `gen_ai.*` | 由具体平台决定 |
| `dsh` | DeepSeek Harness | `@loongsuite/dsh-plugin`，进程内 OTLP | `dsh --profile headless` |
| `claude_code` | Claude Code | 内置 OTel，环境变量启用 | `claude --print` |
| `openclaw` | OpenClaw | 待迁移（现位于 otlp/extractor） | 现有 runner |

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

## 7. 实施路径

1. 本阶段：规范模型、适配器契约、注册表、generic/dsh/claude_code 适配器，
   纯新增，不改动现有核心，主流程行为不变；
2. 下一阶段：将 OpenClaw 归一化逻辑迁入 `adapters/openclaw`，otlp/extractor
   改为调用注册表，保持自测全绿；
3. 接入实验 Runner：按平台适配器的 `build_command` 替换硬编码命令；
4. 配置样例、文档与 CI 矩阵按平台展开。
