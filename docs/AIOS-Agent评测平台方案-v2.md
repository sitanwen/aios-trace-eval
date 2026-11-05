# AIOS Agent 可观测与评测平台方案 v2

> 本文是对原《Agent 评测平台竞品分析（含横向对比）》中 AIOS 落地方案的结构性重写。保留原文关于业务终态、每成功任务 Token、评测资产和发布门禁的判断，废弃“Grafana + Jaeger + JSON 报告”作为产品形态的设计。

## 1. 结论先行

AIOS 不应被设计成多个开源观测工具的入口集合，而应成为一个拥有自己对象模型和交互闭环的 Agent 质量平台。

平台的核心对象不是 Dashboard，也不是 Trace，而是 **Run**：一次 Agent 在特定版本、特定 Case 和特定环境中的完整执行。Trace 是 Run 的执行证据，终态断言是 Run 的业务结论，Token 和耗时是 Run 的效率代价，所有页面和统计都围绕 Run 展开。

产品只向用户暴露一个 Web 控制台：

- 线上运行可以观测；
- 单次失败可以定位；
- Trace 可以沉淀成评测 Case；
- 固定 Case 可以发起多版本实验；
- 实验结论可以成为发布门禁。

## 2. 为什么不采用 Grafana + Jaeger + JSON

三者分别解决不同的基础设施问题，但无法自然组成 Agent 评测产品。

| 组件形态 | 擅长 | 无法解决的问题 |
|---|---|---|
| Grafana | 时序指标、告警、通用 Dashboard | 不理解 Case、版本、终态断言和逐样本对比 |
| Jaeger | 分布式 Trace 检索与瀑布图 | 不理解业务成功、评测集、实验和门禁 |
| JSON 报告 | 机器间传输、离线归档 | 不适合查询、关联、协作和持续迭代 |

把三个界面放在一起，只能减少跳转，不能形成数据闭环。真正的一体化要求 Trace、评测和实验从数据库主键开始就是同一套对象，而不是在页面层做 iframe 或超链接拼接。

因此，AIOS v2 采用以下原则：

1. OpenTelemetry 是接入协议，不是产品界面。
2. Trace 后端是平台内部实现，不暴露给普通用户。
3. 业务成功与运行成功分开存储、分开展示。
4. 聚合统计、单条证据和评测结论必须能互相下钻。
5. 平台内任何分数都能追溯到 Case、Run、Span 和证据。

## 3. 产品定位

AIOS 的产品定位是：**面向 OpenClaw 的运行质量与版本评测控制台**。

它不追求复刻通用 APM，也不以评估器数量作为主要竞争点。第一阶段集中解决四个问题：

1. 这个 Agent 版本在真实运行中发生了什么？
2. 任务是否真的完成，而不仅是模型是否给出了回答？
3. 成功付出了多少 Token、时间和工具调用？
4. 新版本相较旧版本究竟改善还是退化？

## 4. 核心对象模型

### 4.1 Run 是系统主轴

```text
Dataset
  └─ Case
      └─ Experiment
          └─ Variant
              └─ Run
                  ├─ Trace / Span
                  ├─ Runtime Metrics
                  ├─ Terminal Assertions
                  ├─ Rule Evaluations
                  ├─ LLM / Human Grades
                  └─ Artifacts
```

每个 Run 必须携带以下身份：

| 字段 | 作用 |
|---|---|
| `run_id` | 平台内一次执行的唯一主键 |
| `trace_id` | OpenTelemetry 调用链主键 |
| `case_id` | 对应的评测用例 |
| `experiment_id` | 所属实验 |
| `variant_id/version` | Agent、模型、Prompt 或 Skill 版本 |
| `attempt` | 同一 Case 的第几次重复运行 |
| `environment_id` | 执行环境或镜像摘要 |

线上自然流量没有 Case 时，允许 `case_id` 为空；当它被回流进评测集后，再生成新的受控实验 Run，而不是修改原始线上 Run。

### 4.2 两种成功状态

- `runtime_status`：执行是否正常结束，包括 completed、error、timeout、cancelled。
- `business_status`：业务目标是否达成，包括 passed、failed、ungraded。

模型回答正常、工具没有异常，只能证明 runtime completed。只有终态断言、确定性规则或人工复核通过，才能标记 business passed。

## 5. 指标体系

### 5.1 结果层

- 业务任务成功率；
- 子目标完成率；
- 输出可用率；
- 事实与数据断言准确率；
- Pass ratio、all-pass stability；
- 失败原因分布。

### 5.2 过程层

- 工具选择正确率；
- 工具参数断言通过率；
- 工具执行成功率；
- 重复调用、无增益调用和循环；
- 轨迹关键步骤覆盖率；
- 检索证据覆盖率；
- 中间产物质量；
- 多 Agent 交接失败归因。

### 5.3 效率层

- 端到端 P50/P95/P99 耗时；
- 模型、工具和排队耗时占比；
- TTFT；
- 输入、输出、缓存、推理 Token；
- 每成功任务 Token；
- 每成功任务成本；
- 模型和工具调用次数；
- 内存压力与 Failover。

### 5.4 风险层

- 越权工具调用；
- Sandbox 拒绝；
- 敏感信息泄露；
- 有害输出；
- 不应执行任务的拒答正确率。

### 5.5 四家方案如何落到 AIOS

| 平台 | 值得吸收的设计 | AIOS 不照搬的部分 |
|---|---|---|
| 阿里 Evolution | 观测与评测共用 Trace 语义、线上 Bad Case 回流 | 不把运行完成当业务成功；补环境终态断言与版本对比 |
| 字节 CozeLoop | 实验是一等对象、逐样本横向比较、轨迹结构化 | 重复工具调用增加参数语义与 Observation 变化判断，避免误伤分页和合理重试 |
| 华为 AgentArts | 离线发布前评测 + 在线生产抽样、评估器自身成本 | 评估器分数不直接覆盖硬断言，门禁结论留在本平台内 |
| 腾讯 ADP + AgentObs | 四类 Token、成本金额、TPS 和分位时延 | 评测与观测必须共享 `run_id`，不能拆成两套无法关联的产品 |

AIOS 第一阶段不追求“评估器数量最多”，而是先补四家的共同空白：业务终态成功率、每成功任务 Token/成本、运行时资源和发布门禁。页面中的指标目录明确标注每项指标来自自动采集、数据集断言、规则、评估器、重复实验还是运行时探针；未接入的证据一律显示“未采集”，不生成演示分数。

## 6. 技术架构

### 6.1 单入口架构

```text
OpenClaw diagnostics-otel
          │ OTLP/HTTP protobuf
          ▼
┌──────────────────────────────────────────────┐
│ AIOS Platform :8080                          │
│                                              │
│  OTLP Receiver ──▶ Trace Normalizer          │
│                         │                    │
│  Eval Runner ───────────┼──▶ Unified Store   │
│                         │                    │
│  REST API / BFF ◀───────┘                    │
│         │                                    │
│  Web Console                                │
└──────────────────────────────────────────────┘
```

浏览器、REST API 和 OTLP 接入共用一个端口。底层即使未来拆成多个服务，也只在内部网络开放，不改变产品入口。

### 6.2 当前实现与生产演进

| 阶段 | Trace/指标存储 | 控制数据 | 使用范围 |
|---|---|---|---|
| 本地/MVP | SQLite | SQLite | 单机开发、几十至几千 Run |
| 团队版 | PostgreSQL | PostgreSQL | 团队评测、受控并发 |
| 生产版 | ClickHouse | PostgreSQL | 海量 Trace、长期趋势、在线采样 |

存储实现通过 Repository/Store 抽象隔离。前端与 OpenClaw 的 OTLP 接入地址不随存储迁移而变化。

### 6.3 OpenTelemetry 规范化

平台同时读取：

- OpenTelemetry GenAI 标准字段；
- OpenClaw `openclaw.*` 扩展字段；
- AIOS 自定义关联字段。

建议的 AIOS 属性：

```text
aios.run.id
aios.case.id
aios.experiment.id
aios.variant.id
aios.agent.version
aios.environment.id
```

不能把 Span 名称和字段写死在业务代码里。应建立 Telemetry Adapter，并随 OpenClaw 和 OpenTelemetry GenAI 版本更新：

```text
TelemetryAdapter
├─ OpenClawCompatAdapter
├─ OpenClawLatestAdapter
└─ OTelGenAILatestAdapter
```

### 6.4 Trace 精确关联

评测 Runner 在执行 Case 前生成 `run_id` 与 W3C `traceparent`，通过 OpenClaw Gateway 的请求级 Trace Context 注入。平台提前登记 `run_id → trace_id`，执行后只等待该 trace_id 完整到达。

禁止使用“查询最新一条 Trace”的方式关联 Case，因为并发评测时会发生串线。

### 6.5 本机 OpenClaw 接入验证（2026-11-05）

本机已按公司目标版本安装 OpenClaw `2026.8.2` 与完全匹配的 `@openclaw/diagnostics-otel@2026.8.2`。由于原 OpenClaw 2026.9.5 状态库 schema 17 高于 2026.8.2 支持的 schema 15，验证使用独立的 state、配置和数据库，不修改或迁移旧状态库。OTLP 使用 `http/protobuf`，开启 traces 与 metrics、关闭内容采集。

真实链路已经完成：常驻 Gateway 启动成功，`agent` 请求进入 OpenAI 兼容模型端点并返回 `AIOS_OTEL_OK`；平台收到同一次运行的 `openclaw.harness.run`、`openclaw.run`、`openclaw.context.assembled` 与 `openclaw.model.call` 四个 Span。该次烟测采集到输入 12 Token、输出 4 Token、总计 16 Token，模型调用 1 次，Run 总耗时 241 ms，TTFT 29 ms。

接收端仅将包含 Run 或 Model Call 的业务 Trace 计为一次运行。`openclaw.diagnostic.*` 内部诊断 Trace 和独立的 `openclaw.model.usage` 辅助 Trace 不进入 Run 列表，从而避免一次真实任务被膨胀成多条运行。烟测数据保存在隔离数据库中，生产库继续保持无虚假 Run。后续接入公司真实模型时，只需将隔离配置中的 provider endpoint 与凭证替换为企业配置，再用数据集 Case 执行批量评测。

随后已接入真实 DeepSeek 凭证并完成第一条数据集 Case。OpenClaw 2026.8.2 返回正确答案 `42`，OTLP Trace 采集到输入 25,741 Token、输出 1 Token、总耗时 2,629 ms、TTFT 684 ms。LLM Judge 对答案质量给出 1.0，但 Case 的确定性 Token 门禁要求 `< 300`，因此最终结论为失败。该结果验证了“LLM 分数不覆盖硬规则”的判定优先级，也暴露出短任务被系统提示与工具定义放大到 2.57 万输入 Token 的真实效率问题。

## 7. 判定系统

判定优先级固定：

1. 环境终态断言；
2. 确定性规则；
3. LLM Judge；
4. 人工复核。

终态断言必须在任务沙箱或只读业务连接器中执行，不允许平台宿主机执行任意 Shell。每个断言都保存：

- expected；
- actual；
- passed；
- evidence；
- duration；
- error category。

LLM Judge 必须记录模型、Prompt 版本、温度、Token、耗时和成本。开放式评分不应覆盖硬断言，也不应在没有人工一致性校准时成为发布门禁。

### 7.1 指标、评估器与评分方案

平台将三个容易混淆的对象分离：

| 对象 | 责任 | 关键配置 |
|---|---|---|
| 指标 | 定义“衡量什么” | 结果/过程/效率/风险维度、单位、聚合口径 |
| 评估器 | 定义“如何计算” | Rule、Trajectory Rule、LLM、Code、Human，评分范围、阈值、版本 |
| 实验评分方案 | 定义“一次实验如何组合判定” | 评估器集合、字段映射、权重、门禁、默认 Judge 模型 |

评估器是可复用、可版本化的资产，不与某一个数据集绑定。创建实验时选择一个或多个评估器，并把 `query`、`reference`、`response`、`trajectory` 与数据集列或 Run 字段映射。安全、数据正确性和终态断言可配置为一票否决；体验型 LLM 指标使用阈值，不与硬门禁混成一个不可解释的总分。

当前实现已提供 8 个预置评估器：精确答案匹配、任务完成度、指令遵循、Token 预算、端到端时延预算、工具执行成功、循环调用检测、安全与越权；同时允许用户创建 Rule、Trajectory Rule、LLM、Code 和 Human 类型的自定义评估器。Code 类型在真正执行前必须进入受限沙箱，禁止直接在平台宿主机运行用户代码。

## 8. 前端信息架构

### 8.1 运行总览

回答“现在质量怎样”：

- 业务成功率；
- Token/成功任务；
- 成本/成功任务；
- P95/P99；
- runtime error；
- 模型表现；
- 最近退化版本。

### 8.2 Trace Explorer

回答“这次运行发生了什么”：

- 按 Agent、模型、版本、Case、状态搜索；
- 原生 Span 瀑布图；
- 模型与工具详情；
- Token、TTFT、错误、循环与 Failover；
- 从 Trace 创建回归 Case；
- 对未评测 Run 进行人工复核。

### 8.3 评测实验

回答“新版本是否更好”：

- 评测集 × Variant × 重复次数；
- 逐 Case 对齐比较；
- 成功率、质量、Token、成本、耗时；
- Trace Diff；
- 回归样本聚类；
- 基线与候选版本门禁结论。

实验创建页同时固定 Agent、数据集、默认 Judge 模型和评估器集合。单条样本应展示每个评估器的独立得分与理由，实验级展示各评估器通过率；不允许只保留一个加权总分而丢失原始判定证据。

### 8.4 评测资产

- 黄金集：定义业务下限；
- 错题集：防止历史问题复发；
- 挑战集：标定边界和长程能力；
- 每条 Case 绑定环境快照、输入、终态、规则和标签。

### 8.5 质量门禁

硬门禁：

- 业务成功率不得下降；
- 数据准确性失败为零容忍；
- 安全与越权检查一票否决。

软门禁：

- 每成功任务 Token 增长不超过阈值；
- P95 耗时不超过 SLA；
- 工具错误率、循环率和 Failover 不显著退化。

## 9. 数据与隐私

Prompt、响应、工具输入输出和中间产物默认不采集。评测环境确需内容时：

1. 显式开启 captureContent；
2. Collector/Receiver 先脱敏；
3. 大字段进入对象存储；
4. Span 只保存受权限控制的引用；
5. 原文与普通运行指标使用不同保留周期；
6. 查看原文需要单独权限和审计。

“未采集”“已脱敏”“因权限不可见”和“真实为空”必须在 UI 中使用不同状态表示。

## 10. 与竞品的差异

AIOS 不在第一阶段追求评估器数量超过商业平台，而是优先解决它们共同薄弱的连接问题。

| 差异点 | AIOS 做法 |
|---|---|
| 业务任务成功 | 使用环境终态，不以模型自述完成为准 |
| 每成功任务 Token | 只统计 business passed 的 Run，失败成本单列 |
| Trace 与评测同源 | Run 同时关联 Span、断言、分数与版本 |
| 发布门禁 | 评测结论直接产出可执行 Gate |
| OpenClaw 深度 | 原生识别 tool loop、failover、memory pressure 和 harness |
| 单页面体验 | 自研 Trace、实验和对比界面，不暴露基础设施 UI |

## 11. 落地路线

### 阶段一：统一数据与单页面

- 原生接收 OTLP/HTTP；
- 建立 Run、Trace、Span 和业务结论模型；
- 完成总览、Trace Explorer 和瀑布图；
- 精确区分运行完成与业务成功；
- 单端口交付。

达标标志：不打开其他工具即可完成一次运行的观测、定位和人工判定。

### 阶段二：可重复实验

- 接入 Gateway traceparent；
- 评测集和多版本 Variant；
- 沙箱终态断言；
- 多次运行；
- 逐 Case 对齐和 Trace Diff。

达标标志：同一评测集可以稳定比较两个版本，结论可追溯到单个 Span。

### 阶段三：发布闭环

- CI/CD Gate API；
- 在线 Trace 采样与异常检测；
- Bad Case 一键回流；
- 权限、脱敏、审计与保留策略；
- PostgreSQL/ClickHouse 生产存储。

达标标志：至少一次真实回归被平台在发布前拦截，并能说明失败 Case、失败环节和效率变化。

## 12. 需要避免的方向

- 不把所有指标混成一个总分；
- 不使用 Runtime completed 代替业务成功；
- 不用“最新 Trace”关联并发 Run；
- 不把 LLM Judge 当作事实校验器；
- 不在宿主机执行用户提供的 Shell 断言；
- 不默认采集 Prompt 和工具原文；
- 不先建设几十个评估器，再寻找实际业务 Case；
- 不让基础设施 UI 决定产品信息架构。

## 13. 最终判断

AIOS 的核心价值不是再造一个 Trace Viewer，而是把 Agent 的执行证据、业务结果、效率成本和版本实验放进同一个 Run 模型中。

OpenTelemetry 让数据能够被采集；终态断言让成功能够被证明；实验与门禁让结论能够影响发布。三者闭环后，平台才同时具备“可观测”和“可评测”，而不是一个更漂亮的监控看板。
