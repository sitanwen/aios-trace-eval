"""Product metric catalog. Missing evidence never becomes a fabricated value."""
from __future__ import annotations

DIMENSIONS = [
    {"key": "result", "name": "结果", "question": "任务是否真正完成，输出是否可用"},
    {"key": "process", "name": "过程", "question": "规划、工具与执行路径是否合理"},
    {"key": "efficiency", "name": "效率", "question": "完成任务的时间与资源代价是否可接受"},
    {"key": "risk", "name": "风险", "question": "是否发生越权、泄露或不当行为"},
]

def m(key, name, dimension, source, description, gate=False):
    return {"key": key, "name": name, "dimension": dimension, "source": source,
            "description": description, "gate_recommended": gate}

METRICS = [
    m("task_success_rate", "任务成功率", "result", "assertion", "全部业务终态硬条件通过的任务占比。", True),
    m("subgoal_completion", "子目标完成率", "result", "assertion", "已完成子目标占必需子目标总数的比例。"),
    m("rubric_pass_rate", "Rubric 通过率", "result", "evaluator", "把开放式质量要求拆成二元检查项后计算通过率。"),
    m("factual_accuracy", "事实与断言准确率", "result", "evaluator", "输出事实与参考事实或业务数据一致。", True),
    m("faithfulness", "答案忠实性", "result", "evaluator", "输出结论可由检索证据或上下文支撑。"),
    m("multi_turn_consistency", "多轮一致性", "result", "evaluator", "后续回答不与会话中已确认事实冲突。"),
    m("pass_at_k", "稳定成功率 Pass@k / Pass^k", "result", "experiment", "同一 Case 重复运行，衡量至少成功一次与每次都成功。"),
    m("trajectory_quality", "轨迹质量 / 匹配率", "process", "evaluator", "关键步骤出现、顺序合理，且没有偏离任务目标。"),
    m("tool_selection_accuracy", "工具选择准确率", "process", "assertion", "应调用与不应调用的工具选择均符合预期。", True),
    m("tool_argument_accuracy", "工具参数准确率", "process", "assertion", "参数必填项、类型、值域及业务约束逐项通过。", True),
    m("tool_success_rate", "工具执行成功率", "process", "otel", "成功 Tool Span 数占全部 Tool Span 的比例。"),
    m("tool_loop_rate", "循环 / 无效重复调用率", "process", "otel_rule", "相同语义动作在没有新增 Observation 时重复执行。"),
    m("observation_gain", "Observation 信息增益", "process", "evaluator", "新观察是否增加完成任务所需的信息。"),
    m("retrieval_effectiveness", "检索有效性", "process", "evaluator", "使用 Recall@k、MRR 或证据覆盖率评估检索结果。"),
    m("intermediate_artifact_quality", "中间产物质量", "process", "assertion", "计划、文件、代码等阶段产物满足验收条件。"),
    m("latency_percentiles", "端到端耗时 P50 / P95 / P99", "efficiency", "otel", "按 Run 首末 Span 计算耗时并做分位数聚合。"),
    m("ttft", "首 Token 延迟 TTFT", "efficiency", "otel", "模型开始调用至首个输出 Token 的时间。"),
    m("token_breakdown", "Token 分类", "efficiency", "otel", "分别统计输入、输出、缓存读写与 reasoning Token。"),
    m("tokens_per_success", "每成功任务 Token", "efficiency", "derived", "总 Token 除以业务成功任务数，失败成本另列。"),
    m("cost_per_success", "每成功任务成本", "efficiency", "derived", "模型与评估器成本之和除以业务成功任务数。"),
    m("model_tool_share", "模型 / 工具耗时占比", "efficiency", "otel", "模型与工具 Span 时长分别占端到端耗时的比例。"),
    m("output_tps", "输出吞吐 TPS", "efficiency", "otel", "输出 Token 数除以生成阶段秒数。"),
    m("evaluator_overhead", "评估器 Token / 时延", "efficiency", "evaluator", "单独记录 Judge 消耗，避免混入被测 Agent。"),
    m("runtime_memory", "运行内存 / 显存", "efficiency", "runtime", "由 OpenClaw 宿主采集峰值 RSS、Heap 与 GPU 显存。"),
    m("throughput", "并发 / QPS", "efficiency", "runtime", "窗口内并发任务数、请求率和排队时间。"),
    m("unauthorized_tool_rate", "越权操作率", "risk", "assertion", "未经授权的高风险工具调用任务占比。", True),
    m("harmful_output_rate", "有害输出率", "risk", "evaluator", "输出触发安全规则或安全评估器的任务占比。", True),
    m("sensitive_data_leak", "敏感信息泄露率", "risk", "rule", "输出、工具参数或日志命中敏感信息规则。", True),
    m("refusal_accuracy", "拒答正确率", "risk", "evaluator", "该拒绝时正确拒绝、可回答时没有错误拒绝。", True),
    m("blocked_action_rate", "受阻高风险动作率", "risk", "otel_rule", "被 Sandbox、权限或审批机制阻止的动作占比。"),
]

SOURCE_LABELS = {"otel": "自动采集", "otel_rule": "采集 + 规则", "derived": "自动计算",
                 "assertion": "数据集断言", "rule": "规则检测", "evaluator": "评估器",
                 "experiment": "重复实验", "runtime": "运行时探针"}

VENDORS = [
    {"name": "阿里 Evolution", "focus": "观测评测同源", "strength": "Trace 回流评测集、轨迹评估、Observation 增益", "gap": "多版本对比、成本和资源指标偏弱"},
    {"name": "字节 CozeLoop", "focus": "实验与横向对比", "strength": "逐样本对比、结构化轨迹、重复工具调用规则", "gap": "裁判模型受限，缺成本与发布门禁"},
    {"name": "华为 AgentArts", "focus": "离线 + 在线治理", "strength": "评估器最广，安全、拒答、多轮与在线采样完善", "gap": "门禁和告警依赖外部产品"},
    {"name": "腾讯 ADP + AgentObs", "focus": "评测与生产观测", "strength": "成本金额、TPS、分位时延与四类 Token", "gap": "评测和观测割裂，过程评测较弱"},
]

def catalog():
    return {"dimensions": DIMENSIONS,
            "metrics": [{**x, "source_label": SOURCE_LABELS[x["source"]]} for x in METRICS],
            "vendors": VENDORS,
            "principles": ["运行完成不等于业务成功", "能用断言判定的指标不交给模型打分",
                           "缺少证据时显示未采集，不以零代替", "安全与数据正确性建议一票否决"]}
