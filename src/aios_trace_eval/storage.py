"""SQLite persistence for the unified AIOS observability and evaluation console."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    run_id TEXT,
    service_name TEXT NOT NULL DEFAULT 'openclaw',
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at_ns DESC);
CREATE INDEX IF NOT EXISTS idx_traces_experiment ON traces(experiment_id, version);
CREATE INDEX IF NOT EXISTS idx_traces_case ON traces(case_id);

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
        update = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "trace_id")
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

    def list_traces(self, limit: int = 100, status: Optional[str] = None, query: str = "") -> List[Dict[str, Any]]:
        where: List[str] = []
        args: List[Any] = []
        if status:
            where.append("business_status = ?")
            args.append(status)
        if query:
            where.append("(trace_id LIKE ? OR case_id LIKE ? OR model LIKE ? OR agent LIKE ?)")
            needle = f"%{query}%"
            args.extend([needle] * 4)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        args.append(min(max(limit, 1), 500))
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                f"SELECT * FROM traces {clause} ORDER BY started_at_ns DESC LIMIT ?", args
            ).fetchall()]

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            trace_row = db.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
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
        return {"trace": dict(trace_row), "spans": spans, "evaluations": evals}

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
    ) -> bool:
        with self._lock, self.connection() as db:
            changed = db.execute("""
                UPDATE traces SET case_id=?, experiment_id=COALESCE(?, experiment_id),
                    agent=COALESCE(?, agent), business_status=?, score=?
                WHERE trace_id=?
            """, (case_id, experiment_id, agent_name, "passed" if passed else "failed", score, trace_id)).rowcount
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
    ) -> bool:
        status = "passed" if passed else "failed"
        with self._lock, self.connection() as db:
            changed = db.execute(
                "UPDATE traces SET case_id=?, business_status=?, score=? WHERE trace_id=?",
                (case_id, status, score, trace_id),
            ).rowcount
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
                INSERT INTO eval_agents(agent_id,name,description,endpoint,protocol,auth_type)
                VALUES(:agent_id,:name,:description,:endpoint,:protocol,:auth_type)
            """, agent)
        return agent

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

    def list_eval_experiments(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT e.*, a.name AS agent_name, d.name AS dataset_name,
                       j.name AS judge_model_name, j.model AS judge_model,
                       COUNT(DISTINCT t.trace_id) AS run_count,
                       COUNT(DISTINCT ee.evaluator_id) AS evaluator_count,
                       GROUP_CONCAT(DISTINCT ev.name) AS evaluator_names
                FROM eval_experiments e
                JOIN eval_agents a ON a.agent_id=e.agent_id
                JOIN eval_datasets d ON d.dataset_id=e.dataset_id
                LEFT JOIN judge_models j ON j.judge_model_id=e.judge_model_id
                LEFT JOIN experiment_evaluators ee ON ee.experiment_id=e.experiment_id
                LEFT JOIN evaluators ev ON ev.evaluator_id=ee.evaluator_id
                LEFT JOIN traces t ON t.experiment_id=e.experiment_id
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

    def get_eval_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            exp = db.execute("""
                SELECT e.*, a.name AS agent_name, a.endpoint AS agent_endpoint,
                       a.protocol AS agent_protocol, d.name AS dataset_name
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
            runs = [dict(row) for row in db.execute("""
                SELECT t.*, c.name AS case_name FROM traces t
                LEFT JOIN eval_cases c ON c.case_id=t.case_id
                WHERE t.experiment_id=? ORDER BY t.started_at_ns
            """, (experiment_id,)).fetchall()]
        for evaluator in evaluators:
            evaluator["config"] = json.loads(evaluator.pop("config_json") or "{}")
        for case in cases:
            case["assertions"] = json.loads(case.pop("assertions_json") or "[]")
        passed = sum(1 for run in runs if run["business_status"] == "passed")
        return {
            "experiment": dict(exp), "evaluators": evaluators, "cases": cases, "runs": runs,
            "summary": {
                "total": len(runs), "passed": passed,
                "pass_rate": round(passed / len(runs), 4) if runs else None,
                "avg_score": round(sum((run["score"] or 0) for run in runs) / len(runs), 4) if runs else None,
                "total_tokens": sum(run["total_tokens"] or 0 for run in runs),
                "avg_duration_ms": round(sum(run["duration_ms"] or 0 for run in runs) / len(runs), 1) if runs else None,
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
