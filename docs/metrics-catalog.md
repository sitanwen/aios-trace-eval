# AIOS 指标目录

所有指标在 AIOS 控制台中展示。OpenTelemetry 是采集协议，平台直接从 Span 规范化并存储运行指标，不要求用户访问第三方观测界面。

## Run 身份

| 字段 | 来源 |
|---|---|
| trace_id | OTLP Trace |
| run_id | `aios.run.id`，缺失时暂以 trace_id 代替 |
| case_id | `aios.case.id` |
| experiment_id | `aios.experiment.id` |
| agent version | `aios.agent.version` |
| model/provider | `gen_ai.*` 或 `openclaw.*` |

## 运行指标

| 指标 | 口径 |
|---|---|
| 端到端耗时 | 最早 Span 开始到最晚 Span 结束 |
| TTFT | 首个模型调用的 `time_to_first_byte_ms` |
| 模型调用次数 | inference/chat/invoke_agent Span 数量 |
| 工具调用次数 | `openclaw.tool.execution` / `execute_tool` Span 数量 |
| 工具错误次数 | error 或 blocked 工具 Span |
| 工具循环 | `openclaw.tool.loop` Span |
| Failover | `openclaw.model.failover` Span |
| runtime status | Span error/outcome 汇总 |

## Token 与成本

| 指标 | 口径 |
|---|---|
| input/output | 优先按 model.call 逐次 usage 求和 |
| cache read/write | OpenClaw 扩展 usage 或 GenAI usage |
| reasoning | provider/OpenClaw reasoning usage |
| total | provider total，缺失时按各桶求和 |
| cost | OpenClaw 估算成本；不可替代供应商账单 |
| Token/成功任务 | business passed Run 的 Token 总量 ÷ passed Run 数 |

## 质量指标

| 指标 | 说明 |
|---|---|
| 业务成功率 | business passed ÷ 全部 Run |
| 规则通过率 | 通过的确定性规则 ÷ 已执行规则 |
| 终态断言通过率 | 通过的业务终态断言 ÷ 已执行断言 |
| 工具参数准确率 | 参数字段断言通过数 ÷ 参数断言总数 |
| 版本胜率 | 同 Case 下候选版本优于基线的比例 |
| 稳定成功率 | 同一 Case 多次执行的通过分布 |

## 风险指标

- 越权工具调用；
- Sandbox blocked；
- 敏感信息规则命中；
- 不当执行与拒答错误；
- 高风险断言失败。

## 口径约束

1. runtime completed 不能当作业务成功。
2. 未采集 Token 不能记为零。
3. 失败 Run 的成本单独展示，不能摊入成功任务成本。
4. Trace 未完整到达时标记 incomplete，不提前给出最终聚合。
5. LLM Judge 分数必须携带裁判模型、Prompt 版本和自身成本。
