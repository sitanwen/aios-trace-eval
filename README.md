# AIOS Trace Eval

面向 OpenClaw 的一体化 Agent 可观测与评测平台。OpenClaw 通过 **OpenTelemetry GenAI / OTLP HTTP** 上报，平台在一个 Web 控制台里完成 Trace、Token、耗时、业务成功率、实验对比和质量门禁。

项目不再依赖 Grafana、Jaeger 或裸 JSON 报告作为用户界面。

## 设计目标

- **单一入口**：控制台、REST API 和 OTLP 接收端共用 `http://localhost:8080`。
- **统一 Run 模型**：Trace 是运行证据，业务终态是成功判定，两者通过 `run_id / trace_id` 关联。
- **原生 Trace Explorer**：模型调用、工具调用、错误和耗时瀑布图直接在平台内展示。
- **可评测**：业务终态断言优先，其次是轨迹规则，LLM Judge 仅用于开放产出。
- **可比较**：同一评测集下比较 Agent、模型、Prompt 或 Skill 版本。
- **可门禁**：业务正确性一票否决，Token 和耗时相对基线控制。

## 架构

```text
OpenClaw diagnostics-otel
          │ OTLP/HTTP protobuf
          ▼
┌───────────────────────────────────────┐
│ AIOS Trace Eval :8080                 │
│                                       │
│ /v1/traces  OTLP receiver             │
│ /api/*      observability + eval API  │
│ /           unified web console       │
│               │                       │
│          SQLite storage               │
└───────────────────────────────────────┘
```

当前版本使用 SQLite，适合单机和团队评测环境。接口与存储已分层，生产规模可将 `Store` 替换为 PostgreSQL + ClickHouse，而不改变 OpenClaw 接入和前端页面。

## 快速开始

### Docker

```bash
cd config
docker compose up --build -d
```

打开 `http://localhost:8080`。

### 本地运行

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m aios_trace_eval serve
```

## OpenClaw 接入

把 `config/openclaw-otel.json5` 中的配置合并进 OpenClaw 配置。核心配置是：

```json5
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
    sampleRate: 1.0,
    captureContent: false,
  },
}
```

OpenClaw 会向 `/v1/traces` 上报 protobuf。平台从 Trace Span 直接聚合 Token 和运行指标，避免 Trace 与 Metrics 双重计数。

## 控制台

- **工作台**：展示接入流程和真实运行数据概览；没有数据时保持空状态。
- **Agent**：登记 OpenClaw Gateway 或兼容 HTTP Endpoint。
- **数据集**：创建数据集，并维护任务输入、期望输出和断言 Case。
- **评估器**：组合规则、轨迹规则、LLM Judge、受限 Code 表达式与人工复核。
- **评测实验**：选择 Agent、数据集和多个评估器，查看汇总与逐样本结果。
- **运行记录**：查看真实收到的 Trace、Token、耗时、模型与 Span 链路。

## 评测流程

当前最小闭环与百炼、CozeLoop 等平台的主流程一致：

```text
接入 Agent → 定义数据集 → 组合评估器 → 执行实验 → 逐样本评分 → 汇总报告
```

每条样本先经过 OpenClaw 真实执行并产生 OTLP Trace，再运行绑定的评估器。样本只有在全部自动评估器通过时才标记为通过；页面同时保留每个评估器的得分、理由和证据。

### Code 评估器

MVP 不接收任意 Python 文件，也不在平台宿主机执行用户脚本。用户填写一个受限表达式，可读取：

- `query`：样本输入
- `response`：Agent 实际输出
- `reference`：参考答案
- `trace`：本次 Trace 聚合字段
- `trajectory`：Span 列表

可调用 `normalize_number`、`len`、`str`、`int`、`float`、`abs`、`min`、`max` 等纯函数；导入、属性访问、文件、网络和系统命令均被拒绝。例如：

```python
normalize_number(response) == normalize_number(reference)
```

生产版若要支持完整 Python/JavaScript，应将每次评估放入无网络、只读文件系统、限 CPU/内存/时长的独立容器，而不是直接上传到平台进程执行。

## GSM8K 真实小样本回归

启动平台与 OpenClaw Gateway 后运行（脚本会下载并校验 OpenAI 官方 GSM8K，`work/` 不会提交 Git）：

```powershell
$env:PYTHONPATH = "src"
$env:OPENCLAW_GATEWAY_TOKEN = "<你的本地 Gateway Token>"
$env:OPENCLAW_GATEWAY_PORT = "18790"
python scripts/run_gsm8k_openclaw.py --limit 5
```

脚本会幂等创建 5 条数据、数值答案评估器、Token/时延预算与实验，然后逐条调用 OpenClaw、等待完整 Trace、执行评分并输出真实汇总。它不会生成演示 Trace 或填补缺失指标。

## 指标口径

| 指标 | 口径 |
|---|---|
| 运行完成 | OpenClaw Span 正常结束，仅代表执行层没有报错 |
| 业务成功 | 环境终态断言或人工复核通过 |
| Token/成功任务 | 仅对 `business_status=passed` 的 Run 汇总 Token，再除以通过数量 |
| 任务耗时 | Trace 中最早 Span 到最晚 Span 的端到端时间 |
| 工具错误率 | error/blocked 工具 Span ÷ 工具 Span 总数 |
| 成本 | OpenClaw 上报的估算成本；不可替代供应商账单 |

“运行完成”和“业务成功”必须分开。Agent 给出了回答，不代表业务目标已经达成。

## 数据与隐私

`captureContent` 默认关闭。只有在隔离评测环境、数据保留策略和权限已经确认时才应开启。生产环境建议把 Prompt、工具输入输出和产物放在独立对象存储，Trace 只保存引用。

## 目录

```text
aios-trace-eval/
├── config/
│   ├── docker-compose.yml
│   └── openclaw-otel.json5
├── data/
│   └── evalsets/
├── docs/
├── src/aios_trace_eval/
│   ├── app.py                 # Web API + OTLP receiver
│   ├── storage.py             # 统一存储
│   ├── otlp.py                # OTLP protobuf 解析与 Trace 聚合
│   ├── extractor.py           # 离线兼容提取器
│   ├── judge.py               # 评测判定
│   ├── code_eval.py           # 受限 Code 表达式
│   └── runner.py              # OpenClaw Runner
├── web/
│   ├── index.html
│   └── assets/
└── Dockerfile
```

## 当前边界

- SQLite 适合单机评测；高并发生产部署应迁移到 PostgreSQL/ClickHouse。
- 当前 `/v1/metrics` 接收成功但不单独落库，避免与 Trace usage 重复；运行级聚合以 Span 为准。
- 精确的 `case_id / experiment_id / version` 需要作为 OTel 属性随 Run 注入。通过 Gateway 执行评测时应为每个 Case 注入独立 `traceparent`。
- 终态断言仍需在隔离沙箱内执行，不应在平台宿主机运行任意 Shell。
- Code 评估器目前是受限表达式，不是完整 Python/JavaScript 沙箱；评估器版本发布、字段映射 UI、并行调度和重试策略仍待完善。
