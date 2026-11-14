"""环境指纹、独立工作区与可比性判定的单测。

验证点：
  1. 指纹 Comparison Hash 只随环境项变化，不随被测变量（agent/model）变化；
  2. 独立工作区从模板复制并忽略版本库/依赖目录，空模板建空目录，可清理；
  3. storage 写入后能读回指纹并解析 JSON；
  4. 一致性判定：环境相同 + agent 不同 → 可比并列出被测变量；
     时间预算不同 → 不可比并列出差异项。

运行：python scripts/selftest_environment.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TEMP_DB = Path(tempfile.gettempdir()) / "aios-env-selftest.db"
TEMP_WS = Path(tempfile.gettempdir()) / "aios-env-selftest-ws"
try:
    TEMP_DB.unlink()
except FileNotFoundError:
    pass
if TEMP_WS.exists():
    import shutil

    shutil.rmtree(TEMP_WS, ignore_errors=True)
os.environ["AIOS_DB_PATH"] = str(TEMP_DB)
os.environ["AIOS_WORKSPACE_ROOT"] = str(TEMP_WS)
os.environ["AIOS_KEEP_WORKSPACES"] = "0"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aios_trace_eval import experiment_runner as runner
from aios_trace_eval.app import _environment_consistency
from aios_trace_eval.storage import Store

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def make_fingerprint(*, run_id: str, adapter: str = "codex", model: str = "gpt-5.6-sol",
                     timeout: int = 300, case_id: str = "case_demo") -> dict:
    return runner._environment_fingerprint(
        run_id=run_id, experiment_id="exp_demo", case_id=case_id,
        adapter_name=adapter, agent_name=adapter, agent_version="codex-cli 0.167",
        model=model, timeout_s=timeout,
        workspace_path=f"/tmp/{run_id}", workspace_mode="empty_workspace",
        workspace_template="", sampler_scope="process_tree", sampler_interval=0.5,
    )


def main() -> int:
    print("== 环境指纹 ==")
    first = make_fingerprint(run_id="run_a")
    same = make_fingerprint(run_id="run_b")
    other_agent = make_fingerprint(run_id="run_c", adapter="claude_code")
    other_timeout = make_fingerprint(run_id="run_d", timeout=600)
    check("同环境同哈希", same["comparison_hash"], first["comparison_hash"])
    check("换 Agent 不改哈希（被测变量）", other_agent["comparison_hash"], first["comparison_hash"])
    check("换时间预算改哈希（环境项）", other_timeout["comparison_hash"] == first["comparison_hash"], False)
    check("指纹含 comparison 与 tested", sorted(first["fingerprint"]), ["agent", "comparison", "execution", "host", "resources", "sampler", "tested", "workspace"])
    check("comparison 覆盖 7 项", len(first["fingerprint"]["comparison"]), 7)

    print("== 独立工作区 ==")
    template = TEMP_WS / "template"
    (template / "src").mkdir(parents=True)
    (template / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (template / ".git").mkdir()
    (template / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (template / "node_modules").mkdir()
    (template / "node_modules" / "x.js").write_text("x", encoding="utf-8")

    path, mode, source = runner._prepare_workspace("run_ws", "exp_demo", str(template), False)
    copied = Path(path)
    check("模板复制模式", mode, "template_copy")
    check("模板路径记录", source, str(template.resolve()))
    check("文件已复制", (copied / "src" / "main.py").is_file(), True)
    check("忽略 .git", (copied / ".git").exists(), False)
    check("忽略 node_modules", (copied / "node_modules").exists(), False)
    check("工作区标记文件", (copied / ".aios-workspace.json").is_file(), True)
    check("未保留时未清理（由 finally 负责）", copied.exists(), True)

    empty_path, empty_mode, _ = runner._prepare_workspace("run_empty", "exp_demo", None, False)
    check("空工作区模式", empty_mode, "empty_workspace")
    check("空工作区存在", Path(empty_path).is_dir(), True)

    import shutil as _shutil
    _shutil.rmtree(copied, ignore_errors=True)
    check("清理后目录消失", copied.exists(), False)

    print("== 存储读写 ==")
    store = Store(TEMP_DB)
    store.upsert_run_environment(make_fingerprint(run_id="run_store_a"))
    store.upsert_run_environment(make_fingerprint(run_id="run_store_b", adapter="claude_code"))
    records = store.list_run_environments("exp_demo")
    check("写入 2 条", len(records), 2)
    check("读到 fingerprint 且已解析", isinstance(records[0].get("fingerprint"), dict), True)
    check("按 run_id 单条读取", store.get_run_environment("run_store_a")["comparison_hash"], first["comparison_hash"])

    print("== 可比性判定 ==")
    same_env = _environment_consistency([
        {"comparison_hash": first["comparison_hash"],
         "fingerprint": {"comparison": first["fingerprint"]["comparison"],
                         "tested": {"agent": "codex", "model": "gpt-5.6-sol"}}},
        {"comparison_hash": other_agent["comparison_hash"],
         "fingerprint": {"comparison": other_agent["fingerprint"]["comparison"],
                         "tested": {"agent": "claude_code", "model": "gpt-5.6-sol"}}},
    ])
    check("同环境可比", same_env["comparable"], True)
    check("列出被测变量 agent", [item["key"] for item in same_env["tested_variables"]], ["agent"])
    check("摘要标注被测变量", "被测变量" in same_env["summary"], True)

    diff_env = _environment_consistency([
        {"comparison_hash": first["comparison_hash"],
         "fingerprint": {"comparison": first["fingerprint"]["comparison"], "tested": {}}},
        {"comparison_hash": other_timeout["comparison_hash"],
         "fingerprint": {"comparison": other_timeout["fingerprint"]["comparison"], "tested": {}}},
    ])
    check("环境不同不可比", diff_env["comparable"], False)
    check("差异项为时间预算", [item["key"] for item in diff_env["differences"]], ["timeout_sec"])
    check("空记录默认可比", _environment_consistency([])["comparable"], True)

    print()
    if FAILURES:
        print(f"FAILED: {FAILURES}")
        return 1
    print("ENVIRONMENT SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
