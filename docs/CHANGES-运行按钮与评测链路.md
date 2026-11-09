# AIOS-eval 改动说明：实验「运行」按钮与评测链路修复

> 本文档记录本次对 `aios-trace-eval` 的全部代码改动与配套配置改动。项目当前不是 Git 仓库，因此以「新增文件 + 修改点 before/after」的形式呈现，便于评审和交接。

## 一、改动一览

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/aios_trace_eval/experiment_runner.py` | **新增** | 实验批量执行引擎：逐条跑 OpenClaw → 等 OTLP Trace → 调平台评分 |
| `src/aios_trace_eval/app.py` | 修改 | 新增运行/进度接口、后台执行线程、运行状态注册表；修复 LLM 评估器未回退实验默认 Judge |
| `web/assets/app.js` | 修改 | 实验卡片/详情新增「运行」按钮、进度轮询、失败提示 |
| `web/index.html` | 修改 | 静态资源版本号提升；左上角 Logo 改为 `AIOS-eval` |
| `scripts/run_dataset_experiment.py` | **新增** | 命令行版通用 runner（不依赖页面的无头运行方式） |
| `~/.openclaw/openclaw.json` | 配置 | `diagnostics.otel.endpoint` 由 `127.0.0.1:4318` 改为 `127.0.0.1:8080` |
| `data/aios_eval.db`（evaluators 表） | 数据 | `Token 预算` 阈值 `10000` → `60000` |

---

## 二、背景

改造前，平台只能在页面上**定义**实验（Agent + 数据集 + 评估器），不能**执行**实验：

- 后端没有批量运行接口，只有 `POST /api/experiments/{id}/status` 手动改状态；
- 前端实验详情只渲染汇总和已有结果，没有运行入口；
- 实际运行需依赖外部脚本 `scripts/run_gsm8k_openclaw.py`，且该脚本写死 GSM8K 数据集，无法复用于自定义评测集。

本次改动把执行能力内置到平台：页面点击「运行实验」后，后端在后台线程逐条驱动 OpenClaw、等待 Trace、调用评估器并落库，前端轮询展示进度和结果。

执行链路：

```text
页面点击「运行」
  → POST /api/experiments/{id}/run
  → 后台线程逐条 case：
       openclaw agent --message <case.input>
       → OpenClaw 通过 OTLP 上报 /v1/traces
       → GET /api/traces 轮询拿到新 Trace
       → POST /api/traces/{trace_id}/evaluate
  → 实验状态 running → completed / failed
```

---

## 三、后端改动

### 3.1 `src/aios_trace_eval/app.py`

#### (1) 新增导入与运行状态注册表

**改动前：**

```python
import os
import json
import time
import urllib.error
import urllib.request
import uuid
```

**改动后：**

```python
import os
import json
import threading          # 新增
import time
import urllib.error
import urllib.request
import uuid
```

**改动前：**

```python
from .otlp import decode_traces
from .storage import Store
```

**改动后：**

```python
from . import experiment_runner    # 新增
from .otlp import decode_traces
from .storage import Store
```

**改动前：**

```python
app = FastAPI(title="AIOS Trace Eval", version="0.2.0")
```

**改动后：**

```python
app = FastAPI(title="AIOS Trace Eval", version="0.2.0")

# In-memory state of experiment runs triggered from the web UI. The database only
# records finished trace evaluations; this tracks live progress / errors.
_RUN_STATES: dict = {}
_RUN_LOCK = threading.Lock()
```

> 说明：运行进度保存在进程内存中，服务重启后丢失；已完成的评测结果始终落 SQLite，不丢失。

#### (2) 新增后台执行器与运行接口（全新代码）

插入位置：`update_experiment_status()` 之后、`/api/agents` 之前。

```python
def _experiment_worker(experiment_id: str, detail: dict, base_url: str,
                       gateway_port: str, openclaw_agent: str) -> None:
    def on_progress(state: str, completed: int, total: int, current: str, error: str | None) -> None:
        with _RUN_LOCK:
            _RUN_STATES[experiment_id] = {
                "state": state, "total": total, "completed": completed,
                "current_case": current, "error": error,
            }

    try:
        experiment_runner.run_experiment(
            base_url=base_url, experiment=detail["experiment"], cases=detail["cases"],
            gateway_port=gateway_port, openclaw_agent=openclaw_agent, on_progress=on_progress,
        )
        store.set_experiment_status(experiment_id, "completed")
        with _RUN_LOCK:
            _RUN_STATES[experiment_id] = {"state": "completed", "total": len(detail["cases"]),
                                          "completed": len(detail["cases"]),
                                          "current_case": "", "error": None}
    except Exception as exc:
        store.set_experiment_status(experiment_id, "failed")
        with _RUN_LOCK:
            state = _RUN_STATES.get(experiment_id) or {"total": len(detail["cases"]), "completed": 0,
                                                       "current_case": ""}
            _RUN_STATES[experiment_id] = {**state, "state": "failed", "error": str(exc)[:800]}


@app.post("/api/experiments/{experiment_id}/run", status_code=202)
def start_experiment_run(experiment_id: str, request: Request) -> dict:
    detail = store.get_eval_experiment(experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    cases = detail["cases"]
    if not cases:
        raise HTTPException(status_code=400, detail="数据集还没有 Case，无法运行")
    with _RUN_LOCK:
        current = _RUN_STATES.get(experiment_id)
        if current and current.get("state") == "running":
            raise HTTPException(status_code=409, detail="实验正在运行中")
        _RUN_STATES[experiment_id] = {"state": "running", "total": len(cases), "completed": 0,
                                      "current_case": "", "error": None}
    store.set_experiment_status(experiment_id, "running")
    base_url = os.environ.get("AIOS_BASE_URL") or str(request.base_url).rstrip("/")
    agent_endpoint = detail["experiment"].get("agent_endpoint") or ""
    gateway_port = experiment_runner.gateway_port_from_endpoint(
        agent_endpoint, os.environ.get("OPENCLAW_GATEWAY_PORT", "18790"))
    threading.Thread(
        target=_experiment_worker,
        args=(experiment_id, detail, base_url, gateway_port, os.environ.get("OPENCLAW_AGENT", "main")),
        daemon=True,
    ).start()
    return {"experiment_id": experiment_id, "state": "running", "total": len(cases),
            "gateway_port": gateway_port}


@app.get("/api/experiments/{experiment_id}/run")
def experiment_run_status(experiment_id: str) -> dict:
    if store.get_eval_experiment(experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    with _RUN_LOCK:
        state = dict(_RUN_STATES[experiment_id]) if experiment_id in _RUN_STATES else None
    if state is None:
        return {"experiment_id": experiment_id, "state": "idle", "total": 0,
                "completed": 0, "current_case": "", "error": None}
    return {"experiment_id": experiment_id, **state}
```

接口行为：

| 接口 | 方法 | 行为 |
|---|---|---|
| `/api/experiments/{id}/run` | POST | 202 立即返回；已在运行返回 409；数据集无 Case 返回 400 |
| `/api/experiments/{id}/run` | GET | 返回 `state`（idle/running/completed/failed）、`completed/total`、`current_case`、`error` |

#### (3) 修复 LLM 评估器未回退到实验默认 Judge（缺陷修复）

**问题现象：** 实验已选择默认 Judge，但运行时 400：

```json
{"detail":"任务完成度 执行失败：LLM 评估器未绑定 Judge 模型"}
```

**原因：** 内置 LLM 评估器创建时 `judge_model_id` 为空，而 `_run_llm_evaluator` 只读评估器自身绑定，没有使用实验的 `judge_model_id`。

**改动前：**

```python
def _run_llm_evaluator(evaluator: dict, case: dict, actual_output: str, trace_row: dict) -> tuple[bool, float, str, dict]:
    judge_id = evaluator.get("judge_model_id")
    if not judge_id:
        raise ValueError("LLM 评估器未绑定 Judge 模型")
```

**改动后：**

```python
def _run_llm_evaluator(evaluator: dict, case: dict, actual_output: str, trace_row: dict,
                       default_judge_id: str | None = None) -> tuple[bool, float, str, dict]:
    # Fall back to the experiment's default judge model when the evaluator itself
    # does not declare one (built-in LLM evaluators are created without a binding).
    judge_id = evaluator.get("judge_model_id") or default_judge_id
    if not judge_id:
        raise ValueError("LLM 评估器未绑定 Judge 模型")
```

**改动前（`evaluate_trace`）：**

```python
    experiment = store.get_eval_experiment(body.experiment_id) if body.experiment_id else None
    evaluator_ids = body.evaluator_ids or ([item["evaluator_id"] for item in experiment["evaluators"]] if experiment else [])
```

**改动后：**

```python
    experiment = store.get_eval_experiment(body.experiment_id) if body.experiment_id else None
    default_judge_id = experiment["experiment"].get("judge_model_id") if experiment else None
    evaluator_ids = body.evaluator_ids or ([item["evaluator_id"] for item in experiment["evaluators"]] if experiment else [])
```

**改动前（LLM 评估器调用处）：**

```python
                ok, score, reason, evidence = _run_llm_evaluator(evaluator, case, body.actual_output, trace_row)
```

**改动后：**

```python
                ok, score, reason, evidence = _run_llm_evaluator(evaluator, case, body.actual_output, trace_row, default_judge_id)
```

### 3.2 新增 `src/aios_trace_eval/experiment_runner.py`

职责：与具体数据集无关的批量执行引擎。单条 case 失败不中断整批，最终汇总错误。

```python
"""In-process experiment runner.

Drives a real OpenClaw Gateway for every case of an experiment and stores the
evaluated result back through the platform's own HTTP API. This is the engine
behind the "运行实验" button; it mirrors scripts/run_gsm8k_openclaw.py but is
dataset-agnostic and targets an existing experiment.

The caller runs this in a background thread and receives progress through the
``on_progress`` callback so the web UI can poll live status.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse


ProgressFn = Callable[[str, int, int, str, Optional[str]], None]


def _request(base_url: str, path: str, method: str = "GET", payload: Dict[str, Any] | None = None,
             timeout: float = 90.0) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body[:400]}") from exc


def gateway_port_from_endpoint(endpoint: str, default: str) -> str:
    try:
        port = urlparse(endpoint or "").port
        if port:
            return str(port)
    except ValueError:
        pass
    return default


def _openclaw_command(prompt: str, session_id: str, openclaw_agent: str, timeout_s: int) -> List[str]:
    executable = shutil.which("openclaw.cmd") if os.name == "nt" else shutil.which("openclaw")
    if not executable:
        raise RuntimeError("PATH 中找不到 openclaw，请确认 OpenClaw 已安装")
    command = [executable, "agent", "--agent", openclaw_agent, "--session-id", session_id,
               "--message", prompt, "--json", "--timeout", str(timeout_s)]
    if os.name == "nt":
        node = shutil.which("node")
        entry = Path(executable).parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if not node or not entry.exists():
            raise RuntimeError("找不到 OpenClaw 的 Node.js 入口")
        command = [node, str(entry), *command[1:]]
    return command


def _extract_output(envelope: Dict[str, Any]) -> str:
    try:
        return envelope["result"]["payloads"][0]["text"]
    except (KeyError, IndexError, TypeError):
        pass
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if isinstance(result, dict):
        for key in ("text", "output", "content", "message"):
            if isinstance(result.get(key), str):
                return result[key]
    if isinstance(envelope, dict) and isinstance(envelope.get("text"), str):
        return envelope["text"]
    return json.dumps(envelope, ensure_ascii=False)[:2000]


def _run_one_case(base_url: str, experiment_id: str, case: Dict[str, Any], *,
                  gateway_port: str, openclaw_agent: str, timeout_s: int,
                  poll_timeout_s: float) -> Dict[str, Any]:
    before = {item["trace_id"] for item in _request(base_url, "/api/traces?limit=200")}

    env = os.environ.copy()
    env["OPENCLAW_GATEWAY_PORT"] = gateway_port
    command = _openclaw_command(case["input"], f"exp-{uuid.uuid4().hex[:10]}", openclaw_agent, timeout_s)
    completed = subprocess.run(command, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout_s + 30)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout[-800:] or "openclaw 退出码非 0")
    envelope = json.loads(completed.stdout)
    output = _extract_output(envelope)

    trace = None
    deadline = time.time() + poll_timeout_s
    while time.time() < deadline:
        fresh = [item for item in _request(base_url, "/api/traces?limit=200")
                 if item["trace_id"] not in before]
        if fresh:
            trace_id = fresh[0]["trace_id"]
            current = _request(base_url, f"/api/traces/{trace_id}")["trace"]
            if current["runtime_status"] == "completed" and (current.get("total_tokens") or 0) > 0 \
                    and (current.get("duration_ms") or 0) > 0:
                trace = current
                break
        time.sleep(1)
    if trace is None:
        raise RuntimeError(f"{int(poll_timeout_s)} 秒内没有收到对应 OTLP Trace")

    result = _request(base_url, f"/api/traces/{trace['trace_id']}/evaluate", "POST", {
        "case_id": case["case_id"], "actual_output": output, "experiment_id": experiment_id,
    })
    return {
        "case": case.get("name"),
        "trace_id": trace["trace_id"],
        "passed": result.get("passed"),
        "score": result.get("score"),
        "tokens": trace.get("total_tokens"),
        "duration_ms": trace.get("duration_ms"),
    }


def run_experiment(*, base_url: str, experiment: Dict[str, Any], cases: List[Dict[str, Any]],
                   gateway_port: str, openclaw_agent: str = "main", timeout_s: int = 120,
                   poll_timeout_s: float = 30.0,
                   on_progress: Optional[ProgressFn] = None) -> List[Dict[str, Any]]:
    """Run every case sequentially. Per-case failures are collected, not fatal."""
    experiment_id = experiment["experiment_id"]
    total = len(cases)
    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    for index, case in enumerate(cases):
        name = case.get("name") or case.get("case_id") or f"case-{index + 1}"
        if on_progress:
            on_progress("running", index, total, name, None)
        try:
            results.append(_run_one_case(
                base_url, experiment_id, case,
                gateway_port=gateway_port, openclaw_agent=openclaw_agent,
                timeout_s=timeout_s, poll_timeout_s=poll_timeout_s,
            ))
        except Exception as exc:  # keep going so one bad case doesn't sink the batch
            errors.append(f"{name}: {str(exc)[:300]}")
        if on_progress:
            on_progress("running", index + 1, total, name, None)

    if errors:
        raise RuntimeError("；".join(errors[:5]))
    return results
```

---

## 四、前端改动

### 4.1 `web/assets/app.js`

#### (1) 实验详情：新增运行按钮 + 进度轮询（替换原 `openExperiment`）

**改动前（整段只有一个函数）：**

```js
async function openExperiment(id){const detail=await api('/api/eval-experiments/'+id),exp=detail.experiment,s=detail.summary;$('#drawerTitle').textContent=exp.name;$('#drawerDesc').textContent=`${exp.agent_name} · ${exp.dataset_name}`;const rate=s.pass_rate==null?'—':`${(s.pass_rate*100).toFixed(1)}%`;$('#drawerBody').innerHTML=`<div class="detail-section"><h3>实验汇总</h3>...（原样只渲染汇总/评估器/结果）...`;openDrawer();$$('#drawerBody [data-trace]').forEach(row=>row.onclick=()=>openRun(row.dataset.trace))}
```

**改动后（拆分为状态、渲染、绑定、启动、轮询）：**

```js
let experimentPollTimer=null,activeExperimentId=null;
function stopExperimentPoll(){if(experimentPollTimer){clearInterval(experimentPollTimer);experimentPollTimer=null}}
function experimentButtonState(runState){if(!runState||runState.state==='idle')return{text:'▶ 运行实验',disabled:false,note:'点击后逐条执行数据集中的用例，自动采集 Trace 并评分'};if(runState.state==='running')return{text:'运行中…',disabled:true,note:`已完成 ${runState.completed}/${runState.total}${runState.current_case?`，正在执行：${runState.current_case}`:''}`};if(runState.state==='completed')return{text:'重新运行',disabled:false,note:`上次运行完成 ${runState.completed}/${runState.total} 条`};return{text:'重新运行',disabled:false,note:`上次运行失败：${runState.error||'未知错误'}`}}
function renderExperiment(detail,runState){const exp=detail.experiment,s=detail.summary,rate=s.pass_rate==null?'—':`${(s.pass_rate*100).toFixed(1)}%`,btn=experimentButtonState(runState);$('#drawerTitle').textContent=exp.name;$('#drawerDesc').textContent=`${exp.agent_name} · ${exp.dataset_name}`;$('#drawerBody').innerHTML=`<div class="drawer-actions"><div><b>运行实验</b><p class="muted">${escapeHtml(btn.note)}</p></div><button class="button primary" id="runExperimentBtn" ${btn.disabled?'disabled':''}>${btn.text}</button></div><div class="detail-section"><h3>实验汇总</h3><div class="detail-grid detail-grid-four"><div><span>完成样本</span><b>${s.total} / ${detail.cases.length}</b></div><div><span>通过率</span><b>${rate}</b></div><div><span>总 Token</span><b>${formatNumber(s.total_tokens)}</b></div><div><span>平均耗时</span><b>${formatDuration(s.avg_duration_ms)}</b></div></div></div><div class="detail-section"><h3>评估器</h3><div class="evaluation-list">${detail.evaluators.map(item=>`<div class="evaluation-row"><div><b>${escapeHtml(item.name)}</b><span class="tag">${escapeHtml(item.evaluator_type)}</span></div><strong>阈值 ${item.pass_threshold}</strong><p>${escapeHtml(item.description)}</p></div>`).join('')}</div></div><h3>逐样本结果</h3>${detail.runs.length?`<table class="data-table"><thead><tr><th>样本</th><th>结果</th><th>分数</th><th>Token</th><th>耗时</th></tr></thead><tbody>${detail.runs.map(run=>`<tr data-trace="${run.trace_id}"><td>${escapeHtml(run.case_name||run.case_id)}</td><td>${status(run.business_status)}</td><td>${run.score??'—'}</td><td>${formatNumber(run.total_tokens)}</td><td>${formatDuration(run.duration_ms)}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">实验尚未运行。</p>'}`}
function bindExperimentControls(id){const btn=$('#runExperimentBtn');if(btn)btn.onclick=()=>startExperiment(id);$$('#drawerBody [data-trace]').forEach(row=>row.onclick=()=>openRun(row.dataset.trace))}
async function openExperiment(id){stopExperimentPoll();activeExperimentId=id;const [detail,runState]=await Promise.all([api('/api/eval-experiments/'+id),api('/api/experiments/'+id+'/run').catch(()=>({state:'idle'}))]);renderExperiment(detail,runState);openDrawer();bindExperimentControls(id);if(runState.state==='running')pollExperiment(id)}
async function startExperiment(id){const btn=$('#runExperimentBtn');if(btn){btn.disabled=true;btn.textContent='启动中…'}try{await api('/api/experiments/'+id+'/run',{method:'POST'});toast('实验已开始运行');pollExperiment(id)}catch(error){toast(error.message);if(btn){btn.disabled=false;btn.textContent='▶ 运行实验'}}}
function pollExperiment(id){stopExperimentPoll();experimentPollTimer=setInterval(async()=>{try{const [runState,detail]=await Promise.all([api('/api/experiments/'+id+'/run'),api('/api/eval-experiments/'+id)]);if(activeExperimentId===id&&$('#drawer').classList.contains('open')){renderExperiment(detail,runState);bindExperimentControls(id)}if(runState.state!=='running'){stopExperimentPoll();loadPage(state.page);toast(runState.state==='completed'?'实验运行完成':'实验运行失败：'+(runState.error||'未知错误'))}}catch(error){stopExperimentPoll();toast(error.message)}},2000)}
async function startExperimentFromCard(id){try{await api('/api/experiments/'+id+'/run',{method:'POST'});toast('实验已开始运行')}catch(error){if(!/运行中/.test(error.message))toast(error.message)}await openExperiment(id).catch(error=>toast(error.message))}
```

#### (2) 实验列表卡片：新增「运行」按钮

**改动前：**

```js
<div><span class="tag ${exp.status==='completed'?'success':''}">${escapeHtml(exp.status)}</span></div><em>›</em></div></article>
```

**改动后：**

```js
<div><span class="tag ${exp.status==='completed'?'success':''}">${escapeHtml(exp.status)}</span></div><button class="button small" data-run-experiment="${exp.experiment_id}">运行</button><em>›</em></div></article>
```

#### (3) 全局点击事件：优先处理卡片「运行」按钮

**改动前：**

```js
document.addEventListener('click',event=>{const target=event.target.closest('[data-goto],[data-empty-action],[data-experiment]');if(target?.dataset.goto)route(target.dataset.goto);...
```

**改动后：**

```js
document.addEventListener('click',event=>{const runBtn=event.target.closest('[data-run-experiment]');if(runBtn){event.preventDefault();event.stopPropagation();startExperimentFromCard(runBtn.dataset.run_experiment);return}const target=event.target.closest('[data-goto],[data-empty-action],[data-experiment]');if(target?.dataset.goto)route(target.dataset.goto);...
```

#### (4) 关闭抽屉 / 打开 Trace 详情时停止轮询

**改动前：**

```js
function closeDrawer(){$('#drawerBackdrop').classList.add('hidden');$('#drawer').classList.remove('open');$('#drawer').setAttribute('aria-hidden','true')}
```

**改动后：**

```js
function closeDrawer(){stopExperimentPoll();activeExperimentId=null;$('#drawerBackdrop').classList.add('hidden');$('#drawer').classList.remove('open');$('#drawer').setAttribute('aria-hidden','true')}
```

**`openRun` 改动前：**

```js
async function openRun(id){
  const detail=await api('/api/traces/'+id),run=detail.trace,evaluations=detail.evaluations||[];
```

**改动后：**

```js
async function openRun(id){
  stopExperimentPoll();activeExperimentId=null;
  const detail=await api('/api/traces/'+id),run=detail.trace,evaluations=detail.evaluations||[];
```

### 4.2 `web/index.html`

**静态资源版本号（避免浏览器缓存旧 JS）：**

```diff
- <link rel="stylesheet" href="/assets/app.css?v=20261106-5" />
+ <link rel="stylesheet" href="/assets/app.css?v=20261106-6" />
...
- <script src="/assets/app.js?v=20261106-5" defer></script>
+ <script src="/assets/app.js?v=20261106-6" defer></script>
```

**左上角 Logo：**

```diff
- <a class="logo" href="#overview"><span>AI</span><strong>AIOS</strong></a>
+ <a class="logo" href="#overview"><span>AI</span><strong>AIOS-eval</strong></a>
```

---

## 五、新增命令行 runner（可选）

`scripts/run_dataset_experiment.py` 提供不依赖页面的无头运行方式，适用于 CI 或批量脚本：

```bash
export PYTHONPATH=src
python scripts/run_dataset_experiment.py \
  --experiment-id exp_7f0cdcc3c545 \
  --gateway-port 18789
```

与原有 GSM8K runner 的差异：

| | `run_gsm8k_openclaw.py` | `run_dataset_experiment.py` |
|---|---|---|
| 数据集/实验 | 自动新建 GSM8K 实验 | 使用已存在的实验 |
| Prompt | 追加数值答案后缀 | 原样发送 Case input |
| 端口 | 默认 18790 | `--gateway-port` 可指定 |
| 失败策略 | 遇错即停 | 单条失败继续，最后汇总 |

---

## 六、配套配置与运维改动（不在代码库内）

### 6.1 OpenClaw OTLP 出口

**现象：** 平台始终收不到 Trace，运行报 `30 秒内没有收到对应 OTLP Trace`。

**原因：** `~/.openclaw/openclaw.json` 中遥测出口指向了 OTel Collector：

```json
"diagnostics": {
    "otel": {
      "enabled": true,
      "endpoint": "http://127.0.0.1:4318",   // 原值：Collector / Jaeger
      ...
    }
  }
```

**改为（直连 AIOS）：**

```json
"endpoint": "http://127.0.0.1:8080"
```

**备份文件：** `~/.openclaw/openclaw.json.bak.before-aios-8080-20261106_163419`

**生效方式：**

```bash
systemctl --user restart openclaw-gateway.service
```

> 注意：`endpoint` 不要带 `/v1/traces`，平台会自动拼接。

### 6.2 评估器阈值

`Token 预算`（`builtin_token_budget`）原配置 `total_tokens <= 10000`，但单次 OpenClaw 运行实测约 `42833` tokens，会导致所有用例必然失败。

已调整为：

```json
{"field": "total_tokens", "operator": "le", "value": 60000}
```

> 该配置存放在 `data/aios_eval.db` 的 `evaluators.config_json`，属于运行时数据，不在代码仓库中。

---

## 七、验证记录

| 验证项 | 结果 |
|---|---|
| `python -m compileall -q src` | 通过 |
| `node --check web/assets/app.js` | 通过 |
| 导入 `aios_trace_eval.app` | 通过，`/run` 同时注册 GET/POST |
| 临时实例冒烟：`idle → running → failed` | 通过，`gateway_port` 由 Agent endpoint 正确解析为 `18789` |
| OpenClaw 发测试任务后 AIOS 收 Trace | 通过：`runtime=completed, tokens=42833, duration=3645ms, model=deepseek-flash` |
| LLM 评估器 400 问题 | 已定位并修复（回退实验默认 Judge），待重启服务后复验 |

---

## 八、使用方式

1. 重启 AIOS 服务（加载后端新代码）：

```bash
cd /home/10360106/zte_work/AIOS/AIOS-agent-eval/aios-trace-eval-main
source .venv/bin/activate
export PYTHONPATH=src
python -m aios_trace_eval serve --host 0.0.0.0 --port 8080
```

2. 浏览器 `Ctrl+Shift+R` 强制刷新。
3. 进入「评测实验」，点击卡片「运行」或详情内「▶ 运行实验」。
4. 进度实时显示 `已完成 x/y，正在执行：case`；完成后「逐样本结果」和「运行记录」自动更新。

可选环境变量（由服务进程继承给子进程）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPENCLAW_AGENT` | `main` | OpenClaw 内的 agent 名称 |
| `OPENCLAW_GATEWAY_PORT` | 从 Agent endpoint 解析 | 网关端口 |
| `OPENCLAW_STATE_DIR` | `~/.openclaw` | OpenClaw 状态目录 |
| `OPENCLAW_GATEWAY_TOKEN` | 空 | 网关鉴权 token（如需） |
| `AIOS_BASE_URL` | 当前请求地址 | 后台线程回写评分所用地址 |

---

## 九、已知边界

- **运行进度为进程内存态**：服务重启会丢失 `running/completed` 进度；已完成样本的评分结果在 SQLite 中，不受影响。
- **单进程后台线程**：无任务队列、无并行调度、无取消接口；同一实验通过 409 防止重复启动。
- **Trace 关联方式**：按「运行前快照 → 取最新新增 Trace」顺序窗口关联，依赖逐条串行执行；高并发或别处同时产生 Trace 时可能串号。
- **无鉴权**：运行接口与 Web 控制台一样没有登录和权限控制，不应直接暴露公网。
- **`captureContent=false`**：不采集 Prompt/响应原文，依赖内容的轨迹评估不可用；但本次结果型评估器（精确匹配、Token、时延、LLM Judge）不受影响。

---

## 十、回滚方式

1. 代码：删除 `src/aios_trace_eval/experiment_runner.py`、`scripts/run_dataset_experiment.py`，还原 `app.py` / `app.js` / `index.html` 对应片段。
2. OpenClaw 遥测：恢复备份 `~/.openclaw/openclaw.json.bak.before-aios-8080-20261106_163419`，重启 `openclaw-gateway.service`。
3. Token 阈值：`UPDATE evaluators SET config_json='{"field": "total_tokens", "operator": "le", "value": 10000}' WHERE evaluator_id='builtin_token_budget';`
