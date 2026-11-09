# Agent 接入指南

平台通过 **OTLP/HTTP** 接收各 Agent 的 trace，并**自动识别平台**（Claude Code / DeepSeek Harness / OpenClaw / 通用 GenAI），统一归一化后入库、评测、展示。因此「接入」本质上是：

> 让你的 Agent 把 OTLP trace 报到平台的 `/v1/traces` 地址。

平台地址默认 `http://127.0.0.1:8080`（下面统一写作 `<AIOS_HOST>`，部署后换成实际地址，例如 `https://eval.example.com`）。OTLP 协议固定为 `http/protobuf`。

---

## 0. 通用：三种接入能力（务必先看）

| 能力 | 现在支持 |
|---|---|
| **观测**：agent 推 trace → 前端「运行记录」看到归一化结果 | ✅ 全部平台 |
| **评测评分**：对入库 trace 跑规则/Judge 评估 | ✅ 全部平台 |
| **一键跑实验**（平台主动调用 agent 逐个执行 case） | ✅ OpenClaw Gateway、Claude Code/DSH Local CLI |

Claude Code / DSH 使用 Local CLI 模式时，平台直接启动 CLI，并采集完整进程树的 CPU 与 RSS。Trace 优先精确关联，无法携带关联属性时按同平台新增 Trace 与执行时间窗回退。Remote Worker 尚未开放；其它机器目前使用“仅观测”模式。

---

## 1. Claude Code

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=<AIOS_HOST>      # 不要带 /v1/traces
export OTEL_SERVICE_NAME=claude-code

claude --print "<任务>"
```

> 对应样例：`config/claude-code-otel.env`。

## 2. DeepSeek Harness（DSH）

```bash
dsh plugin --profile headless add @loongsuite/dsh-plugin   # 安装 dsh 采集插件
export OTEL_SERVICE_NAME=dsh-agent
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=<AIOS_HOST>

dsh --profile headless "<任务>"
```

> 对应样例：`config/dsh-otel.env`。

## 3. OpenClaw

在 OpenClaw 配置里把 OTLP endpoint 指向平台：

```json5
// ~/.openclaw/openclaw.json
{
  "diagnostics": {
    "enabled": true,
    "otel": {
      "endpoint": "<AIOS_HOST>",
      "protocol": "http/protobuf",
      "traces": true,
      "metrics": true
    }
  }
}
```

执行：既可在平台「评测实验」里一键运行，也可直接 `openclaw agent "<任务>"`。
> 对应样例：`config/openclaw-otel.json5`。

## 4. 通用 GenAI（OpenAI 兼容，如 opentelemetry-instrumentation-openai 埋点）

```bash
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=<AIOS_HOST>
```

应用侧用 `opentelemetry-instrumentation-openai`（或同类 GenAI 埋点）产出标准 `gen_ai.*` span，平台即可自动识别归一化，无需额外适配器。

---

## 5. 如何确认接入成功

去前端「运行记录」页，应看到一条对应平台的 trace：

- **Claude Code**：`model=claude-sonnet-4`（或你的默认模型）、`service=claude-code`，`total_tokens > 0`；
- **DSH**：`service=dsh-agent`，`total_tokens > 0`；
- **OpenClaw**：`service` 为你的 openclaw serviceName；
- **通用 GenAI**：`model/provider` 来自 `gen_ai.*` 字段。

如果 trace 出现但 `total_tokens = 0`，通常是该平台上报的 token 字段名和适配器假设不一致，把原始 span 属性发出来即可定位。

---

## 6. 平台如何识别（排查用）

| 平台 | 识别特征 | token 口径 |
|---|---|---|
| Claude Code | span 名 `claude_code.*` | `input/output/cache_read/cache_creation_tokens`，total = input+output+cache_read+cache_write |
| DSH | span 名 `chat / execute_tool / invoke_agent` + `gen_ai.*` | total = input + output（cache_read 是 input 子集）|
| OpenClaw | span 名 `openclaw.*` | total = input+output+cache_read+cache_write+reasoning |
| 通用 GenAI | 标准 `gen_ai.*` | total = 各分类之和 |
