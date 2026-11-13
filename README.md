# AIOS Trace Eval

AIOS Trace Eval 是一个面向多种 AI Agent 的可观测与评测平台。它通过 OTLP/HTTP 接收 Agent 运行数据，自动识别 OpenClaw、Claude Code、DeepSeek Harness、Hermes、Codex CLI 和标准 GenAI 运行时，将 Trace、模型调用、Token、耗时、工具调用和评测结果关联到同一次 Run，并提供数据集、评估器、实验批次与逐样本结果管理。

当前版本适合本地开发、单机实验和小规模团队验证。生产部署前请先阅读“当前边界”。

## 支持的 Agent 平台

| 平台 | 采集方式 | 执行方式 | 状态 |
|---|---|---|---|
| OpenClaw | `diagnostics-otel` 插件 | Gateway / 本地 CLI | 已按 `2026.8.2` 回归 |
| Claude Code | 内置 OTel（Enhanced Telemetry Beta） | 本地 CLI | 已回归 |
| DeepSeek Harness（DSH） | `@loongsuite/dsh-plugin` | 本地 CLI | 已回归 |
| Hermes Agent | `hermes-plugin-otel-tracing` | 本地 CLI | Hermes `0.19.0` + 插件 `0.1.1` 已回归 |
| Codex CLI | 内置 `[otel]` | 本地 CLI | 适配器按 `0.167.0` 校准，升级后需复核 Span 名 |
| 通用 GenAI | 标准 `gen_ai.*` 埋点 | 仅观测 | 自动兜底 |

平台会自动探测适配器；页面内的「接入手册」提供每个平台的安装、配置与验证步骤，[docs/agent-integration.md](docs/agent-integration.md) 是同一份接入说明的文档版本。

平台核心使用 Python、FastAPI、SQLite 和原生浏览器 JavaScript。OTLP 接收固定为 `http/protobuf`，Runner 同时支持 Linux/macOS 和 Windows 的 CLI 启动。

## 能做什么

- 接收并解析多种 Agent 的 OTLP Trace，自动识别平台
- 接受任意 OTLP/HTTP 上游（原生探针、Pilot、Collector、代理），可用 `X-AIOS-Ingest-Source` 标注来源
- 在 Agent 卡片展示接入能力矩阵：观测、会话上下文、工具调用、可执行、资源采集
- 统计输入、输出、缓存与总 Token；按平台处理不同的缓存与推理口径
- 记录端到端耗时、TTFT、模型调用、工具调用、工具错误与 Failover
- 本地 CLI 模式采集整个进程树的 RSS / CPU；Gateway 模式使用 Gateway 进程指标
- 管理 Agent、评测数据集和 Case
- 组合规则、轨迹规则、LLM Judge、Code 表达式与人工评估器
- 保存每个评估器的得分、理由和证据
- 汇总实验通过率、平均分、总 Token、平均耗时和逐样本结果
- 在 Web 控制台中查看 Trajectory、Raw Spans、绝对时间、工具参数与工具结果
- 支持按运行批次查看全部 Trace 预览，再下钻到单个 Trace 的 Span 明细

## 快速开始

使用 Docker Compose：

```bash
git clone https://github.com/sitanwen/aios-trace-eval.git
cd aios-trace-eval/config
docker compose up --build -d
```

或者直接运行 Python 服务。Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
aios-trace-eval serve --host 0.0.0.0 --port 8080
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
aios-trace-eval serve --host 0.0.0.0 --port 8080
```

Docker Compose 只包含平台自身，适合运行控制台、API 和 OTLP 接收端。页面中的“运行实验”会调用平台进程所在环境里的 Agent CLI；需要使用该功能时，建议在同一台主机按 Python 方式运行平台。

服务入口：

| 路径 | 用途 |
|---|---|
| `/` | Web 控制台 |
| `/api/health` | 健康检查 |
| `/api/adapters` | 适配器能力矩阵 |
| `/api/*` | 平台 API |
| `/v1/traces` | OTLP Trace 接收 |
| `/v1/metrics` | OTLP Metrics 接收（只保留资源曲线所需指标） |
| `/v1/logs` | OTLP Logs 入口（当前接受请求但不解析落库） |

本机调试访问 `http://127.0.0.1:8080`。部署到服务器后，将配置中的 endpoint 换成实际服务地址。

## 接入 Agent

### 通用约定

- OTLP endpoint 写到平台服务地址，**不要**带 `/v1/traces`；Codex 的 `[otel]` 配置例外，它要求完整 `/v1/traces` 地址。
- 只做观测时，让 Agent 把 Trace 推到 `/v1/traces` 即可。
- 要一键跑实验，再到「Agent」页登记平台、执行方式和可执行文件。
- 外部采集器可用请求头标注来源：

```bash
X-AIOS-Ingest-Source: pilot
```

对应的 resource 属性是 `aios.ingest.source=pilot`，请求头优先。运行详情会显示来源。

### OpenClaw

安装与 OpenClaw 匹配的 OTel 插件：

```bash
openclaw plugins install @openclaw/diagnostics-otel@2026.8.2 --accept-capabilities --pin
openclaw plugins list
```

把 [config/openclaw-otel.json5](config/openclaw-otel.json5) 合并到现有 `openclaw.json`：

```json5
{
  plugins: {
    allow: ["diagnostics-otel"],
    entries: { "diagnostics-otel": { enabled: true } },
  },
  diagnostics: {
    enabled: true,
    otel: {
      enabled: true,
      endpoint: "http://127.0.0.1:8080",
      protocol: "http/protobuf",
      serviceName: "openclaw-eval",
      traces: true,
      metrics: true,
      logs: false,
      flushIntervalMs: 1000,
      captureContent: false,
    },
  },
}
```

Runner 默认使用 `openclaw agent --local`；如果已经单独管理 Gateway，可设置 `AIOS_OPENCLAW_MODE=gateway`：

```bash
export AIOS_OPENCLAW_CONFIG_PATH=/etc/openclaw/openclaw.json5
export AIOS_OPENCLAW_STATE_DIR=/var/lib/aios-trace-eval/openclaw-state
export AIOS_OPENCLAW_MODE=local   # gateway 模式时改为 gateway
export OPENCLAW_GATEWAY_PORT=18789

aios-trace-eval serve --host 0.0.0.0 --port 8080
```

`AIOS_OPENCLAW_STATE_DIR` 必须是已存在的独立目录，不同 OpenClaw 版本不要共用 state。

### Claude Code

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8080
export OTEL_SERVICE_NAME=claude-code

claude --print "<任务>"
```

平台登记时选择「Claude Code / 本地 CLI」，可执行文件默认 `claude`。

### DeepSeek Harness（DSH）

```bash
dsh plugin --profile headless add @loongsuite/dsh-plugin

export OTEL_SERVICE_NAME=dsh-agent
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8080

dsh --profile headless "<任务>"
```

平台登记时选择「DeepSeek Harness / 本地 CLI」，Profile 默认 `headless`。

### Hermes Agent

```bash
# 安装到 Hermes 实际使用的 Python 环境
python -m pip install hermes-plugin-otel-tracing

export HERMES_OTEL_ENABLED=true
export HERMES_OTEL_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8080
export OTEL_SERVICE_NAME=hermes-agent

hermes chat -Q -q "<任务>"
```

平台登记时选择「Hermes / 本地 CLI」，可执行文件默认 `hermes`。

### Codex CLI

Codex 0.160+ 内置 OTel，无需额外插件。在 `~/.codex/config.toml` 增加：

```toml
[otel]
trace_exporter = { otlp-http = { endpoint = "http://127.0.0.1:8080/v1/traces", protocol = "binary" } }
```

非交互执行：

```bash
codex exec --skip-git-repo-check "<任务>"
```

不想改全局配置时，可用 `-c` 在单次运行注入同样的值。Codex 原生 Trace 不含 usage，Local CLI 模式由 Runner 从 `codex exec --json` 的 `turn.completed` 事件补写 Token；仅观测模式暂不采集 Token。

### 内容采集

默认只采集工具名、状态和耗时，不采集 Prompt、模型响应与工具参数/结果原文。需要在轨迹里看到 Tool input/output 时：

| 平台 | 开关 |
|---|---|
| OpenClaw | `diagnostics.otel.captureContent: true`，重启 Gateway |
| Hermes | `HERMES_OTEL_CAPTURE_CONTENT=true` |
| Codex | 按版本配置 `[otel]` 的 `log_user_prompt` / `log_agent_responses` |

内容可能包含业务数据、密钥或模型原文，只建议在受控评测环境开启，并配合访问控制、脱敏和保留策略。

## 完整评测流程

```text
接入 Agent
    ↓
创建数据集与 Case
    ↓
配置评估器
    ↓
创建实验并绑定评估器
    ↓
逐条运行 Agent，接收完整 Trace
    ↓
执行评分并保存证据
    ↓
查看实验汇总和逐样本结果
```

### 数据集

每条 Case 包含任务输入、参考答案和可选的确定性断言。数据集负责定义“测什么”，不保存运行生成的 Token 或耗时。

### 评估器

| 类型 | 用途 |
|---|---|
| Rule | 精确匹配、Token 上限、时延上限等确定性规则 |
| Trajectory Rule | 工具错误、循环调用等过程规则 |
| LLM Judge | 任务完成度、指令遵循、安全等语义评分 |
| Code | 使用受限表达式执行可复核的自定义逻辑 |
| Human | 人工复核结果 |

LLM Judge 的模型配置与被测 Agent 分离。添加模型后应先使用“测试连通性”确认 API、模型名和密钥有效。

### Code 评估器

Code 评估器不接收 Python 文件，也不会直接执行任意用户脚本。用户填写一个受限表达式，可访问：

- `query`：Case 输入
- `response`：Agent 实际输出
- `reference`：参考答案
- `trace`：聚合后的 Trace 字段
- `trajectory`：Span 列表

例如，数值答案一致性：

```python
normalize_number(response) == normalize_number(reference)
```

允许使用少量纯函数，如 `normalize_number`、`len`、`str`、`int`、`float`、`abs`、`min` 和 `max`。导入模块、属性访问、文件、网络和系统命令均会被拒绝。

## GSM8K 小样本真实回归

[scripts/run_gsm8k_openclaw.py](scripts/run_gsm8k_openclaw.py) 可以完成一个五条样本的 OpenClaw 端到端验证：

1. 下载 OpenAI 官方 GSM8K `test.jsonl`。
2. 校验文件 SHA-256。
3. 创建数据集、Case 和评估器。
4. 通过与页面实验按钮相同的 OpenClaw Runner 逐条运行。
5. 等待完整 OTLP Trace。
6. 执行数值正确性、Token、时延和工具错误评分。
7. 输出并保存实验汇总。

运行前需按上面的 OpenClaw Runner 配置设置配置文件和独立状态目录：

```bash
export PYTHONPATH=src
export AIOS_OPENCLAW_CONFIG_PATH=/etc/openclaw/openclaw.json5
export AIOS_OPENCLAW_STATE_DIR=/var/lib/aios-trace-eval/openclaw-state
python scripts/run_gsm8k_openclaw.py --limit 5
```

下载的数据、OpenClaw 本地状态、密钥和 SQLite 运行数据库位于被 Git 忽略的目录中，不会随代码提交。

## 指标口径

| 指标 | 口径 |
|---|---|
| Runtime 完成 | Trace 正常结束，只表示执行层完成 |
| 业务通过 | 绑定的自动评估器全部通过 |
| 总 Token | 按平台口径聚合，见下表 |
| 端到端耗时 | Run Trace 最早 Span 到最晚 Span 的时间 |
| TTFT | 模型请求到首个输出的时间，前提是该平台上报该字段 |
| 工具错误 | 状态为 error 或 blocked 的工具 Span 数量 |
| RSS / Heap | 本地 CLI 为进程树采样；OpenClaw Gateway 为共享 Gateway 进程指标 |
| CPU 核心占用 | 进程 CPU 使用率除以单核容量，使用多个核心时可以超过 100% |
| 综合分 | 当前为已执行自动评估器得分的算术平均值 |

Token 口径：

| 平台 | 口径 |
|---|---|
| OpenClaw | `input + output + cache_read + cache_write + reasoning` |
| Claude Code | `input + output + cache_read + cache_write` |
| Hermes | `input + output + cache_read + cache_write`（reasoning 已含在 output） |
| DSH | `input + output`（cache_read 是 input 子集） |
| Codex | `input + output`（cached_input 是 input 子集，来自 CLI usage） |
| 通用 GenAI | 各分类之和 |

“Runtime 完成”和“业务通过”是两个不同概念。模型成功返回内容不代表答案正确，也不代表任务目标已经完成。

当前业务结论按以下规则计算：

- 每个自动评估器先根据自己的 `pass_threshold` 得出通过或失败。
- 绑定的自动评估器全部通过时，Run 的 `business_status` 为 `passed`。
- 任意自动评估器失败时，Run 的 `business_status` 为 `failed`。
- 综合分是本次已执行自动评估器得分的算术平均值；它用于比较，不覆盖失败门禁。
- `human` 评估器不会自动执行，需要人工复核后写入结论。

## 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AIOS_HOST` | `0.0.0.0` | 服务监听地址 |
| `AIOS_PORT` | `8080` | 服务监听端口 |
| `AIOS_DB_PATH` | `data/aios_eval.db` | SQLite 数据库路径 |
| `AIOS_WEB_DIR` | `web` | 前端静态资源目录 |
| `AIOS_RELOAD` | `0` | 开发环境自动重载开关 |
| `AIOS_OPENCLAW_MODE` | `local` | OpenClaw Runner：`local` 或 `gateway` |
| `AIOS_OPENCLAW_CONFIG_PATH` | 空 | OpenClaw 配置文件 |
| `AIOS_OPENCLAW_STATE_DIR` | 空 | OpenClaw 独立状态目录（必须存在） |
| `OPENCLAW_GATEWAY_PORT` | `18789` | Gateway 模式端口 |
| `AIOS_AGENT_MODEL` | 空 | 覆盖 Agent 默认模型 |
| `AIOS_AGENT_PROFILE` | 空 | 覆盖 Agent Profile |
| `AIOS_AGENT_EXTRA_ARGS` | 空 | 追加 Agent CLI 参数 |
| `AIOS_RESOURCE_SAMPLE_SECONDS` | `0.5` | 本地 CLI 进程树采样间隔 |
| `HERMES_OTEL_CAPTURE_CONTENT` | `false` | Hermes 是否采集正文与工具内容 |

## 开发验证

Linux / macOS：

```bash
export PYTHONPATH=src
python -m compileall -q src scripts
node --check web/assets/app.js
python scripts/selftest.py
python scripts/selftest_adapters.py
python scripts/selftest_platform.py
```

Windows PowerShell：

```powershell
$env:PYTHONPATH = "src"
python -m compileall -q src scripts
node --check web/assets/app.js
python scripts/selftest.py
python scripts/selftest_adapters.py
python scripts/selftest_platform.py
```

## 项目结构

```text
aios-trace-eval/
├── config/                       # OpenClaw / Claude Code / DSH / Codex 接入样例
├── data/evalsets/                # 可提交的评测集样例
├── docs/                         # 接入指南、适配器架构、指标与字段映射
├── scripts/
│   ├── run_gsm8k_openclaw.py     # GSM8K 真实批量回归
│   ├── selftest.py
│   ├── selftest_adapters.py      # 适配器层单测
│   └── selftest_platform.py      # 平台端到端自测
├── src/aios_trace_eval/
│   ├── app.py                    # API、OTLP 接收和静态页面服务
│   ├── storage.py                # SQLite 存储与实验聚合
│   ├── otlp.py                   # OTLP Trace 解码
│   ├── otlp_metrics.py           # OTLP Metrics 解码
│   ├── experiment_runner.py      # 本地 CLI / Gateway 执行与关联
│   ├── canonical.py              # 平台无关的 CanonicalRun
│   ├── adapters/                 # openclaw / claude_code / dsh / hermes / codex / generic
│   ├── code_eval.py              # 受限 Code 表达式
│   ├── metrics.py                # 指标目录
│   └── judge.py                  # 离线评测逻辑
├── web/                          # 前端页面
└── Dockerfile
```

## 当前边界

- 当前使用 SQLite，适合单机和小规模团队评测，不适合高并发遥测写入。
- `/v1/logs` 当前接受请求但不解析落库；`/v1/metrics` 只保留资源曲线读取的运行时指标（RSS、Heap、CPU），其余每秒上报的指标直接丢弃；运行级 Token、耗时以 Trace Span 为准。
- Web 控制台目前不提供登录、租户隔离和权限控制，不应直接暴露到公网。
- 批量实验按 Case 串行执行，尚未提供平台内任务队列、并行调度、取消和自动重试。
- Code 评估器是受限表达式，不是完整 Python 或 JavaScript 沙箱。
- LLM Judge 尚未实现评估器版本发布、Prompt 版本对比和 Judge 一致性校准。
- Codex 原生 Trace 不带 usage：Local CLI 模式由 Runner 补写 Token，仅观测模式没有 Token；Codex Span 名会随版本变化，升级后需要复核适配器。
- Claude Code / DSH / Hermes / Codex 的本地 CLI 通过 `aios.run.id` 精确关联 Trace；OpenClaw（Gateway 或本地）按新增 Trace 与时间窗回退。Remote Worker 尚未开放。
- `captureContent=false` 时不会采集 Prompt 与响应原文，因此无法执行依赖完整内容或工具参数的轨迹评估。
- 尚未提供 Prompt / Skill 资产管理和记忆库功能。

生产部署建议至少增加 TLS、接入鉴权、数据保留策略、密钥管理、PostgreSQL/ClickHouse 存储和独立的评估任务执行器。

## License

当前仓库尚未添加开源许可证。在选择许可证之前，请不要假定代码可以被无限制复制、修改或再分发。
