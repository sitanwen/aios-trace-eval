# AIOS Trace Eval

AIOS Trace Eval 是一个面向 OpenClaw 的 Agent 可观测与评测平台。它接收 OpenClaw 通过 OpenTelemetry 导出的运行数据，将 Trace、模型调用、Token、耗时、工具调用和评测结果关联到同一次 Run，并提供数据集、评估器、实验与逐样本结果管理。

当前版本适合本地开发、单机实验和小规模团队验证。生产部署前请先阅读“当前边界”。

## 已验证的 OpenClaw 版本

| 组件 | 已验证版本 | 说明 |
|---|---:|---|
| OpenClaw | `2026.8.2` | 本项目的真实回归环境 |
| diagnostics-otel | `2026.8.2` | OpenClaw OpenTelemetry 插件 |
| OTLP 协议 | `http/protobuf` | Trace 接收路径为 `/v1/traces` |
| Python | `3.10+` | 平台运行环境 |

项目按 OpenClaw `2026.8.x` 的诊断配置结构实现。目前只对 `2026.8.2` 做过完整验证；使用更早或更新版本时，应先确认 `diagnostics.otel` 字段和插件版本是否兼容。

平台核心使用 Python、FastAPI、SQLite 和原生浏览器 JavaScript，不依赖 Windows 专有组件。Docker 镜像基于 Linux，持续集成会在 Ubuntu 上使用 Python 3.10 和 3.12 执行完整自测。Windows 和 Linux 的差异只出现在环境变量写法及 OpenClaw 可执行文件解析中，批量 Runner 已分别处理 `openclaw.cmd` 和 `openclaw`。

## 能做什么

- 接收并解析 OpenClaw OTLP Trace
- 统计输入、输出、缓存与总 Token
- 记录端到端耗时、TTFT、模型调用、工具调用、工具错误与 Failover
- 管理 Agent、评测数据集和 Case
- 组合规则、轨迹规则、LLM Judge、Code 表达式与人工评估器
- 保存每个评估器的得分、理由和证据
- 汇总实验通过率、平均分、总 Token、平均耗时和逐样本结果
- 在 Web 控制台中查看 Run 和 Span 明细

## OpenClaw 接入

### 1. 部署 AIOS Trace Eval

使用 Docker Compose：

```bash
git clone https://github.com/sitanwen/aios-trace-eval.git
cd aios-trace-eval/config
docker compose up --build -d
```

或者直接运行 Python 服务：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m aios_trace_eval serve --host 0.0.0.0 --port 8080
```

服务包含以下入口：

| 路径 | 用途 |
|---|---|
| `/` | Web 控制台 |
| `/api/health` | 健康检查 |
| `/api/*` | 平台 API |
| `/v1/traces` | OTLP Trace 接收 |
| `/v1/metrics` | OTLP Metrics 接收 |
| `/v1/logs` | OTLP Logs 接收 |

本机调试可以访问 `http://127.0.0.1:8080`。部署到服务器后，应将下面配置中的 endpoint 换成实际服务地址，例如 `https://eval.example.com`。

### 2. 安装匹配版本的 OpenTelemetry 插件

在运行 OpenClaw 的环境中执行：

```bash
openclaw plugins install @openclaw/diagnostics-otel@2026.8.2 --accept-capabilities --pin
```

检查插件是否可用：

```bash
openclaw plugins list
openclaw plugins doctor
```

OpenClaw 与 `diagnostics-otel` 建议使用相同版本，避免配置结构或遥测字段不一致。

### 3. 配置 OpenClaw

将 [config/openclaw-otel.json5](config/openclaw-otel.json5) 中的内容合并到现有 `openclaw.json`，不要直接覆盖原有模型、渠道和 Agent 配置。

核心配置如下：

```json5
{
  plugins: {
    allow: ["diagnostics-otel"],
    entries: {
      "diagnostics-otel": { enabled: true },
    },
  },
  diagnostics: {
    enabled: true,
    otel: {
      enabled: true,
      endpoint: "https://eval.example.com",
      protocol: "http/protobuf",
      serviceName: "openclaw-eval",
      traces: true,
      metrics: true,
      logs: false,
      sampleRate: 1.0,
      flushIntervalMs: 1000,
      captureContent: false,
    },
  },
}
```

配置说明：

- `endpoint`：AIOS Trace Eval 的实际服务地址，不要包含 `/v1/traces`。
- `protocol`：当前适配 `http/protobuf`。
- `serviceName`：用于区分环境、版本或 OpenClaw 实例，建议使用稳定且可识别的名称。
- `sampleRate`：评测环境建议设为 `1.0`，保证每次运行都有完整证据。
- `flushIntervalMs`：小规模评测建议使用 `1000`，减少运行结束到页面可见的等待时间。
- `captureContent`：默认关闭，避免 Prompt、响应和工具参数随遥测发送。只有在数据治理允许的隔离环境中才应开启。

如果采集服务需要鉴权，可以在 OpenClaw OTLP 配置中增加请求头，并在网关或反向代理层校验：

```json5
headers: {
  Authorization: "Bearer ${OTEL_TOKEN}",
}
```

### 4. 重启并验证

重启 OpenClaw Gateway 或对应的 OpenClaw 进程，然后执行一次真实 Agent 任务。验证顺序：

1. 请求 AIOS Trace Eval 的 `/api/health`，确认服务正常。
2. 检查 OpenClaw 日志，确认 `diagnostics-otel` 已加载且没有导出错误。
3. 执行一次会进入模型请求的 Agent 任务。
4. 在“运行记录”中确认出现新的 Trace。
5. 打开 Run 详情，核对模型、Token、耗时与 Span 链路。

如果只出现健康检查或 Gateway 活动、但没有 Token 和模型 Span，通常表示任务没有真正进入模型请求，不能把这类运行当作评测样本。

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

当前 Code 评估器不接收 Python 文件，也不会直接执行任意用户脚本。用户填写一个受限表达式，可访问：

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

[scripts/run_gsm8k_openclaw.py](scripts/run_gsm8k_openclaw.py) 可以完成一个五条样本的端到端验证：

1. 下载 OpenAI 官方 GSM8K `test.jsonl`。
2. 校验文件 SHA-256。
3. 创建数据集、Case 和评估器。
4. 通过本地 OpenClaw Gateway 逐条运行。
5. 等待完整 OTLP Trace。
6. 执行数值正确性、Token、时延和工具错误评分。
7. 输出并保存实验汇总。

运行前需已启动 AIOS Trace Eval 和 OpenClaw Gateway，并提供 Gateway 连接信息：

Linux / macOS：

```bash
export PYTHONPATH=src
export OPENCLAW_GATEWAY_TOKEN="<Gateway Token>"
export OPENCLAW_GATEWAY_PORT=18790
python scripts/run_gsm8k_openclaw.py --limit 5
```

Windows PowerShell：

```powershell
$env:PYTHONPATH = "src"
$env:OPENCLAW_GATEWAY_TOKEN = "<Gateway Token>"
$env:OPENCLAW_GATEWAY_PORT = "18790"
python scripts/run_gsm8k_openclaw.py --limit 5
```

非默认部署可以通过参数指定平台地址和 OpenClaw 目录：

```bash
python scripts/run_gsm8k_openclaw.py \
  --api-url https://eval.example.com \
  --gateway-port 18790 \
  --openclaw-config /etc/openclaw/openclaw.json \
  --openclaw-state-dir /var/lib/openclaw \
  --limit 5
```

下载的数据、OpenClaw 本地状态、密钥和 SQLite 运行数据库位于被 Git 忽略的目录中，不会随代码提交。

## 指标口径

| 指标 | 口径 |
|---|---|
| Runtime 完成 | Trace 正常结束，只表示执行层完成 |
| 业务通过 | 绑定的自动评估器全部通过 |
| 总 Token | Trace 中输入、输出及缓存等 usage 的聚合结果 |
| 端到端耗时 | Run Trace 最早 Span 到最晚 Span 的时间 |
| TTFT | 模型请求到首个输出的时间，前提是 OpenClaw 上报该字段 |
| 工具错误 | 状态为 error 或 blocked 的工具 Span 数量 |
| 综合分 | 当前为已执行自动评估器得分的算术平均值 |

“Runtime 完成”和“业务通过”是两个不同概念。模型成功返回内容不代表答案正确，也不代表任务目标已经完成。

当前业务结论按以下规则计算：

- 每个自动评估器先根据自己的 `pass_threshold` 得出通过或失败。
- 绑定的自动评估器全部通过时，Run 的 `business_status` 为 `passed`。
- 任意自动评估器失败时，Run 的 `business_status` 为 `failed`。
- 综合分是本次已执行自动评估器得分的算术平均值；它用于比较，不覆盖失败门禁。
- `human` 评估器不会自动执行，需要人工复核后写入结论。

例如四个评估器中三个得 1 分、一个得 0 分时，综合分为 `0.75`，但业务结论仍然是失败。

### 输入输出与轨迹内容

正式评测 Runner 会在关联 Case 时保存任务输入和 Agent 实际输出。若要查看每次模型调用和工具调用的完整内容，需要在受控评测环境将 OpenClaw 配置设为：

```json5
captureContent: true
```

开启后，OpenClaw 2026.8.2 会在 Trace 中包含模型输入、模型输出、工具参数和工具 Observation，页面会将它们按执行步骤展示。内容可能包含 Prompt、业务数据、工具参数或模型响应，不建议在生产环境无条件开启；应同时配置访问控制、脱敏和数据保留策略。

## 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AIOS_HOST` | `0.0.0.0` | 服务监听地址 |
| `AIOS_PORT` | `8080` | 服务监听端口 |
| `AIOS_DB_PATH` | `data/aios_eval.db` | SQLite 数据库路径 |
| `AIOS_WEB_DIR` | `web` | 前端静态资源目录 |
| `AIOS_RELOAD` | `0` | 开发环境自动重载开关 |

## 开发验证

Linux / macOS：

```bash
export PYTHONPATH=src
python -m compileall -q src scripts
node --check web/assets/app.js
python scripts/selftest.py
python scripts/selftest_platform.py
```

Windows PowerShell：

```powershell
$env:PYTHONPATH = "src"
python -m compileall -q src scripts
node --check web/assets/app.js
python scripts/selftest.py
python scripts/selftest_platform.py
```

## 项目结构

```text
aios-trace-eval/
├── config/                       # Docker 与 OpenClaw 接入示例
├── data/evalsets/                # 可提交的评测集样例
├── docs/                         # 指标、字段映射与方案文档
├── scripts/
│   ├── run_gsm8k_openclaw.py     # GSM8K 真实批量回归
│   ├── selftest.py
│   └── selftest_platform.py
├── src/aios_trace_eval/
│   ├── app.py                    # API、OTLP 接收和静态页面服务
│   ├── storage.py                # SQLite 存储与实验聚合
│   ├── otlp.py                   # OTLP protobuf 解码
│   ├── code_eval.py              # 受限 Code 表达式
│   ├── metrics.py                # 指标目录
│   └── judge.py                  # 离线评测逻辑
├── web/                          # 前端页面
└── Dockerfile
```

## 当前边界

- 当前使用 SQLite，适合单机和小规模团队评测，不适合高并发遥测写入。
- `/v1/metrics` 和 `/v1/logs` 当前接受请求但不单独落库；运行级指标以 Trace Span 为准。
- Web 控制台目前不提供登录、租户隔离和权限控制，不应直接暴露到公网。
- 批量实验目前通过脚本执行，尚未提供平台内任务队列、并行调度、取消和自动重试。
- Code 评估器是受限表达式，不是完整 Python 或 JavaScript 沙箱。
- LLM Judge 尚未实现评估器版本发布、Prompt 版本对比和 Judge 一致性校准。
- 精确关联 Case 与 Trace 目前依赖批量 Runner 的顺序窗口；后续应在 OpenClaw 调用链中注入稳定的 Run ID 或 Trace Context。
- `captureContent=false` 时不会采集 Prompt 与响应原文，因此无法执行依赖完整内容或工具参数的轨迹评估。

生产部署建议至少增加 TLS、接入鉴权、数据保留策略、密钥管理、PostgreSQL/ClickHouse 存储和独立的评估任务执行器。

## License

当前仓库尚未添加开源许可证。在选择许可证之前，请不要假定代码可以被无限制复制、修改或再分发。
