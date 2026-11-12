"""SQLite persistence for the unified AIOS observability and evaluation console."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


# 历史记录没有 run_batch_id 时，两次执行之间超过这个间隔就视为不同的运行批次。
RUN_BATCH_GAP_NS = 30 * 60 * 1_000_000_000

# /v1/metrics 只落库资源曲线会读取的指标。OpenClaw Gateway 每秒导出约 70 条时间序列，
# 队列深度、累计计数器、耗时直方图等只写不读；全量落库会让 metric_samples
# 每天增加约 580 万行。新增读取方时，把指标名同步加入这里以及下面的查询条件。
RUNTIME_METRIC_NAMES = frozenset({
    "process.memory.rss",
    "process.cpu.utilization",
    "openclaw.memory.rss_bytes",
    "openclaw.memory.heap_used_bytes",
    "openclaw.liveness.cpu_core_ratio",
    "openclaw.phase.cpu_core_ratio",
    "openclaw.process.cpu_core_ratio",
    "openclaw.phase.cpu_total_ms",
})


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    run_id TEXT,
    service_name TEXT NOT NULL DEFAULT 'openclaw',
    ingest_source TEXT NOT NULL DEFAULT 'otlp',
    case_id TEXT,
    experiment_id TEXT,
    version TEXT,
    agent TEXT,
    provider TEXT,
    model TEXT,
    started_at_ns INTEGER NOT NULL,
    ended_at_ns INTEGER NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0,
    runtime_status TEXT NOT NULL DEFAULT 'unknown',
    business_status TEXT NOT NULL DEFAULT 'ungraded',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    tool_errors INTEGER NOT NULL DEFAULT 0,
    tool_loops INTEGER NOT NULL DEFAULT 0,
    failovers INTEGER NOT NULL DEFAULT 0,
    ttft_ms REAL,
    score REAL,
    input_text TEXT,
    output_text TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at_ns DESC);
CREATE INDEX IF NOT EXISTS idx_traces_experiment ON traces(experiment_id, version);
CREATE INDEX IF NOT EXISTS idx_traces_case ON traces(case_id);
CREATE INDEX IF NOT EXISTS idx_traces_run_id ON traces(run_id);

CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT,
    started_at_ns INTEGER NOT NULL,
    ended_at_ns INTEGER NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unset',
    attributes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, started_at_ns);

CREATE TABLE IF NOT EXISTS metric_samples (
    service_name TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    metric_name TEXT NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    value REAL NOT NULL,
    count INTEGER,
    sum_value REAL,
    min_value REAL,
    max_value REAL,
    unit TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (service_name, metric_name, timestamp_ns, attributes_json)
);

CREATE INDEX IF NOT EXISTS idx_metric_samples_window
ON metric_samples(service_name, timestamp_ns, metric_name);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    baseline_version TEXT,
    candidate_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    evaluator TEXT NOT NULL,
    evaluator_type TEXT NOT NULL,
    passed INTEGER,
    score REAL,
    reason TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL,
    protocol TEXT NOT NULL DEFAULT 'openclaw_gateway',
    runtime_type TEXT NOT NULL DEFAULT 'openclaw',
    execution_mode TEXT NOT NULL DEFAULT 'gateway',
    executable TEXT NOT NULL DEFAULT '',
    working_directory TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    auth_type TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'configured',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_cases (
    case_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES eval_datasets(dataset_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    input TEXT NOT NULL,
    expected_output TEXT NOT NULL DEFAULT '',
    assertions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_eval_cases_dataset ON eval_cases(dataset_id, created_at);

CREATE TABLE IF NOT EXISTS judge_models (
    judge_model_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'configured',
    last_tested_at TEXT,
    last_latency_ms REAL,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evaluators (
    evaluator_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    evaluator_type TEXT NOT NULL,
    dimension TEXT NOT NULL,
    score_min REAL NOT NULL DEFAULT 0,
    score_max REAL NOT NULL DEFAULT 1,
    pass_threshold REAL NOT NULL DEFAULT 1,
    judge_model_id TEXT REFERENCES judge_models(judge_model_id),
    prompt_template TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_experiments (
    experiment_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES eval_agents(agent_id),
    dataset_id TEXT NOT NULL REFERENCES eval_datasets(dataset_id),
    judge_model_id TEXT REFERENCES judge_models(judge_model_id),
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS experiment_evaluators (
    experiment_id TEXT NOT NULL REFERENCES eval_experiments(experiment_id) ON DELETE CASCADE,
    evaluator_id TEXT NOT NULL REFERENCES evaluators(evaluator_id),
    weight REAL NOT NULL DEFAULT 1,
    is_gate INTEGER NOT NULL DEFAULT 0,
    field_mapping_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (experiment_id, evaluator_id)
);

CREATE TABLE IF NOT EXISTS eval_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    run_batch_id TEXT,
    experiment_id TEXT NOT NULL REFERENCES eval_experiments(experiment_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES eval_cases(case_id),
    trace_id TEXT REFERENCES traces(trace_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_type TEXT,
    error_message TEXT,
    actual_output TEXT,
    score REAL,
    started_at_ns INTEGER NOT NULL,
    ended_at_ns INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_eval_attempts_experiment
ON eval_attempts(experiment_id, started_at_ns);

CREATE INDEX IF NOT EXISTS idx_eval_attempts_case
ON eval_attempts(case_id, started_at_ns);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES eval_experiments(experiment_id) ON DELETE CASCADE,
    total INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    started_at_ns INTEGER NOT NULL,
    ended_at_ns INTEGER
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_experiment
ON eval_runs(experiment_id, started_at_ns DESC);
"""


BUILTIN_EVALUATORS = [
    {
        "evaluator_id": "builtin_output_exact",
        "name": "精确答案匹配",
        "description": "实际输出与参考答案完全一致，适合确定性短答案。",
        "evaluator_type": "rule",
        "dimension": "result",
        "pass_threshold": 1,
        "config_json": json.dumps({"operator": "output_exact"}, ensure_ascii=False),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_task_completion",
        "name": "任务完成度",
        "description": "由 Judge 按任务目标、参考答案和实际输出判断完成程度。",
        "evaluator_type": "llm",
        "dimension": "result",
        "pass_threshold": 0.8,
        "config_json": json.dumps({"rubric": "task_completion"}, ensure_ascii=False),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_instruction_following",
        "name": "指令遵循",
        "description": "检查格式、范围和用户显式约束是否全部满足。",
        "evaluator_type": "llm",
        "dimension": "result",
        "pass_threshold": 0.8,
        "config_json": json.dumps({"rubric": "instruction_following"}, ensure_ascii=False),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_token_budget",
        "name": "Token 预算",
        "description": "按 Trace 的 total_tokens 与配置上限进行确定性判定。",
        "evaluator_type": "rule",
        "dimension": "efficiency",
        "pass_threshold": 1,
        "config_json": json.dumps({"field": "total_tokens", "operator": "le", "value": 10000}),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_latency_budget",
        "name": "端到端时延预算",
        "description": "检查 Run 总耗时是否低于配置阈值。",
        "evaluator_type": "rule",
        "dimension": "efficiency",
        "pass_threshold": 1,
        "config_json": json.dumps({"field": "duration_ms", "operator": "le", "value": 30000}),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_tool_success",
        "name": "工具执行成功",
        "description": "工具错误数必须为零，可作为过程质量门禁。",
        "evaluator_type": "rule",
        "dimension": "process",
        "pass_threshold": 1,
        "config_json": json.dumps({"field": "tool_errors", "operator": "eq", "value": 0}),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_tool_loop",
        "name": "循环调用检测",
        "description": "检测无信息增益的工具重复与 Action 循环。",
        "evaluator_type": "trajectory_rule",
        "dimension": "process",
        "pass_threshold": 1,
        "config_json": json.dumps({"field": "tool_loops", "operator": "eq", "value": 0}),
        "is_builtin": 1,
    },
    {
        "evaluator_id": "builtin_safety",
        "name": "安全与越权",
        "description": "检查有害输出、敏感信息泄露与未授权高风险操作。",
        "evaluator_type": "llm",
        "dimension": "risk",
        "pass_threshold": 1,
        "config_json": json.dumps({"rubric": "safety", "gate": True}, ensure_ascii=False),
        "is_builtin": 1,
    },
]


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connection() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(eval_experiments)")}
            if "judge_model_id" not in columns:
                db.execute(
                    "ALTER TABLE eval_experiments ADD COLUMN judge_model_id TEXT "
                    "REFERENCES judge_models(judge_model_id)"
                )
            judge_columns = {row[1] for row in db.execute("PRAGMA table_info(judge_models)")}
            for name, column_type in (
                ("last_tested_at", "TEXT"),
                ("last_latency_ms", "REAL"),
                ("last_error", "TEXT"),
            ):
                if name not in judge_columns:
                    db.execute(f"ALTER TABLE judge_models ADD COLUMN {name} {column_type}")
            trace_columns = {row[1] for row in db.execute("PRAGMA table_info(traces)")}
            for name in ("input_text", "output_text"):
                if name not in trace_columns:
                    db.execute(f"ALTER TABLE traces ADD COLUMN {name} TEXT")
            if "ingest_source" not in trace_columns:
                db.execute("ALTER TABLE traces ADD COLUMN ingest_source TEXT NOT NULL DEFAULT 'otlp'")
            attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(eval_attempts)")}
            if "run_batch_id" not in attempt_columns:
                db.execute("ALTER TABLE eval_attempts ADD COLUMN run_batch_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_eval_attempts_run_batch "
                       "ON eval_attempts(run_batch_id)")
            metric_columns = {row[1] for row in db.execute("PRAGMA table_info(metric_samples)")}
            if "run_id" not in metric_columns:
                db.execute("ALTER TABLE metric_samples ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_metric_samples_run "
                       "ON metric_samples(run_id, timestamp_ns, metric_name)")
            agent_columns = {row[1] for row in db.execute("PRAGMA table_info(eval_agents)")}
            for name, definition in (
                ("runtime_type", "TEXT NOT NULL DEFAULT 'openclaw'"),
                ("execution_mode", "TEXT NOT NULL DEFAULT 'gateway'"),
                ("executable", "TEXT NOT NULL DEFAULT ''"),
                ("working_directory", "TEXT NOT NULL DEFAULT ''"),
                ("config_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in agent_columns:
                    db.execute(f"ALTER TABLE eval_agents ADD COLUMN {name} {definition}")
            db.execute("""
                UPDATE eval_agents
                SET runtime_type=CASE
                    WHEN protocol='openclaw_gateway' THEN 'openclaw'
                    ELSE protocol
                END
                WHERE protocol IN ('openclaw_gateway','openclaw','claude_code','dsh','generic')
            """)
            db.execute("""
                UPDATE eval_agents SET execution_mode='local_cli'
                WHERE runtime_type IN ('claude_code','dsh') AND execution_mode='gateway'
            """)
            for evaluator in BUILTIN_EVALUATORS:
                db.execute("""
                    INSERT OR IGNORE INTO evaluators(
                        evaluator_id,name,description,evaluator_type,dimension,
                        pass_threshold,config_json,is_builtin
                    ) VALUES(
                        :evaluator_id,:name,:description,:evaluator_type,:dimension,
                        :pass_threshold,:config_json,:is_builtin
                    )
                """, evaluator)
            # Preserve experiment history created before Attempt records existed.
            # The insert is idempotent and never rewrites a real Attempt.
            db.execute("""
                INSERT OR IGNORE INTO eval_attempts(
                    attempt_id,run_id,experiment_id,case_id,trace_id,status,
                    score,started_at_ns,ended_at_ns
                )
                SELECT 'attempt_legacy_' || t.trace_id,
                       CASE WHEN t.run_id IS NULL OR t.run_id='' THEN t.trace_id ELSE t.run_id END,
                       t.experiment_id,t.case_id,t.trace_id,
                       CASE
                           WHEN t.runtime_status='error' THEN 'runtime_error'
                           WHEN t.business_status='passed' THEN 'passed'
                           WHEN t.business_status='failed' THEN 'failed'
                           ELSE 'ungraded'
                       END,
                       t.score,t.started_at_ns,t.ended_at_ns
                FROM traces t
                JOIN eval_experiments e ON e.experiment_id=t.experiment_id
                JOIN eval_cases c ON c.case_id=t.case_id AND c.dataset_id=e.dataset_id
                WHERE NOT EXISTS(
                    SELECT 1 FROM eval_attempts a WHERE a.trace_id=t.trace_id
                )
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def upsert_trace(self, trace: Dict[str, Any]) -> None:
        columns = list(trace)
        values = [trace[column] for column in columns]
        preserve_when_missing = {"case_id", "experiment_id", "version", "agent", "provider", "model"}
        updates = []
        for column in columns:
            if column == "trace_id":
                continue
            if column == "run_id":
                updates.append(
                    "run_id=CASE WHEN excluded.run_id=excluded.trace_id "
                    "THEN COALESCE(traces.run_id, excluded.run_id) ELSE excluded.run_id END"
                )
            elif column in preserve_when_missing:
                updates.append(f"{column}=COALESCE(excluded.{column}, traces.{column})")
            else:
                updates.append(f"{column}=excluded.{column}")
        update = ", ".join(updates)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO traces ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(trace_id) DO UPDATE SET {update}"
        )
        with self._lock, self.connection() as db:
            db.execute(sql, values)

    def upsert_spans(self, spans: Iterable[Dict[str, Any]]) -> None:
        rows = list(spans)
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ", ".join("?" for _ in columns)
        update = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "span_id")
        sql = (
            f"INSERT INTO spans ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(span_id) DO UPDATE SET {update}"
        )
        with self._lock, self.connection() as db:
            db.executemany(sql, [[row[column] for column in columns] for row in rows])

    def spans_for_summary(self, trace_id: str) -> List[Dict[str, Any]]:
        with self.connection() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM spans WHERE trace_id=? ORDER BY started_at_ns", (trace_id,)
            ).fetchall()]
        for row in rows:
            row["_attributes"] = json.loads(row["attributes_json"] or "{}")
        return rows

    def get_trace_by_run_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("""
                SELECT trace_id FROM traces WHERE run_id=?
                ORDER BY (total_tokens > 0) DESC, total_tokens DESC, ended_at_ns DESC
                LIMIT 1
            """, (run_id,)).fetchone()
        return self.get_trace(row["trace_id"]) if row else None

    def create_attempt(self, experiment_id: str, case_id: str, run_id: str,
                       run_batch_id: Optional[str] = None) -> Dict[str, Any]:
        attempt = {
            "attempt_id": f"attempt_{run_id.removeprefix('run_')}",
            "run_id": run_id,
            "run_batch_id": run_batch_id,
            "experiment_id": experiment_id,
            "case_id": case_id,
            "status": "running",
            "started_at_ns": time.time_ns(),
        }
        with self._lock, self.connection() as db:
            valid = db.execute("""
                SELECT 1 FROM eval_experiments e
                JOIN eval_cases c ON c.dataset_id=e.dataset_id
                WHERE e.experiment_id=? AND c.case_id=?
            """, (experiment_id, case_id)).fetchone()
            if not valid:
                raise KeyError("experiment or case not found")
            db.execute("""
                INSERT INTO eval_attempts(
                    attempt_id,run_id,run_batch_id,experiment_id,case_id,status,started_at_ns
                ) VALUES(:attempt_id,:run_id,:run_batch_id,:experiment_id,:case_id,:status,:started_at_ns)
            """, attempt)
        return attempt

    def update_attempt(
        self, run_id: str, status: str, *, trace_id: Optional[str] = None,
        actual_output: Optional[str] = None, score: Optional[float] = None,
        error_type: Optional[str] = None, error_message: Optional[str] = None,
    ) -> bool:
        terminal = status in {
            "passed", "failed", "runtime_error", "telemetry_missing", "evaluation_error"
        }
        with self._lock, self.connection() as db:
            changed = db.execute("""
                UPDATE eval_attempts SET status=?,
                    trace_id=COALESCE(?, trace_id),
                    actual_output=COALESCE(?, actual_output),
                    score=COALESCE(?, score),
                    error_type=?, error_message=?,
                    ended_at_ns=CASE WHEN ? THEN COALESCE(ended_at_ns, ?) ELSE ended_at_ns END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE run_id=?
            """, (
                status, trace_id, actual_output, score, error_type,
                error_message[:2000] if error_message else None,
                1 if terminal else 0, time.time_ns(), run_id,
            )).rowcount
        return bool(changed)

    def create_run(self, run_id: str, experiment_id: str, total: int) -> None:
        with self._lock, self.connection() as db:
            db.execute("""
                INSERT INTO eval_runs(run_id, experiment_id, total, status, started_at_ns)
                VALUES(?, ?, ?, 'running', ?)
            """, (run_id, experiment_id, total, time.time_ns()))

    def update_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        terminal = status in {"completed", "failed"}
        with self._lock, self.connection() as db:
            db.execute("""
                UPDATE eval_runs SET status=?, error=?,
                    ended_at_ns=CASE WHEN ? THEN COALESCE(ended_at_ns, ?) ELSE ended_at_ns END
                WHERE run_id=?
            """, (status, error[:800] if error else None,
                  1 if terminal else 0, time.time_ns(), run_id))

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT r.run_id, r.experiment_id, r.total, r.status, r.error,
                       r.started_at_ns, r.ended_at_ns,
                       e.name AS experiment_name,
                       COALESCE(SUM(CASE WHEN a.status='passed' THEN 1 ELSE 0 END), 0) AS passed,
                       COALESCE(SUM(CASE WHEN a.status IN (
                           'failed','runtime_error','telemetry_missing','evaluation_error'
                       ) THEN 1 ELSE 0 END), 0) AS failed
                FROM eval_runs r
                JOIN eval_experiments e ON e.experiment_id = r.experiment_id
                LEFT JOIN eval_attempts a ON a.run_batch_id = r.run_id
                GROUP BY r.run_id
                ORDER BY r.started_at_ns DESC
                LIMIT ?
            """, (limit,)).fetchall()]
        # 运行序号与实验抽屉里的批次序号保持一致（含历史伪批次）
        sequences: Dict[tuple, int] = {}
        for experiment_id in {row["experiment_id"] for row in rows}:
            for batch in self.experiment_run_batches(experiment_id):
                sequences[(experiment_id, batch["run_id"])] = batch["sequence"]
        for row in rows:
            terminal = row["passed"] + row["failed"]
            row["terminal"] = terminal
            row["pass_rate"] = (row["passed"] / terminal) if terminal else None
            row["run_sequence"] = sequences.get((row["experiment_id"], row["run_id"]))
        return rows

    def upsert_metric_samples(self, samples: Iterable[Dict[str, Any]]) -> None:
        rows = list(samples)
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ", ".join("?" for _ in columns)
        update = ", ".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"service_name", "metric_name", "timestamp_ns", "attributes_json"}
        )
        sql = (
            f"INSERT INTO metric_samples ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(service_name, metric_name, timestamp_ns, attributes_json) "
            f"DO UPDATE SET {update}"
        )
        with self._lock, self.connection() as db:
            db.executemany(sql, [[row[column] for column in columns] for row in rows])

    def _runtime_metrics(self, db: sqlite3.Connection, trace: Dict[str, Any]) -> Dict[str, Any]:
        padding_ns = 10_000_000_000
        attempt = db.execute(
            "SELECT run_id FROM eval_attempts WHERE trace_id=? ORDER BY started_at_ns DESC LIMIT 1",
            (trace["trace_id"],),
        ).fetchone()
        run_id = attempt["run_id"] if attempt else ""
        start_ns = trace["started_at_ns"] - padding_ns
        end_ns = trace["ended_at_ns"] + padding_ns
        select_columns = """
            SELECT run_id, metric_name, timestamp_ns, value, count, sum_value,
                   min_value, max_value, unit, attributes_json
            FROM metric_samples
        """
        metric_filter = """AND metric_name IN (
                'process.memory.rss',
                'process.cpu.utilization',
                'openclaw.memory.rss_bytes',
                'openclaw.memory.heap_used_bytes',
                'openclaw.liveness.cpu_core_ratio',
                'openclaw.phase.cpu_core_ratio',
                'openclaw.process.cpu_core_ratio',
                'openclaw.phase.cpu_total_ms'
              )
            ORDER BY timestamp_ns"""
        # 不用 OR 合并两个来源：run_id='' 会命中上百万行，导致索引扫描退化成秒级查询。
        rows: List[Dict[str, Any]] = []
        exact_run_rows: List[Dict[str, Any]] = []
        if run_id:
            exact_run_rows = [dict(row) for row in db.execute(
                select_columns + " WHERE run_id=? AND timestamp_ns BETWEEN ? AND ? " + metric_filter,
                (run_id, start_ns, end_ns),
            ).fetchall()]
            rows = exact_run_rows
        if not rows:
            rows = [dict(row) for row in db.execute(
                select_columns + " WHERE run_id='' AND service_name=? AND timestamp_ns BETWEEN ? AND ? " + metric_filter,
                (trace["service_name"], start_ns, end_ns),
            ).fetchall()]

        def source_of(row: Dict[str, Any]) -> str:
            try:
                return json.loads(row["attributes_json"]).get("aios.resource.source", "otel")
            except (TypeError, ValueError):
                return "otel"

        def summary(name: str) -> Optional[Dict[str, Any]]:
            matched = [row for row in rows if row["metric_name"] == name]
            if not exact_run_rows:
                matched = [row for row in matched
                           if trace["started_at_ns"] <= row["timestamp_ns"] <= trace["ended_at_ns"]]
            if not matched:
                return None
            weighted_count = sum(row["count"] or 1 for row in matched)
            weighted_sum = sum(
                row["sum_value"] if row["sum_value"] is not None else row["value"]
                for row in matched
            )
            peaks = [
                row["max_value"] if row["max_value"] is not None else row["value"]
                for row in matched
            ]
            return {
                "average": weighted_sum / weighted_count,
                "peak": max(peaks),
                "sample_count": weighted_count,
                "point_count": len(matched),
                "unit": matched[0]["unit"],
                "source": source_of(matched[0]),
            }

        def first_summary(*names: str) -> Optional[Dict[str, Any]]:
            for name in names:
                result = summary(name)
                if result:
                    return result
            return None

        def combined_summary(*names: str) -> Optional[Dict[str, Any]]:
            values = [summary(name) for name in names]
            values = [value for value in values if value]
            if not values:
                return None
            return {
                "average": sum(value["average"] * value["sample_count"] for value in values)
                / sum(value["sample_count"] for value in values),
                "peak": max(value["peak"] for value in values),
                "sample_count": sum(value["sample_count"] for value in values),
                "point_count": sum(value["point_count"] for value in values),
                "unit": values[0]["unit"],
            }

        def series(name: str) -> Dict[str, Any]:
            matched = [row for row in rows if row["metric_name"] == name]
            selected = matched if exact_run_rows else [
                row for row in matched
                if trace["started_at_ns"] <= row["timestamp_ns"] <= trace["ended_at_ns"]
            ]
            unique: Dict[int, Dict[str, Any]] = {row["timestamp_ns"]: row for row in selected}
            series_start = min(unique, default=trace["started_at_ns"])
            series_end = max(unique, default=trace["ended_at_ns"])
            points = [{
                "offset_ms": round((timestamp - series_start) / 1_000_000, 3),
                "value": row["value"],
            } for timestamp, row in sorted(unique.items())]
            return {
                "points": points,
                "trace_duration_ms": round((series_end - series_start) / 1_000_000, 3),
                "source": source_of(selected[0]) if selected else "otel",
            }

        def first_series(*names: str) -> Dict[str, Any]:
            for name in names:
                result = series(name)
                if result["points"]:
                    return result
            return {"points": [], "trace_duration_ms": trace["duration_ms"], "source": "otel"}

        scope = "process_tree" if exact_run_rows else "gateway_process"
        return {
            "scope": scope,
            "sample_scope": "run" if exact_run_rows else "trace_interval",
            "service_name": trace["service_name"],
            "window_padding_ms": 10000,
            "rss_bytes": first_summary("process.memory.rss", "openclaw.memory.rss_bytes"),
            "rss_series": first_series("process.memory.rss", "openclaw.memory.rss_bytes"),
            "heap_used_bytes": summary("openclaw.memory.heap_used_bytes"),
            "cpu_core_ratio": first_summary(
                "process.cpu.utilization",
                "openclaw.process.cpu_core_ratio",
                "openclaw.phase.cpu_core_ratio",
                "openclaw.liveness.cpu_core_ratio"
            ),
            "cpu_series": first_series(
                "process.cpu.utilization",
                "openclaw.process.cpu_core_ratio",
                "openclaw.phase.cpu_core_ratio",
                "openclaw.liveness.cpu_core_ratio",
            ),
            "cpu_total_ms": summary("openclaw.phase.cpu_total_ms"),
        }

    def experiment_run_batches(self, experiment_id: str) -> List[Dict[str, Any]]:
        """把一次执行聚成一个批次：同一 run_batch_id 的 Case 属于同一次运行。

        历史记录没有 run_batch_id，按开始时间间隔归并成伪批次，避免不同次运行混在一起。
        """
        with self.connection() as db:
            official = [dict(row) for row in db.execute("""
                SELECT run_id, total, status, error, started_at_ns, ended_at_ns
                FROM eval_runs WHERE experiment_id=? ORDER BY started_at_ns
            """, (experiment_id,)).fetchall()]
            attempts = [dict(row) for row in db.execute("""
                SELECT a.run_id, a.run_batch_id, a.case_id, a.status, a.error_type,
                       a.error_message, a.score, a.started_at_ns, a.ended_at_ns,
                       a.trace_id, c.name AS case_name
                FROM eval_attempts a JOIN eval_cases c ON c.case_id=a.case_id
                WHERE a.experiment_id=? ORDER BY a.started_at_ns
            """, (experiment_id,)).fetchall()]
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        legacy: List[Dict[str, Any]] = []
        for attempt in attempts:
            batch_id = attempt.get("run_batch_id")
            if batch_id:
                grouped.setdefault(batch_id, []).append(attempt)
            else:
                legacy.append(attempt)
        batches: List[Dict[str, Any]] = []
        for run in official:
            batches.append(self._batch_payload(run["run_id"], grouped.pop(run["run_id"], []), run))
        for batch_id, items in grouped.items():
            batches.append(self._batch_payload(batch_id, items))
        cluster: List[Dict[str, Any]] = []
        seen_cases: set = set()
        last_ns: Optional[int] = None
        for attempt in legacy:
            # 同一批里每个 Case 只跑一次；Case 重复出现或间隔过久，说明进入了新的一次运行
            if cluster and (
                attempt["started_at_ns"] - (last_ns or attempt["started_at_ns"]) > RUN_BATCH_GAP_NS
                or attempt["case_id"] in seen_cases
            ):
                batches.append(self._batch_payload(f"legacy_{cluster[0]['started_at_ns']}", cluster))
                cluster, seen_cases = [], set()
            cluster.append(attempt)
            seen_cases.add(attempt["case_id"])
            last_ns = attempt["started_at_ns"]
        if cluster:
            batches.append(self._batch_payload(f"legacy_{cluster[0]['started_at_ns']}", cluster))
        batches.sort(key=lambda item: item["started_at_ns"] or 0)
        for index, batch in enumerate(batches, start=1):
            batch["sequence"] = index
        return batches

    @staticmethod
    def _batch_payload(batch_id: str, attempts: List[Dict[str, Any]],
                       run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        run = run or {}
        terminal_status = {"passed", "failed", "runtime_error", "telemetry_missing", "evaluation_error"}
        started = min((item["started_at_ns"] for item in attempts), default=None) or run.get("started_at_ns")
        ended = run.get("ended_at_ns")
        if ended is None:
            ended = max((item.get("ended_at_ns") or item["started_at_ns"] for item in attempts), default=None)
        passed = sum(1 for item in attempts if item["status"] == "passed")
        failed = sum(1 for item in attempts if item["status"] in terminal_status - {"passed"})
        finished = sum(1 for item in attempts if item["status"] in terminal_status)
        status = run.get("status") or ("running" if finished < len(attempts) else ("failed" if failed else "completed"))
        return {
            "run_id": batch_id,
            "started_at_ns": started,
            "ended_at_ns": ended,
            "status": status,
            "error": run.get("error"),
            "total": run.get("total") or len(attempts),
            "passed": passed,
            "failed": failed,
            "finished": finished,
            "attempts": attempts,
        }

    def experiment_resource_series(self, experiment_id: str,
                                   run_batch_id: Optional[str] = None) -> Dict[str, Any]:
        """按「一次运行」的窗口产出资源曲线，默认取最近一次运行。"""
        empty = {
            "rss_series": {"points": [], "trace_duration_ms": 0, "source": "otel"},
            "cpu_series": {"points": [], "trace_duration_ms": 0, "source": "otel"},
            "window_duration_ms": 0,
        }
        batches = self.experiment_run_batches(experiment_id)
        selected = None
        if run_batch_id:
            selected = next((item for item in batches if item["run_id"] == run_batch_id), None)
        if selected is None and batches:
            selected = batches[-1]
        run_ids: List[str] = []
        start_ns: Optional[int] = None
        end_ns: Optional[int] = None
        if selected is not None:
            start_ns = selected.get("started_at_ns")
            end_ns = selected.get("ended_at_ns") or start_ns
            run_ids = [item["run_id"] for item in selected["attempts"]]
        with self.connection() as db:
            if start_ns is None:
                trace_bounds = db.execute(
                    "SELECT MIN(started_at_ns), MAX(ended_at_ns) "
                    "FROM traces WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchone()
                if not trace_bounds or trace_bounds[0] is None or trace_bounds[1] is None:
                    return empty
                start_ns, end_ns = trace_bounds
            if end_ns is None:
                end_ns = start_ns
            rows: List[Dict[str, Any]] = []
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                rows = [dict(row) for row in db.execute(f"""
                    SELECT metric_name, timestamp_ns, value, attributes_json
                    FROM metric_samples
                    WHERE run_id IN ({placeholders})
                      AND metric_name IN (
                          'process.memory.rss', 'process.cpu.utilization',
                          'openclaw.memory.rss_bytes', 'openclaw.process.cpu_core_ratio',
                          'openclaw.phase.cpu_core_ratio', 'openclaw.liveness.cpu_core_ratio'
                      )
                    ORDER BY timestamp_ns
                """, run_ids).fetchall()]
            if not rows:
                # Gateway 模式的 OTel 指标不带 run_id，退回按服务名 + 本次运行窗口匹配
                service_name = db.execute(
                    "SELECT MIN(service_name) FROM traces WHERE experiment_id=?", (experiment_id,)
                ).fetchone()[0] or "openclaw"
                rows = [dict(row) for row in db.execute("""
                    SELECT metric_name, timestamp_ns, value, attributes_json
                    FROM metric_samples
                    WHERE service_name=? AND timestamp_ns BETWEEN ? AND ?
                      AND metric_name IN (
                          'openclaw.memory.rss_bytes', 'openclaw.process.cpu_core_ratio',
                          'openclaw.phase.cpu_core_ratio', 'openclaw.liveness.cpu_core_ratio'
                      )
                    ORDER BY timestamp_ns
                """, (service_name, start_ns, end_ns)).fetchall()]
            if rows:
                start_ns = min(start_ns, rows[0]["timestamp_ns"])
                end_ns = max(end_ns, rows[-1]["timestamp_ns"])

        def build_series(*names: str) -> Dict[str, Any]:
            matched = [r for r in rows if r["metric_name"] in names]
            local = [r for r in matched
                     if '"aios.resource.source":"same_host_process"' in r["attributes_json"]]
            if local:
                matched = local
            unique: Dict[int, Dict[str, Any]] = {r["timestamp_ns"]: r for r in matched}
            points = [{
                "offset_ms": round((ts - start_ns) / 1_000_000, 3),
                "value": r["value"],
            } for ts, r in sorted(unique.items())]
            return {
                "points": points,
                "trace_duration_ms": round((end_ns - start_ns) / 1_000_000, 3),
                "source": "same_host_process" if local else "otel",
            }

        return {
            "rss_series": build_series("process.memory.rss", "openclaw.memory.rss_bytes"),
            "cpu_series": build_series(
                "process.cpu.utilization",
                "openclaw.process.cpu_core_ratio",
                "openclaw.phase.cpu_core_ratio",
                "openclaw.liveness.cpu_core_ratio",
            ),
            "window_start_ns": start_ns,
            "window_end_ns": end_ns,
            "window_duration_ms": round((end_ns - start_ns) / 1_000_000, 3),
            "run_id": selected["run_id"] if selected else None,
        }

    def overview(self) -> Dict[str, Any]:
        with self.connection() as db:
            totals = dict(db.execute("""
                SELECT COUNT(*) AS runs,
                       SUM(CASE WHEN business_status='passed' THEN 1 ELSE 0 END) AS passed,
                       SUM(CASE WHEN runtime_status='error' THEN 1 ELSE 0 END) AS errors,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost,
                       COALESCE(AVG(duration_ms), 0) AS avg_duration
                FROM traces
            """).fetchone())
            passed = totals["passed"] or 0
            totals["success_rate"] = round(passed / totals["runs"], 4) if totals["runs"] else 0
            success_tokens = db.execute(
                "SELECT COALESCE(SUM(total_tokens),0) FROM traces WHERE business_status='passed'"
            ).fetchone()[0]
            totals["tokens_per_success"] = round(success_tokens / passed, 2) if passed else None

            durations = [row[0] for row in db.execute(
                "SELECT duration_ms FROM traces ORDER BY duration_ms"
            ).fetchall()]
            totals["p95_duration"] = percentile(durations, 0.95)
            totals["p99_duration"] = percentile(durations, 0.99)
            totals["tool_calls"] = db.execute("SELECT COALESCE(SUM(tool_calls),0) FROM traces").fetchone()[0]
            totals["model_calls"] = db.execute("SELECT COALESCE(SUM(model_calls),0) FROM traces").fetchone()[0]

            trend = [dict(row) for row in db.execute("""
                SELECT date(started_at_ns / 1000000000, 'unixepoch') AS day,
                       COUNT(*) AS runs,
                       SUM(total_tokens) AS tokens,
                       AVG(duration_ms) AS duration_ms,
                       AVG(CASE WHEN business_status='passed' THEN 1.0 ELSE 0.0 END) AS success_rate
                FROM traces GROUP BY day ORDER BY day DESC LIMIT 14
            """).fetchall()][::-1]
            models = [dict(row) for row in db.execute("""
                SELECT COALESCE(model, 'unknown') AS model, COUNT(*) AS runs,
                       SUM(total_tokens) AS tokens, AVG(duration_ms) AS duration_ms,
                       AVG(CASE WHEN business_status='passed' THEN 1.0 ELSE 0.0 END) AS success_rate
                FROM traces GROUP BY model ORDER BY runs DESC LIMIT 8
            """).fetchall()]
        return {"totals": totals, "trend": trend, "models": models}

    def otlp_health(self) -> Dict[str, Any]:
        with self.connection() as db:
            last_trace_at_ns = db.execute(
                "SELECT MAX(started_at_ns) FROM traces"
            ).fetchone()[0]
            last_metric_at_ns = db.execute(
                "SELECT MAX(timestamp_ns) FROM metric_samples"
            ).fetchone()[0]
        return {
            "last_trace_at_ns": last_trace_at_ns,
            "last_metric_at_ns": last_metric_at_ns,
        }

    def list_traces(self, limit: int = 100, status: Optional[str] = None, query: str = "") -> List[Dict[str, Any]]:
        where: List[str] = []
        args: List[Any] = []
        if status:
            where.append("t.business_status = ?")
            args.append(status)
        if query:
            where.append("""(
                t.trace_id LIKE ? OR t.case_id LIKE ? OR t.model LIKE ? OR t.agent LIKE ?
                OR c.name LIKE ? OR d.name LIKE ? OR e.name LIKE ?
            )""")
            needle = f"%{query}%"
            args.extend([needle] * 7)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        args.append(min(max(limit, 1), 500))
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                f"""
                SELECT t.*, c.name AS case_name, c.dataset_id, c.input AS case_input,
                       d.name AS dataset_name, e.name AS experiment_name
                FROM traces t
                LEFT JOIN eval_cases c ON c.case_id=t.case_id
                LEFT JOIN eval_datasets d ON d.dataset_id=c.dataset_id
                LEFT JOIN eval_experiments e ON e.experiment_id=t.experiment_id
                {clause}
                ORDER BY t.started_at_ns DESC LIMIT ?
                """, args
            ).fetchall()]

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            trace_row = db.execute("""
                SELECT t.*, c.name AS case_name, c.dataset_id, c.input AS case_input,
                       d.name AS dataset_name, e.name AS experiment_name
                FROM traces t
                LEFT JOIN eval_cases c ON c.case_id=t.case_id
                LEFT JOIN eval_datasets d ON d.dataset_id=c.dataset_id
                LEFT JOIN eval_experiments e ON e.experiment_id=t.experiment_id
                WHERE t.trace_id=?
            """, (trace_id,)).fetchone()
            if not trace_row:
                return None
            spans = []
            for row in db.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY started_at_ns", (trace_id,)):
                item = dict(row)
                item["attributes"] = json.loads(item.pop("attributes_json") or "{}")
                spans.append(item)
            evals = [dict(row) for row in db.execute("""
                SELECT r.* FROM eval_results r
                JOIN (
                    SELECT evaluator, evaluator_type, MAX(id) AS latest_id
                    FROM eval_results WHERE trace_id=?
                    GROUP BY evaluator, evaluator_type
                ) latest ON latest.latest_id=r.id
                ORDER BY r.id
            """, (trace_id,)).fetchall()]
            for item in evals:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            trace = dict(trace_row)
            runtime_metrics = self._runtime_metrics(db, trace)
        return {"trace": trace, "spans": spans, "evaluations": evals, "runtime_metrics": runtime_metrics}

    def delete_trace(self, trace_id: str) -> bool:
        """Remove one exact trace; used to clean up an OTLP self-test probe."""
        with self._lock, self.connection() as db:
            changed = db.execute(
                "DELETE FROM traces WHERE trace_id=?", (trace_id,)
            ).rowcount
        return bool(changed)

    def set_verdict(self, trace_id: str, status: str, score: Optional[float], reason: str) -> bool:
        if status not in {"passed", "failed", "ungraded"}:
            raise ValueError("invalid business status")
        with self._lock, self.connection() as db:
            changed = db.execute(
                "UPDATE traces SET business_status=?, score=? WHERE trace_id=?",
                (status, score, trace_id),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO eval_results(trace_id,evaluator,evaluator_type,passed,score,reason) VALUES(?,?,?,?,?,?)",
                    (trace_id, "manual-verdict", "human", 1 if status == "passed" else 0, score, reason),
                )
        return bool(changed)

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM eval_cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["assertions"] = json.loads(item.pop("assertions_json") or "[]")
        return item

    def get_evaluator(self, evaluator_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM evaluators WHERE evaluator_id=?", (evaluator_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json") or "{}")
        return item

    def evaluate_trace(
        self,
        trace_id: str,
        case_id: str,
        results: List[Dict[str, Any]],
        passed: bool,
        score: float,
        experiment_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        input_text: Optional[str] = None,
        output_text: Optional[str] = None,
    ) -> bool:
        with self._lock, self.connection() as db:
            changed = db.execute("""
                UPDATE traces SET case_id=?, experiment_id=COALESCE(?, experiment_id),
                    agent=COALESCE(?, agent), business_status=?, score=?,
                    input_text=COALESCE(?, input_text), output_text=COALESCE(?, output_text)
                WHERE trace_id=?
            """, (
                case_id, experiment_id, agent_name, "passed" if passed else "failed", score,
                input_text, output_text, trace_id,
            )).rowcount
            if not changed:
                return False
            for result in results:
                db.execute("""
                    INSERT INTO eval_results(
                        trace_id,evaluator,evaluator_type,passed,score,reason,evidence_json
                    ) VALUES(?,?,?,?,?,?,?)
                """, (
                    trace_id, result["evaluator"], result["evaluator_type"],
                    1 if result["passed"] else 0, result["score"], result["reason"],
                    json.dumps(result.get("evidence", {}), ensure_ascii=False),
                ))
        return True

    def set_llm_verdict(
        self,
        trace_id: str,
        case_id: str,
        evaluator: str,
        passed: bool,
        score: float,
        reason: str,
        evidence: Dict[str, Any],
        input_text: Optional[str] = None,
        output_text: Optional[str] = None,
    ) -> bool:
        status = "passed" if passed else "failed"
        with self._lock, self.connection() as db:
            changed = db.execute("""
                UPDATE traces SET case_id=?, business_status=?, score=?,
                    input_text=COALESCE(?, input_text), output_text=COALESCE(?, output_text)
                WHERE trace_id=?
            """, (case_id, status, score, input_text, output_text, trace_id)).rowcount
            if changed:
                db.execute("""
                    INSERT INTO eval_results(
                        trace_id,evaluator,evaluator_type,passed,score,reason,evidence_json
                    ) VALUES(?,?,?,?,?,?,?)
                """, (
                    trace_id,
                    evaluator,
                    "llm_judge",
                    1 if passed else 0,
                    score,
                    reason,
                    json.dumps(evidence, ensure_ascii=False),
                ))
        return bool(changed)

    def experiments(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT e.*,
                       COUNT(t.trace_id) AS runs,
                       AVG(CASE WHEN t.business_status='passed' THEN 1.0 ELSE 0.0 END) AS success_rate,
                       AVG(t.total_tokens) AS avg_tokens,
                       AVG(t.duration_ms) AS avg_duration
                FROM experiments e LEFT JOIN traces t ON t.experiment_id=e.experiment_id
                GROUP BY e.experiment_id ORDER BY e.created_at DESC
            """).fetchall()]

    def compare(self, experiment_id: str) -> Dict[str, Any]:
        with self.connection() as db:
            exp = db.execute("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
            if not exp:
                return {}
            rows = [dict(row) for row in db.execute("""
                SELECT version, COUNT(*) AS runs,
                       AVG(CASE WHEN business_status='passed' THEN 1.0 ELSE 0.0 END) AS success_rate,
                       AVG(total_tokens) AS avg_tokens,
                       AVG(duration_ms) AS avg_duration,
                       AVG(score) AS avg_score
                FROM traces WHERE experiment_id=? GROUP BY version ORDER BY version
            """, (experiment_id,)).fetchall()]
        return {"experiment": dict(exp), "variants": rows}

    def list_agents(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT a.*, COUNT(t.trace_id) AS run_count,
                       MAX(t.started_at_ns) AS last_run_at_ns
                FROM eval_agents a
                LEFT JOIN traces t ON t.agent=a.name
                GROUP BY a.agent_id ORDER BY a.created_at DESC
            """).fetchall()]

    def create_agent(self, agent: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.connection() as db:
            db.execute("""
                INSERT INTO eval_agents(
                    agent_id,name,description,endpoint,protocol,runtime_type,
                    execution_mode,executable,working_directory,config_json,auth_type
                ) VALUES(
                    :agent_id,:name,:description,:endpoint,:protocol,:runtime_type,
                    :execution_mode,:executable,:working_directory,:config_json,:auth_type
                )
            """, agent)
        return agent

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM eval_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_agent(
        self, agent_id: str, agent: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self.connection() as db:
            current = db.execute(
                "SELECT name FROM eval_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if not current:
                return None
            # 历史 Trace 用 Agent 名称关联，改名时同步过去，避免运行记录断开
            if current["name"] != agent["name"]:
                db.execute(
                    "UPDATE traces SET agent=? WHERE agent=?",
                    (agent["name"], current["name"]),
                )
            db.execute("""
                UPDATE eval_agents SET
                    name=:name,
                    description=:description,
                    endpoint=:endpoint,
                    protocol=:protocol,
                    runtime_type=:runtime_type,
                    execution_mode=:execution_mode,
                    executable=:executable,
                    working_directory=:working_directory,
                    config_json=:config_json,
                    auth_type=:auth_type
                WHERE agent_id=:agent_id
            """, {**agent, "agent_id": agent_id})
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> bool:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM eval_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if not exists:
                return False
            used = db.execute(
                "SELECT COUNT(*) FROM eval_experiments WHERE agent_id=?", (agent_id,)
            ).fetchone()[0]
            if used:
                raise ValueError(f"该 Agent 正被 {used} 个评测实验使用，请先删除这些实验")
            db.execute("DELETE FROM eval_agents WHERE agent_id=?", (agent_id,))
        return True

    def list_judge_models(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT judge_model_id, name, provider, base_url, model, status,
                       CASE WHEN api_key <> '' THEN 1 ELSE 0 END AS has_api_key,
                       last_tested_at, last_latency_ms, last_error, created_at
                FROM judge_models ORDER BY created_at DESC
            """).fetchall()]

    def create_judge_model(self, judge: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.connection() as db:
            db.execute("""
                INSERT INTO judge_models(
                    judge_model_id,name,provider,base_url,model,api_key,status
                ) VALUES(
                    :judge_model_id,:name,:provider,:base_url,:model,:api_key,:status
                )
            """, judge)
        return {
            key: value for key, value in judge.items() if key != "api_key"
        } | {"has_api_key": bool(judge.get("api_key"))}

    def get_judge_model_secret(self, judge_model_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM judge_models WHERE judge_model_id=?",
                (judge_model_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_judge_model_test(
        self,
        judge_model_id: str,
        status: str,
        latency_ms: float,
        error: str = "",
    ) -> None:
        with self._lock, self.connection() as db:
            db.execute("""
                UPDATE judge_models
                SET status=?, last_tested_at=CURRENT_TIMESTAMP,
                    last_latency_ms=?, last_error=?
                WHERE judge_model_id=?
            """, (status, latency_ms, error, judge_model_id))

    def update_judge_model(
        self, judge_model_id: str, judge: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM judge_models WHERE judge_model_id=?", (judge_model_id,)
            ).fetchone()
            if not exists:
                return None
            # 编辑时留空 api_key 表示保持原 Key
            api_key = (judge.get("api_key") or "").strip()
            if api_key:
                db.execute("""
                    UPDATE judge_models SET
                        name=?, provider=?, base_url=?, model=?, api_key=?,
                        status='configured', last_tested_at=NULL,
                        last_latency_ms=NULL, last_error=NULL
                    WHERE judge_model_id=?
                """, (
                    judge["name"], judge["provider"], judge["base_url"],
                    judge["model"], api_key, judge_model_id,
                ))
            else:
                db.execute("""
                    UPDATE judge_models SET
                        name=?, provider=?, base_url=?, model=?,
                        status='configured', last_tested_at=NULL,
                        last_latency_ms=NULL, last_error=NULL
                    WHERE judge_model_id=?
                """, (
                    judge["name"], judge["provider"], judge["base_url"],
                    judge["model"], judge_model_id,
                ))
            row = db.execute("""
                SELECT judge_model_id, name, provider, base_url, model, status,
                       CASE WHEN api_key <> '' THEN 1 ELSE 0 END AS has_api_key,
                       last_tested_at, last_latency_ms, last_error, created_at
                FROM judge_models WHERE judge_model_id=?
            """, (judge_model_id,)).fetchone()
        return dict(row) if row else None

    def delete_judge_model(self, judge_model_id: str) -> bool:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM judge_models WHERE judge_model_id=?", (judge_model_id,)
            ).fetchone()
            if not exists:
                return False
            used_by_evaluators = db.execute(
                "SELECT COUNT(*) FROM evaluators WHERE judge_model_id=?",
                (judge_model_id,),
            ).fetchone()[0]
            used_by_experiments = db.execute(
                "SELECT COUNT(*) FROM eval_experiments WHERE judge_model_id=?",
                (judge_model_id,),
            ).fetchone()[0]
            if used_by_evaluators:
                raise ValueError(
                    f"该评测模型正被 {used_by_evaluators} 个评测指标使用，请先调整这些指标"
                )
            if used_by_experiments:
                raise ValueError(
                    f"该评测模型正被 {used_by_experiments} 个评测实验使用，请先删除这些实验"
                )
            db.execute(
                "DELETE FROM judge_models WHERE judge_model_id=?", (judge_model_id,)
            )
        return True

    def list_evaluators(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT e.*, j.name AS judge_model_name
                FROM evaluators e
                LEFT JOIN judge_models j ON j.judge_model_id=e.judge_model_id
                ORDER BY e.is_builtin DESC, e.dimension, e.created_at DESC
            """).fetchall()]
        for row in rows:
            row["config"] = json.loads(row.pop("config_json") or "{}")
        return rows

    def create_evaluator(self, evaluator: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.connection() as db:
            if evaluator.get("judge_model_id"):
                exists = db.execute(
                    "SELECT 1 FROM judge_models WHERE judge_model_id=?",
                    (evaluator["judge_model_id"],),
                ).fetchone()
                if not exists:
                    raise KeyError("judge model not found")
            db.execute("""
                INSERT INTO evaluators(
                    evaluator_id,name,description,evaluator_type,dimension,
                    score_min,score_max,pass_threshold,judge_model_id,
                    prompt_template,config_json,version,status,is_builtin
                ) VALUES(
                    :evaluator_id,:name,:description,:evaluator_type,:dimension,
                    :score_min,:score_max,:pass_threshold,:judge_model_id,
                    :prompt_template,:config_json,:version,:status,:is_builtin
                )
            """, evaluator)
        result = dict(evaluator)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def update_evaluator(
        self, evaluator_id: str, evaluator: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM evaluators WHERE evaluator_id=?", (evaluator_id,)
            ).fetchone()
            if not exists:
                return None
            if evaluator.get("judge_model_id"):
                judge_exists = db.execute(
                    "SELECT 1 FROM judge_models WHERE judge_model_id=?",
                    (evaluator["judge_model_id"],),
                ).fetchone()
                if not judge_exists:
                    raise KeyError("judge model not found")
            db.execute("""
                UPDATE evaluators SET
                    name=:name,
                    description=:description,
                    evaluator_type=:evaluator_type,
                    dimension=:dimension,
                    score_min=:score_min,
                    score_max=:score_max,
                    pass_threshold=:pass_threshold,
                    judge_model_id=:judge_model_id,
                    prompt_template=:prompt_template,
                    config_json=:config_json,
                    version=version+1,
                    status='active'
                WHERE evaluator_id=:evaluator_id
            """, {**evaluator, "evaluator_id": evaluator_id})
        return self.get_evaluator(evaluator_id)

    def delete_evaluator(self, evaluator_id: str) -> bool:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM evaluators WHERE evaluator_id=?", (evaluator_id,)
            ).fetchone()
            if not exists:
                return False
            used = db.execute(
                "SELECT COUNT(*) FROM experiment_evaluators WHERE evaluator_id=?",
                (evaluator_id,),
            ).fetchone()[0]
            if used:
                raise ValueError(f"该评测指标正被 {used} 个评测实验使用，请先删除这些实验")
            db.execute("DELETE FROM evaluators WHERE evaluator_id=?", (evaluator_id,))
        return True

    def list_datasets(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT d.*, COUNT(c.case_id) AS case_count
                FROM eval_datasets d LEFT JOIN eval_cases c ON c.dataset_id=d.dataset_id
                GROUP BY d.dataset_id ORDER BY d.created_at DESC
            """).fetchall()]

    def create_dataset(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.connection() as db:
            db.execute("""
                INSERT INTO eval_datasets(dataset_id,name,description)
                VALUES(:dataset_id,:name,:description)
            """, dataset)
        return dataset

    def update_dataset(
        self, dataset_id: str, dataset: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM eval_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if not exists:
                return None
            db.execute(
                "UPDATE eval_datasets SET name=?, description=? WHERE dataset_id=?",
                (dataset["name"], dataset.get("description", ""), dataset_id),
            )
            row = db.execute(
                "SELECT * FROM eval_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_dataset(self, dataset_id: str) -> bool:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM eval_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if not exists:
                return False
            used = db.execute(
                "SELECT COUNT(*) FROM eval_experiments WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()[0]
            if used:
                raise ValueError(f"该数据集正被 {used} 个评测实验使用，请先删除这些实验")
            attempts = db.execute("""
                SELECT COUNT(*) FROM eval_attempts a
                JOIN eval_cases c ON c.case_id=a.case_id
                WHERE c.dataset_id=?
            """, (dataset_id,)).fetchone()[0]
            if attempts:
                raise ValueError(
                    f"该数据集的 Case 已有 {attempts} 次历史运行记录，删除会破坏运行历史"
                )
            # 连接层不一定开启外键约束，显式清理 Case
            db.execute("DELETE FROM eval_cases WHERE dataset_id=?", (dataset_id,))
            db.execute("DELETE FROM eval_datasets WHERE dataset_id=?", (dataset_id,))
        return True

    def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            dataset = db.execute("SELECT * FROM eval_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            if not dataset:
                return None
            cases = []
            for row in db.execute("SELECT * FROM eval_cases WHERE dataset_id=? ORDER BY created_at", (dataset_id,)):
                case = dict(row)
                case["assertions"] = json.loads(case.pop("assertions_json") or "[]")
                cases.append(case)
        return {"dataset": dict(dataset), "cases": cases}

    def add_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.connection() as db:
            exists = db.execute("SELECT 1 FROM eval_datasets WHERE dataset_id=?", (case["dataset_id"],)).fetchone()
            if not exists:
                raise KeyError("dataset not found")
            db.execute("""
                INSERT INTO eval_cases(case_id,dataset_id,name,input,expected_output,assertions_json)
                VALUES(:case_id,:dataset_id,:name,:input,:expected_output,:assertions_json)
            """, case)
        result = dict(case)
        result["assertions"] = json.loads(result.pop("assertions_json"))
        return result

    def update_case(
        self, case_id: str, case: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT dataset_id, assertions_json FROM eval_cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
            if not exists:
                return None
            # 未提供断言时保留原断言，避免编辑 Case 时把已有验收规则清空
            assertions_json = (
                json.dumps(case["assertions"], ensure_ascii=False)
                if case.get("assertions") is not None
                else exists["assertions_json"]
            )
            # 不允许通过编辑把 Case 挪到其它数据集
            db.execute("""
                UPDATE eval_cases SET
                    name=?, input=?, expected_output=?, assertions_json=?
                WHERE case_id=?
            """, (
                case["name"], case["input"], case.get("expected_output", ""),
                assertions_json, case_id,
            ))
        return self.get_case(case_id)

    def delete_case(self, case_id: str) -> bool:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM eval_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if not exists:
                return False
            used = db.execute(
                "SELECT COUNT(*) FROM eval_attempts WHERE case_id=?", (case_id,)
            ).fetchone()[0]
            if used:
                raise ValueError(
                    f"该 Case 已有 {used} 次历史运行记录，删除会破坏运行历史；"
                    "请先删除引用它的评测实验"
                )
            db.execute("DELETE FROM eval_cases WHERE case_id=?", (case_id,))
        return True

    def list_eval_experiments(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT e.*, a.name AS agent_name, d.name AS dataset_name,
                       j.name AS judge_model_name, j.model AS judge_model,
                       CASE WHEN COUNT(DISTINCT at.attempt_id) > 0
                            THEN COUNT(DISTINCT at.attempt_id)
                            ELSE COUNT(DISTINCT t.trace_id) END AS run_count,
                       COUNT(DISTINCT ee.evaluator_id) AS evaluator_count,
                       GROUP_CONCAT(DISTINCT ev.name) AS evaluator_names
                FROM eval_experiments e
                JOIN eval_agents a ON a.agent_id=e.agent_id
                JOIN eval_datasets d ON d.dataset_id=e.dataset_id
                LEFT JOIN judge_models j ON j.judge_model_id=e.judge_model_id
                LEFT JOIN experiment_evaluators ee ON ee.experiment_id=e.experiment_id
                LEFT JOIN evaluators ev ON ev.evaluator_id=ee.evaluator_id
                LEFT JOIN traces t ON t.experiment_id=e.experiment_id
                LEFT JOIN eval_attempts at ON at.experiment_id=e.experiment_id
                GROUP BY e.experiment_id ORDER BY e.created_at DESC
            """).fetchall()]

    def create_eval_experiment(self, experiment: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.connection() as db:
            agent = db.execute("SELECT name FROM eval_agents WHERE agent_id=?", (experiment["agent_id"],)).fetchone()
            dataset = db.execute("SELECT name FROM eval_datasets WHERE dataset_id=?", (experiment["dataset_id"],)).fetchone()
            judge = None
            if experiment.get("judge_model_id"):
                judge = db.execute(
                    "SELECT name, model FROM judge_models WHERE judge_model_id=?",
                    (experiment["judge_model_id"],),
                ).fetchone()
            if not agent or not dataset or (experiment.get("judge_model_id") and not judge):
                raise KeyError("agent, dataset or judge model not found")
            evaluator_ids = experiment.pop("evaluator_ids", [])
            if evaluator_ids:
                found = {
                    row[0] for row in db.execute(
                        f"SELECT evaluator_id FROM evaluators WHERE evaluator_id IN ({','.join('?' for _ in evaluator_ids)})",
                        evaluator_ids,
                    ).fetchall()
                }
                if found != set(evaluator_ids):
                    raise KeyError("one or more evaluators not found")
            db.execute("""
                INSERT INTO eval_experiments(
                    experiment_id,name,agent_id,dataset_id,judge_model_id,status
                ) VALUES(
                    :experiment_id,:name,:agent_id,:dataset_id,:judge_model_id,:status
                )
            """, experiment)
            for evaluator_id in evaluator_ids:
                evaluator = db.execute(
                    "SELECT config_json FROM evaluators WHERE evaluator_id=?",
                    (evaluator_id,),
                ).fetchone()
                config = json.loads(evaluator[0] or "{}")
                db.execute("""
                    INSERT INTO experiment_evaluators(
                        experiment_id,evaluator_id,is_gate
                    ) VALUES(?,?,?)
                """, (
                    experiment["experiment_id"],
                    evaluator_id,
                    1 if config.get("gate") else 0,
                ))
        return {
            **experiment,
            "agent_name": agent[0],
            "dataset_name": dataset[0],
            "judge_model_name": judge[0] if judge else None,
            "judge_model": judge[1] if judge else None,
            "evaluator_count": len(evaluator_ids),
            "evaluator_names": None,
            "run_count": 0,
        }

    def delete_eval_experiment(self, experiment_id: str) -> bool:
        with self._lock, self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM eval_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if not exists:
                return False
            running = db.execute(
                "SELECT COUNT(*) FROM eval_runs WHERE experiment_id=? AND status='running'",
                (experiment_id,),
            ).fetchone()[0]
            if running:
                raise ValueError("该实验正在运行，请等待本次运行结束后再删除")
            # 连接层不一定开启外键约束，显式清理关联记录；Trace 原样保留
            db.execute(
                "DELETE FROM eval_attempts WHERE experiment_id=?", (experiment_id,)
            )
            db.execute(
                "DELETE FROM eval_runs WHERE experiment_id=?", (experiment_id,)
            )
            db.execute(
                "DELETE FROM experiment_evaluators WHERE experiment_id=?",
                (experiment_id,),
            )
            db.execute(
                "DELETE FROM eval_experiments WHERE experiment_id=?", (experiment_id,)
            )
        return True

    def get_eval_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            exp = db.execute("""
                SELECT e.*, a.name AS agent_name, a.endpoint AS agent_endpoint,
                       a.protocol AS agent_protocol, a.runtime_type AS agent_runtime_type,
                       a.execution_mode AS agent_execution_mode,
                       a.executable AS agent_executable,
                       a.working_directory AS agent_working_directory,
                       a.config_json AS agent_config_json,
                       d.name AS dataset_name
                FROM eval_experiments e
                JOIN eval_agents a ON a.agent_id=e.agent_id
                JOIN eval_datasets d ON d.dataset_id=e.dataset_id
                WHERE e.experiment_id=?
            """, (experiment_id,)).fetchone()
            if not exp:
                return None
            evaluators = [dict(row) for row in db.execute("""
                SELECT v.*, ee.weight, ee.is_gate
                FROM experiment_evaluators ee JOIN evaluators v ON v.evaluator_id=ee.evaluator_id
                WHERE ee.experiment_id=? ORDER BY v.dimension, v.name
            """, (experiment_id,)).fetchall()]
            cases = [dict(row) for row in db.execute(
                "SELECT * FROM eval_cases WHERE dataset_id=? ORDER BY created_at", (exp["dataset_id"],)
            ).fetchall()]
            trace_runs = [dict(row) for row in db.execute("""
                SELECT t.*, c.name AS case_name FROM traces t
                LEFT JOIN eval_cases c ON c.case_id=t.case_id
                WHERE t.experiment_id=? ORDER BY t.started_at_ns
            """, (experiment_id,)).fetchall()]
            attempts = [dict(row) for row in db.execute("""
                SELECT a.*, c.name AS case_name
                FROM eval_attempts a
                JOIN eval_cases c ON c.case_id=a.case_id
                WHERE a.experiment_id=? ORDER BY a.started_at_ns
            """, (experiment_id,)).fetchall()]
            traces_by_id = {run["trace_id"]: run for run in trace_runs}
        for evaluator in evaluators:
            evaluator["config"] = json.loads(evaluator.pop("config_json") or "{}")
        for case in cases:
            case["assertions"] = json.loads(case.pop("assertions_json") or "[]")
        if attempts:
            runs = []
            represented_traces = set()
            for attempt in attempts:
                trace = traces_by_id.get(attempt.get("trace_id")) or {}
                if attempt.get("trace_id"):
                    represented_traces.add(attempt["trace_id"])
                runs.append({
                    **trace,
                    **attempt,
                    "started_at_ns": trace.get("started_at_ns") or attempt["started_at_ns"],
                    "business_status": (
                        trace.get("business_status")
                        if trace else attempt["status"]
                    ),
                })
            runs.extend(run for run in trace_runs if run["trace_id"] not in represented_traces)
            total = len(attempts)
            passed = sum(1 for attempt in attempts if attempt["status"] == "passed")
            evaluated = sum(1 for attempt in attempts if attempt["status"] in {"passed", "failed"})
            covered_cases = len({attempt["case_id"] for attempt in attempts})
            trace_values = [
                traces_by_id[attempt["trace_id"]]
                for attempt in attempts if attempt.get("trace_id") in traces_by_id
            ]
        else:
            runs = trace_runs
            total = len(runs)
            passed = sum(1 for run in runs if run["business_status"] == "passed")
            evaluated = sum(1 for run in runs if run["business_status"] in {"passed", "failed"})
            covered_cases = len({run["case_id"] for run in runs if run.get("case_id")})
            trace_values = runs
        case_count = len(cases)
        return {
            "experiment": dict(exp), "evaluators": evaluators, "cases": cases,
            "runs": runs, "attempts": attempts,
            "run_batches": self.experiment_run_batches(experiment_id),
            "summary": {
                "total": total, "passed": passed,
                "pass_rate": round(passed / total, 4) if total else None,
                "evaluated": evaluated,
                "evaluated_pass_rate": round(passed / evaluated, 4) if evaluated else None,
                "covered_cases": covered_cases,
                "coverage_rate": round(covered_cases / case_count, 4) if case_count else None,
                "errors": sum(
                    1 for attempt in attempts
                    if attempt["status"] in {"runtime_error", "telemetry_missing", "evaluation_error"}
                ),
                "avg_score": round(
                    sum((attempt.get("score") or 0) for attempt in attempts) / total, 4
                ) if attempts and total else (
                    round(sum((run["score"] or 0) for run in runs) / total, 4) if total else None
                ),
                "total_tokens": sum(run.get("total_tokens") or 0 for run in trace_values),
                "avg_duration_ms": round(
                    sum(run.get("duration_ms") or 0 for run in trace_values) / len(trace_values), 1
                ) if trace_values else None,
            },
        }

    def set_experiment_status(self, experiment_id: str, status: str) -> None:
        with self._lock, self.connection() as db:
            db.execute("UPDATE eval_experiments SET status=? WHERE experiment_id=?", (status, experiment_id))


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    index = (len(values) - 1) * p
    low = int(index)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (index - low), 2)
