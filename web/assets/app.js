const state={page:'overview',agents:[],datasets:[],judgeModels:[],evaluators:[],experiments:[],runs:[],metrics:null};
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const api=async(path,options)=>{const response=await fetch(path,options);if(!response.ok)throw new Error((await response.json().catch(()=>({}))).detail||'请求失败');return response.json()};
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const formatNumber=value=>new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(value??0);
const formatDuration=value=>value==null?'—':value>=1000?`${(value/1000).toFixed(1)} 秒`:`${Math.round(value)} 毫秒`;
const formatBytes=value=>value==null?'未采集':value>=1073741824?`${(value/1073741824).toFixed(2)} GiB`:`${(value/1048576).toFixed(1)} MiB`;
const formatRatio=value=>value==null?'未采集':`${(value*100).toFixed(1)}%`;
const formatDate=value=>value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const nsDate=value=>value?formatDate(Number(BigInt(value)/1000000n)):'—';
const firstAttr=(attrs,keys)=>keys.map(key=>attrs?.[key]).find(value=>value!==undefined&&value!==null&&value!==''&&value!=='<REDACTED>');
function formatPayload(value){if(value==null)return'';if(typeof value!=='string')return JSON.stringify(value,null,2);try{return JSON.stringify(JSON.parse(value),null,2)}catch{return value}}
function processChart(series,{unit,axisLabel,scale,decimals=1,tone='rss'}){
  const width=900,height=250,padLeft=72,padRight=24,padTop=20,padBottom=52,duration=Math.max(Number(series.trace_duration_ms)||0,1);
  const points=(series?.points||[]).filter(point=>point.offset_ms>=0&&point.offset_ms<=duration);
  if(!points.length)return`<div class="process-chart-empty">任务执行期间没有${unit==='MiB'?' RSS':' CPU'}采样点。</div>`;
  const minOffset=0,maxOffset=duration;
  const values=points.map(point=>point.value*scale),rawMin=Math.min(...values),rawMax=Math.max(...values),spread=Math.max(rawMax-rawMin,unit==='%'?1:.5);
  const minY=Math.max(0,rawMin-spread*.2),maxY=rawMax+spread*.2;
  const x=value=>padLeft+(value-minOffset)/Math.max(maxOffset-minOffset,1)*(width-padLeft-padRight);
  const y=value=>height-padBottom-(value-minY)/Math.max(maxY-minY,1)*(height-padTop-padBottom);
  const xTicks=Array.from({length:5},(_,i)=>minOffset+(maxOffset-minOffset)*i/4),yTicks=Array.from({length:5},(_,i)=>minY+(maxY-minY)*i/4);
  const line=points.map((point,index)=>`${index?'L':'M'} ${x(point.offset_ms).toFixed(1)} ${y(point.value*scale).toFixed(1)}`).join(' ');
  const peak=Math.max(...values);
  return`<div class="process-chart ${tone}"><svg viewBox="0 0 ${width} ${height}" role="img">${yTicks.map(tick=>`<line class="chart-grid" x1="${padLeft}" y1="${y(tick)}" x2="${width-padRight}" y2="${y(tick)}"></line><text class="chart-tick y" x="${padLeft-10}" y="${y(tick)+4}">${tick.toFixed(decimals)}</text>`).join('')}${xTicks.map(tick=>`<line class="chart-tick-mark" x1="${x(tick)}" y1="${height-padBottom}" x2="${x(tick)}" y2="${height-padBottom+5}"></line><text class="chart-tick x" x="${x(tick)}" y="${height-padBottom+19}">${(tick/1000).toFixed(1)}</text>`).join('')}<line class="chart-axis" x1="${padLeft}" y1="${padTop}" x2="${padLeft}" y2="${height-padBottom}"></line><line class="chart-axis" x1="${padLeft}" y1="${height-padBottom}" x2="${width-padRight}" y2="${height-padBottom}"></line><path class="process-line" d="${line}"></path>${points.map(point=>`<circle class="process-point" cx="${x(point.offset_ms).toFixed(1)}" cy="${y(point.value*scale).toFixed(1)}" r="3"><title>${(point.offset_ms/1000).toFixed(2)} 秒 · ${(point.value*scale).toFixed(decimals)} ${unit}</title></circle>`).join('')}<text class="chart-axis-title y" transform="translate(17 ${(padTop+height-padBottom)/2}) rotate(-90)">${axisLabel}</text><text class="chart-axis-title x" x="${(padLeft+width-padRight)/2}" y="${height-5}">任务相对时间（秒）</text></svg><div class="process-chart-meta"><span>${points.length} 个任务内采样点</span><b>峰值 ${peak.toFixed(decimals)} ${unit}</b></div></div>`;
}
function readableMessages(value,includeRole=true){
  if(value==null)return'';
  let parsed=value;
  if(typeof value==='string'){try{parsed=JSON.parse(value)}catch{return value}}
  if(!Array.isArray(parsed))return formatPayload(parsed);
  const blocks=parsed.map(message=>{
    const role=message?.role==='assistant'?'助手':message?.role==='user'?'用户':message?.role||'消息';
    const content=Array.isArray(message?.content)?message.content.map(part=>part?.text??part?.content??formatPayload(part)).filter(Boolean).join('\n'):message?.content;
    return content==null?'':`${includeRole?`${role}\n`:''}${typeof content==='string'?content:formatPayload(content)}`;
  }).filter(Boolean);
  return blocks.join('\n\n');
}
function capturedPayloads(detail){const run=detail.trace;let input=run.input_text||run.case_input||'',output=run.output_text||'';for(const span of [...detail.spans].sort((a,b)=>Number(a.started_at_ns)-Number(b.started_at_ns))){const attrs=span.attributes||{};input=input||firstAttr(attrs,['openclaw.content.input_messages','gen_ai.input.messages','input.value','user_prompt'])||'';const currentOutput=firstAttr(attrs,['openclaw.content.output_messages','gen_ai.output.messages','output.value']);if(!run.output_text&&currentOutput)output=currentOutput}return{input:readableMessages(input,false),output:readableMessages(output,false)}}
function spanKind(span){
  const attrs=span.attributes||{},name=span.name||'',operation=String(attrs['gen_ai.operation.name']||'').toLowerCase();
  if(name.includes('tool')||operation==='execute_tool')return'tool';
  if(name.includes('context.assembled'))return'context';
  // 只有真正的模型调用才算 model：invoke_agent/enter/react 等 Agent 级 Span 不能当成 LLM 调用。
  if(operation==='chat'||operation==='text_completion'||operation==='completion'||name.includes('model.call')||name.includes('llm_request'))return'model';
  return'runtime';
}

function messageList(value){
  if(value==null)return[];let parsed=value;
  if(typeof value==='string'){try{parsed=JSON.parse(value)}catch{return[{role:'message',content:value}]}}
  if(!Array.isArray(parsed))return[{role:'message',content:formatPayload(parsed)}];
  return parsed.map(message=>{
    const parts=Array.isArray(message?.content)?message.content:message?.parts;
    const content=Array.isArray(parts)?parts.map(part=>part?.text??part?.content??part?.result??formatPayload(part)).filter(Boolean).join('\n'):message?.content;
    return{role:message?.role||'message',content:typeof content==='string'?content:formatPayload(content)};
  }).filter(message=>message.content);
}
function spanIO(span){
  const attrs=span.attributes||{},kind=spanKind(span),isTool=kind==='tool';
  return{kind,isTool,input:firstAttr(attrs,isTool?['openclaw.content.tool_input','gen_ai.tool.call.arguments','tool.input']:['openclaw.content.input_messages','gen_ai.input.messages','input.value']),output:firstAttr(attrs,isTool?['openclaw.content.tool_output','gen_ai.tool.call.result','tool.output']:['openclaw.content.output_messages','gen_ai.output.messages','output.value'])};
}
function conversationEvents(detail){
  const events=[],seenUsers=new Set(),ordered=[...detail.spans].sort((a,b)=>Number(a.started_at_ns)-Number(b.started_at_ns));
  const payloads=capturedPayloads(detail);
  if(payloads.input){seenUsers.add(payloads.input);events.push({kind:'user',title:'User',content:payloads.input,duration:null})}
  for(const span of ordered){
    const io=spanIO(span),attrs=span.attributes||{};
    if(io.kind==='context')events.push({kind:'context',title:'Runtime context',content:[attrs['openclaw.context.message_count']!=null?`${attrs['openclaw.context.message_count']} messages`:null,attrs['openclaw.context.system_prompt_chars']!=null?`${formatNumber(attrs['openclaw.context.system_prompt_chars'])} prompt chars`:null,attrs['openclaw.model_call.prompt.tool_definitions_count']!=null?`${attrs['openclaw.model_call.prompt.tool_definitions_count']} tools`:null].filter(Boolean).join(' · ')||'Context assembled',duration:span.duration_ms,span});
    if(io.kind==='model'){
      for(const message of messageList(io.input))if(message.role==='user'&&!seenUsers.has(message.content)){seenUsers.add(message.content);events.push({kind:'user',title:'User',content:message.content,duration:null,span})}
      const outputs=messageList(io.output),model=attrs['openclaw.model']||attrs['gen_ai.request.model']||'Model';
      for(const message of outputs)events.push({kind:'assistant',title:model,content:message.content,input:io.input,output:io.output,model,duration:span.duration_ms,inputTokens:attrs['gen_ai.usage.input_tokens']??attrs['openclaw.model_call.usage.prompt_tokens'],outputTokens:attrs['gen_ai.usage.output_tokens']??attrs['openclaw.model_call.usage.output_tokens'],span});
      // 各平台遥测只上报 token/耗时、不上报消息正文时，这里是一行 LLM 调用记录，而不是空的助手回复
      if(!outputs.length)events.push({kind:'llm',title:model,content:'未上报消息内容',model,input:io.input,output:io.output,duration:span.duration_ms,inputTokens:attrs['gen_ai.usage.input_tokens']??attrs['openclaw.model_call.usage.prompt_tokens']??attrs['input_tokens'],outputTokens:attrs['gen_ai.usage.output_tokens']??attrs['openclaw.model_call.usage.output_tokens']??attrs['output_tokens'],span});
    }
    if(io.kind==='tool')events.push({kind:'tool',title:attrs['gen_ai.tool.name']||attrs['openclaw.tool.name']||attrs['tool.name']||'tool',input:io.input,output:io.output,content:io.output??io.input??'',duration:span.duration_ms,error:span.status==='error',span});
  }
  if(!events.some(event=>event.kind==='user')&&payloads.input)events.unshift({kind:'user',title:'User',content:payloads.input});
  const finalContent=events.filter(event=>event.kind==='assistant').at(-1)?.content||'';
  if(payloads.output&&payloads.output.trim()!==finalContent.trim())events.push({kind:'assistant',title:'Final answer',content:payloads.output,output:payloads.output,final:true,duration:null});
  return events;
}
const recordKind=event=>({user:'USER',context:'CONTEXT',assistant:'ASSISTANT',tool:'TOOL',llm:'LLM'}[event.kind]||event.kind.toUpperCase());
const oneLine=value=>String(value??'').replace(/\s+/g,' ').trim();
function trajectoryRow(event,index){const preview=oneLine(event.kind==='tool'?(event.input??event.output):event.content);return`<button class="trajectory-row ${event.error?'error':''}" data-event-index="${index}"><span class="trajectory-index">#${index+1}</span><span class="trajectory-kind ${event.kind}">${recordKind(event)}</span><span class="trajectory-content"><b>${escapeHtml(event.kind==='user'?preview:event.title)}</b>${event.kind==='user'||!preview?'':`<small>${escapeHtml(preview)}</small>`}</span><span class="trajectory-time">${event.duration==null?'—':formatDuration(event.duration)}</span></button>`}
function trajectoryInspector(event){
  const attrs=event.span?.attributes||{},input=event.input??(event.kind==='user'?event.content:null),output=event.output??(event.kind==='assistant'?event.content:null);
  const metrics=[event.inputTokens!=null?`${formatNumber(event.inputTokens)} input tokens`:null,event.outputTokens!=null?`${formatNumber(event.outputTokens)} output tokens`:null,event.duration!=null?formatDuration(event.duration):null].filter(Boolean);
  const summary=`<div class="record-summary"><span class="trajectory-kind ${event.kind}">${recordKind(event)}</span><h4>${escapeHtml(event.title||recordKind(event))}</h4>${metrics.length?`<p>${escapeHtml(metrics.join(' · '))}</p>`:''}</div>`;
  const missingText=event.kind==='llm'?'该平台遥测只上报 token 与耗时，不包含消息正文':'';
  const panels=[['overview','Overview',`<div class="record-overview"><b>${escapeHtml(oneLine(event.content)||event.title||recordKind(event))}</b>${event.error?'<span class="record-error">ERROR</span>':''}</div>`],['input','Input',input!=null?`<pre>${escapeHtml(event.kind==='tool'?formatPayload(input):readableMessages(input))}</pre>`:`<p class="muted">${missingText||'No input captured'}</p>`],['output','Output',output!=null?`<pre>${escapeHtml(event.kind==='tool'?formatPayload(output):readableMessages(output))}</pre>`:`<p class="muted">${missingText||'No output captured'}</p>`],['raw','Raw',`<pre>${escapeHtml(JSON.stringify({span:event.span?.name,attributes:attrs},null,2))}</pre>`]];
  return`${summary}<div class="record-tabs">${panels.map(([id,label],i)=>`<button class="${i===0?'active':''}" data-record-tab="${id}">${label}</button>`).join('')}</div>${panels.map(([id,,content],i)=>`<div class="record-panel ${i===0?'active':''}" data-record-panel="${id}">${content}</div>`).join('')}`;
}
function trajectoryLedger(events){return`<div class="trajectory-explorer"><div class="trajectory-ledger"><div class="trajectory-columns"><span>#</span><span>Event</span><span>Content</span><span>Time</span></div>${events.map(trajectoryRow).join('')}</div><section id="trajectoryInspector">${events.length?trajectoryInspector(events[0]):'<p class="muted">No trajectory records</p>'}</section></div>`}
function bindRecordTabs(){const root=$('#trajectoryInspector');if(!root)return;root.querySelectorAll('[data-record-tab]').forEach(button=>button.onclick=()=>{root.querySelectorAll('[data-record-tab]').forEach(item=>item.classList.toggle('active',item===button));root.querySelectorAll('[data-record-panel]').forEach(panel=>panel.classList.toggle('active',panel.dataset.recordPanel===button.dataset.recordTab))})}
function spanDepth(span,byId){let depth=0,parent=span.parent_span_id;while(parent&&byId.has(parent)&&depth<8){depth++;parent=byId.get(parent).parent_span_id}return depth}
function rawSpanIdentity(span){const attrs=span.attributes||{},kind=spanKind(span);if(kind==='model')return{kind:'LLM',name:attrs['openclaw.model']||attrs['gen_ai.request.model']||span.name};if(kind==='tool')return{kind:'TOOL',name:attrs['gen_ai.tool.name']||attrs['openclaw.tool.name']||attrs['tool.name']||span.name};if(kind==='context')return{kind:'CONTEXT',name:'runtime context'};if(span.name.includes('run'))return{kind:'RUN',name:attrs['openclaw.agent']||'openclaw'};return{kind:'SPAN',name:span.name}}
function spanTree(detail){const ordered=[...detail.spans].sort((a,b)=>Number(a.started_at_ns)-Number(b.started_at_ns)),byId=new Map(ordered.map(span=>[span.span_id,span]));return ordered.map((span,index)=>{const item=rawSpanIdentity(span),depth=spanDepth(span,byId);return`<button class="span-node ${span.status==='error'?'error':''}" data-span-index="${index}" data-span-id="${escapeHtml(span.span_id)}" style="--depth:${depth}"><span class="raw-span-kind">${item.kind}</span><span><b>${escapeHtml(item.name)}</b></span><em>${formatDuration(span.duration_ms)}</em></button>`}).join('')}
function spanInspector(span){
  const attrs=span.attributes||{},io=spanIO(span),entries=Object.entries(attrs),identity=rawSpanIdentity(span);
  const input=io.input!=null?`<div class="span-io-card"><label>Input</label><pre>${escapeHtml(io.isTool?formatPayload(io.input):readableMessages(io.input))}</pre></div>`:'';
  const output=io.output!=null?`<div class="span-io-card"><label>Output</label><pre>${escapeHtml(io.isTool?formatPayload(io.output):readableMessages(io.output))}</pre></div>`:'';
  return`<div class="span-inspector-head"><div><span class="raw-span-kind">${identity.kind}</span><h4>${escapeHtml(identity.name)}</h4><code>${escapeHtml(span.name)}</code></div><div><b>${formatDuration(span.duration_ms)}</b><span>${span.status==='error'?'ERROR':'OK'}</span></div></div>${input}${output}<div class="span-attribute-section"><div><b>Attributes</b><span>${entries.length}</span></div><dl>${entries.map(([key,value])=>`<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(typeof value==='object'?JSON.stringify(value):value)}</dd>`).join('')||'<p class="muted">No attributes</p>'}</dl></div>`;
}
function traceExplorer(detail){
  const events=conversationEvents(detail),spans=[...detail.spans].sort((a,b)=>Number(a.started_at_ns)-Number(b.started_at_ns));
  return`<div class="trace-view-tabs"><button class="active" data-trace-view="conversation">Trajectory <span>${events.length}</span></button><button data-trace-view="spans">Raw Spans <span>${spans.length}</span></button></div><div class="trace-view active" data-trace-panel="conversation">${trajectoryLedger(events)}</div><div class="trace-view" data-trace-panel="spans"><div class="span-explorer"><nav>${spanTree(detail)}</nav><section id="spanInspector">${spans.length?spanInspector(spans[0]):'<p class="muted">No spans</p>'}</section></div></div>`;
}
function bindTraceExplorer(detail){
  const spans=[...detail.spans].sort((a,b)=>Number(a.started_at_ns)-Number(b.started_at_ns)),events=conversationEvents(detail);
  $$('[data-trace-view]').forEach(button=>button.onclick=()=>{$$('[data-trace-view]').forEach(item=>item.classList.toggle('active',item===button));$$('[data-trace-panel]').forEach(panel=>panel.classList.toggle('active',panel.dataset.tracePanel===button.dataset.traceView))});
  $$('.trajectory-row').forEach(button=>button.onclick=()=>{$$('.trajectory-row').forEach(item=>item.classList.toggle('active',item===button));const event=events[Number(button.dataset.eventIndex)];if(event){$('#trajectoryInspector').innerHTML=trajectoryInspector(event);bindRecordTabs()}});
  $$('.span-node').forEach(button=>button.onclick=()=>{$$('.span-node').forEach(item=>item.classList.toggle('active',item===button));const span=spans[Number(button.dataset.spanIndex)];if(span)$('#spanInspector').innerHTML=spanInspector(span)});
  $$('[data-evidence-span]').forEach(button=>button.onclick=()=>{const target=$$('.span-node').find(node=>node.dataset.spanId===button.dataset.evidenceSpan);if(!target)return;const spansTab=$('[data-trace-view="spans"]');spansTab?.click();target.click();target.scrollIntoView({block:'center',behavior:'smooth'})});
  $('.trajectory-row')?.classList.add('active');bindRecordTabs();
  $('.span-node')?.classList.add('active');
}

const pages={
  overview:{title:'工作台',desc:'管理 Agent、数据集与评测实验'},
  agents:{title:'Agent',desc:'接入并管理需要评测的 Agent',action:'接入 Agent'},
  datasets:{title:'数据集',desc:'管理评测 Case、期望输出与验收标准',action:'创建数据集'},
  judges:{title:'评测模型',desc:'配置独立的 LLM-as-a-Judge 评分模型',action:'添加模型'},
  evaluators:{title:'评测指标',desc:'定义每个 Case 如何评分，以及达到多少分才算通过',action:'创建指标'},
  experiments:{title:'评测实验',desc:'选择 Agent、数据集与评测模型，创建可重复实验',action:'创建实验'},
  runs:{title:'运行记录',desc:'查看真实采集的 Trace、Token、耗时与评测结论'},
  guide:{title:'接入手册',desc:'把 Agent 的 Trace 接入平台，并配置一键运行实验'}
};

function route(page){if(!pages[page])page='overview';state.page=page;$$('.page').forEach(node=>node.classList.toggle('active',node.id===`page-${page}`));$$('#nav a').forEach(node=>node.classList.toggle('active',node.dataset.page===page));$('#pageTitle').textContent=pages[page].title;$('#pageDesc').textContent=pages[page].desc;const action=$('#headerAction');action.classList.toggle('hidden',!pages[page].action);action.textContent=pages[page].action||'';location.hash=page;loadPage(page)}
async function loadPage(page){try{if(page==='overview')await loadOverview();if(page==='agents')await loadAgents();if(page==='datasets')await loadDatasets();if(page==='judges')await loadJudgeModels();if(page==='evaluators')await loadEvaluators();if(page==='experiments')await loadExperiments();if(page==='runs')await loadRuns();if(page==='guide')renderGuide()}catch(error){toast(error.message)}}

async function loadOverview(){const [overview,runs]=await Promise.all([api('/api/overview'),api('/api/traces?limit=5')]);const t=overview.totals;$('#overviewStats').innerHTML=[['运行次数',t.runs],['业务成功率',t.runs?`${(t.success_rate*100).toFixed(1)}%`:'—'],['总 Token',formatNumber(t.tokens)],['P95 耗时',formatDuration(t.p95_duration)]].map(x=>`<div class="stat"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');renderRuns($('#recentRuns'),runs,'还没有运行数据','完成 Agent 接入并执行任务后，Trace 会显示在这里。')}

async function loadAgents(){state.agents=await api('/api/agents');const node=$('#agentList');if(!state.agents.length){node.innerHTML=empty('◇','还没有接入 Agent','选择 Agent 平台与运行方式，完成 Trace、执行和资源采集配置。','接入 Agent','agent');return}const runtimeLabels={openclaw:'OpenClaw',claude_code:'Claude Code',dsh:'DeepSeek Harness',generic:'通用 GenAI'},modeLabels={gateway:'Gateway',local_cli:'Local CLI',remote_worker:'Remote Worker',observe_only:'仅观测'};node.innerHTML=state.agents.map(agent=>{const runtime=agent.runtime_type||agent.protocol||'openclaw',mode=agent.execution_mode||'gateway',observed=!!agent.last_run_at_ns,executable=['gateway','local_cli'].includes(mode),resource=mode==='local_cli'?'运行时采集':mode==='gateway'?'Gateway 指标':'未接入';return `<article class="resource-card agent-card"><div class="resource-icon agent-runtime-icon">${runtime==='openclaw'?'O':runtime==='claude_code'?'C':runtime==='dsh'?'D':'G'}</div><div class="resource-main"><div class="agent-title"><h3>${escapeHtml(agent.name)}</h3><span class="tag">${runtimeLabels[runtime]||escapeHtml(runtime)}</span></div><p>${escapeHtml(agent.description||`${modeLabels[mode]||mode} 接入`)}</p><div class="capability-row"><span class="capability ${observed?'active':''}">Trace ${observed?'已连接':'待验证'}</span><span class="capability ${executable?'active':''}">${executable?'可执行':'仅观测'}</span><span class="capability ${mode==='local_cli'?'active':''}">资源 ${resource}</span></div></div><div class="resource-meta"><div><b>${agent.run_count}</b>运行次数</div><div><b>${agent.last_run_at_ns?nsDate(agent.last_run_at_ns):'—'}</b>最近运行</div><div><span class="tag">${modeLabels[mode]||escapeHtml(mode)}</span></div><div class="card-actions"><button class="button small" data-edit-agent="${agent.agent_id}">编辑</button><button class="button small danger" data-delete-agent="${agent.agent_id}">删除</button></div></div></article>`}).join('');$$('[data-edit-agent]').forEach(button=>button.onclick=event=>{event.stopPropagation();openAgentEditor(button.dataset.editAgent)});$$('[data-delete-agent]').forEach(button=>button.onclick=event=>{event.stopPropagation();const agent=state.agents.find(item=>item.agent_id===button.dataset.deleteAgent);confirmDelete(`/api/agents/${button.dataset.deleteAgent}`,`Agent「${agent?.name||''}」`,loadAgents)})}

async function loadDatasets(){state.datasets=await api('/api/datasets');const node=$('#datasetList');if(!state.datasets.length){node.innerHTML=empty('▤','还没有数据集','创建一个数据集，并添加真实业务 Case、期望输出和验收断言。','创建数据集','dataset');return}node.innerHTML=state.datasets.map(dataset=>`<article class="resource-card" data-dataset="${dataset.dataset_id}"><div class="resource-icon">D</div><div class="resource-main"><h3>${escapeHtml(dataset.name)}</h3><p>${escapeHtml(dataset.description||'暂无描述')}</p></div><div class="resource-meta"><div><b>${dataset.case_count}</b>Cases</div><div><b>${formatDate(dataset.created_at)}</b>创建时间</div><div class="card-actions"><button class="button small" data-edit-dataset="${dataset.dataset_id}">编辑</button><button class="button small danger" data-delete-dataset="${dataset.dataset_id}">删除</button></div><em>›</em></div></article>`).join('');$$('[data-dataset]').forEach(card=>card.onclick=()=>openDataset(card.dataset.dataset));$$('[data-edit-dataset]').forEach(button=>button.onclick=event=>{event.stopPropagation();openDatasetEditor(button.dataset.editDataset)});$$('[data-delete-dataset]').forEach(button=>button.onclick=event=>{event.stopPropagation();const dataset=state.datasets.find(item=>item.dataset_id===button.dataset.deleteDataset);confirmDelete(`/api/datasets/${button.dataset.deleteDataset}`,`数据集「${dataset?.name||''}」`,loadDatasets)})}

async function loadJudgeModels(){state.judgeModels=await api('/api/judge-models');const node=$('#judgeList');if(!state.judgeModels.length){node.innerHTML=empty('✦','还没有评测模型','添加一个 OpenAI 兼容模型，用于对 Agent 输出进行独立评分。','添加模型','judge');return}node.innerHTML=state.judgeModels.map(item=>`<article class="resource-card"><div class="resource-icon judge-icon">J</div><div class="resource-main"><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.provider)} · <span class="mono">${escapeHtml(item.model)}</span></p><small class="endpoint-line">${escapeHtml(item.base_url)}${item.last_tested_at?` · 最近测试 ${formatDate(item.last_tested_at)}`:''}</small>${item.last_error?`<small class="connection-error">${escapeHtml(item.last_error)}</small>`:''}</div><div class="resource-meta"><div><b>${item.last_latency_ms==null?'—':formatDuration(item.last_latency_ms)}</b>连接延迟</div><div><span class="tag ${item.status==='connected'?'success':item.status==='error'?'error':''}">${escapeHtml(item.status==='connected'?'连接正常':item.status==='error'?'连接失败':'未测试')}</span></div><button class="button small" data-test-judge="${item.judge_model_id}">测试连通性</button><div class="card-actions"><button class="button small" data-edit-judge="${item.judge_model_id}">编辑</button><button class="button small danger" data-delete-judge="${item.judge_model_id}">删除</button></div></div></article>`).join('');$$('[data-test-judge]').forEach(button=>button.onclick=event=>testJudgeModel(event,button.dataset.testJudge));$$('[data-edit-judge]').forEach(button=>button.onclick=event=>{event.stopPropagation();openJudgeEditor(button.dataset.editJudge)});$$('[data-delete-judge]').forEach(button=>button.onclick=event=>{event.stopPropagation();const judge=state.judgeModels.find(item=>item.judge_model_id===button.dataset.deleteJudge);confirmDelete(`/api/judge-models/${button.dataset.deleteJudge}`,`评测模型「${judge?.name||''}」`,loadJudgeModels)})}

async function testJudgeModel(event,id){event.stopPropagation();const button=event.currentTarget,original=button.textContent;button.disabled=true;button.textContent='测试中…';try{const result=await api(`/api/judge-models/${id}/test`,{method:'POST'});toast(`连接成功 · ${formatDuration(result.latency_ms)}`)}catch(error){toast(`连接失败：${error.message}`)}finally{button.disabled=false;button.textContent=original;await loadJudgeModels()}}

async function loadEvaluators(){
  state.evaluators=await api('/api/evaluators');
  const dimension=$('#evaluatorDimension')?.value||'',type=$('#evaluatorType')?.value||'';
  const items=state.evaluators.filter(item=>(!dimension||item.dimension===dimension)&&(!type||item.evaluator_type===type));
  const dimensionLabels={result:'结果',process:'过程',efficiency:'效率',risk:'风险'};
  const typeLabels={rule:'规则',trajectory_rule:'轨迹规则',llm:'LLM Judge',code:'Code',human:'人工'};
  const custom=state.evaluators.filter(item=>!item.is_builtin).length;
  $('#evaluatorSummary').innerHTML=`<span><b>${state.evaluators.length}</b>项指标</span><span><b>${custom}</b>项自定义</span>`;
  $('#evaluatorList').innerHTML=items.map(item=>`<article class="evaluator-card interactive" role="button" tabindex="0" data-edit-evaluator="${item.evaluator_id}"><div class="evaluator-card-head"><span class="dimension-pill ${item.dimension}">${dimensionLabels[item.dimension]||item.dimension}</span><span class="tag">${typeLabels[item.evaluator_type]||item.evaluator_type}</span></div><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.description||'暂无说明')}</p><div class="evaluator-rule"><span>判定方式</span><b>${escapeHtml(evaluatorRuleSummary(item))}</b></div><div class="evaluator-card-foot"><span>通过分 ≥ ${item.pass_threshold}</span><span>${item.evaluator_type==='llm'?'调用评测模型':'不额外调用模型'}</span><button class="button small" type="button">编辑</button><button class="button small danger" type="button" data-delete-evaluator="${item.evaluator_id}">删除</button></div></article>`).join('')||`<div class="panel empty"><h3>没有匹配的评测指标</h3><p>调整筛选条件或创建自定义指标。</p></div>`;
  $$('[data-edit-evaluator]').forEach(card=>{card.onclick=()=>openEvaluatorEditor(card.dataset.editEvaluator);card.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openEvaluatorEditor(card.dataset.editEvaluator)}}});
  $$('[data-delete-evaluator]').forEach(button=>button.onclick=event=>{event.stopPropagation();const item=state.evaluators.find(value=>value.evaluator_id===button.dataset.deleteEvaluator);confirmDelete(`/api/evaluators/${button.dataset.deleteEvaluator}`,`评测指标「${item?.name||''}」`,loadEvaluators)});
}

function evaluatorRuleSummary(item){
  const config=item.config||{},operators={lt:'小于',le:'不超过',eq:'等于',ge:'不少于',gt:'大于'};
  if(item.evaluator_type==='llm')return `Judge 评分达到 ${item.pass_threshold}`;
  if(item.evaluator_type==='human')return `由人工给出评分，达到 ${item.pass_threshold}`;
  if(item.evaluator_type==='code')return config.expression||'执行受限表达式';
  if(config.operator==='output_exact')return '实际输出与参考答案完全一致';
  if(config.field)return `${config.field} ${operators[config.operator]||config.operator||'比较'} ${config.value??'—'}`;
  return '按运行轨迹和规则判定';
}

async function loadExperiments(){state.experiments=await api('/api/experiments');const node=$('#experimentList');if(!state.experiments.length){node.innerHTML=empty('◎','还没有评测实验','选择 Agent、数据集和一组评测指标，创建第一组评测实验。','创建实验','experiment');return}node.innerHTML=state.experiments.map(exp=>`<article class="resource-card interactive" role="button" tabindex="0" aria-label="查看实验 ${escapeHtml(exp.name)}" data-experiment="${exp.experiment_id}"><div class="resource-icon">E</div><div class="resource-main"><h3>${escapeHtml(exp.name)}</h3><p>${escapeHtml(exp.agent_name)} · ${escapeHtml(exp.dataset_name)}</p><small class="endpoint-line">${exp.evaluator_names?`评测指标：${escapeHtml(exp.evaluator_names)}`:`Judge：${escapeHtml(exp.judge_model_name||'未配置')}`}</small></div><div class="resource-meta"><div><b>${exp.run_count}</b>运行记录</div><div><b>${exp.evaluator_count||0}</b>评测指标</div><div><span class="tag ${exp.status==='completed'?'success':''}">${escapeHtml(exp.status)}</span></div><button class="button small" data-run-experiment="${exp.experiment_id}">运行</button><button class="button small danger" data-delete-experiment="${exp.experiment_id}">删除</button><em>›</em></div></article>`).join('');$$('[data-delete-experiment]').forEach(button=>button.onclick=event=>{event.stopPropagation();const exp=state.experiments.find(item=>item.experiment_id===button.dataset.deleteExperiment);confirmDelete(`/api/experiments/${button.dataset.deleteExperiment}`,`评测实验「${exp?.name||''}」`,loadExperiments)})}

async function loadMetrics(){state.metrics=state.metrics||await api('/api/metrics/catalog');const data=state.metrics,counts=Object.fromEntries(data.dimensions.map(d=>[d.key,data.metrics.filter(m=>m.dimension===d.key).length]));$('#metricSummary').innerHTML=data.dimensions.map(d=>`<article><span>${escapeHtml(d.name)}</span><b>${counts[d.key]} 项</b><p>${escapeHtml(d.question)}</p></article>`).join('');$('#metricCatalog').innerHTML=data.dimensions.map(d=>`<section class="metric-group"><div class="metric-group-title"><b>${escapeHtml(d.name)}指标</b><span>${escapeHtml(d.question)}</span></div><table class="data-table metric-table"><thead><tr><th>指标</th><th>证据来源</th><th>口径</th><th>门禁</th></tr></thead><tbody>${data.metrics.filter(m=>m.dimension===d.key).map(m=>`<tr><td><b>${escapeHtml(m.name)}</b></td><td><span class="source-tag">${escapeHtml(m.source_label)}</span></td><td>${escapeHtml(m.description)}</td><td>${m.gate_recommended?'建议':'—'}</td></tr>`).join('')}</tbody></table></section>`).join('');$('#vendorCompare').innerHTML=`<table class="data-table vendor-table"><thead><tr><th>平台</th><th>设计重点</th><th>强项</th><th>主要缺口</th></tr></thead><tbody>${data.vendors.map(v=>`<tr><td><b>${escapeHtml(v.name)}</b></td><td>${escapeHtml(v.focus)}</td><td>${escapeHtml(v.strength)}</td><td>${escapeHtml(v.gap)}</td></tr>`).join('')}</tbody></table>`}

async function loadRuns(){const runs=await api('/api/runs?limit=100');renderRunHistory($('#runList'),applyRunFilters(runs));scheduleRunPoll()}
function renderRuns(node,runs,title,desc){if(!runs.length){node.innerHTML=`<div class="empty"><div class="empty-icon">≡</div><h3>${title}</h3><p>${desc}</p></div>`;return}node.innerHTML=`<table class="data-table"><thead><tr><th>Case / Trace</th><th>评测上下文</th><th>Agent / 模型</th><th>业务结果</th><th>耗时</th><th>Token</th><th>时间</th></tr></thead><tbody>${runs.map(run=>`<tr data-trace="${run.trace_id}"><td><b>${escapeHtml(run.case_name||run.case_id||'未关联 Case')}</b><br><span class="mono">${run.trace_id.slice(0,12)}…</span></td><td>${run.dataset_name?`<b>${escapeHtml(run.dataset_name)}</b><br><span>${escapeHtml(run.experiment_name||'未关联实验')}</span>`:'<span class="muted">未关联评测集</span>'}</td><td>${escapeHtml(run.agent||'未命名 Agent')}<br><span class="mono">${escapeHtml(run.model||'unknown')}</span></td><td>${status(run.business_status)}</td><td>${formatDuration(run.duration_ms)}</td><td>${formatNumber(run.total_tokens)}</td><td>${nsDate(run.started_at_ns)}</td></tr>`).join('')}</tbody></table>`;node.querySelectorAll('[data-trace]').forEach(row=>row.onclick=()=>openRun(row.dataset.trace))}
let runPollTimer=null;
function stopRunPoll(){if(runPollTimer){clearInterval(runPollTimer);runPollTimer=null}}
function applyRunFilters(runs){const query=($('#runSearch')?.value||'').trim().toLowerCase(),status=$('#runStatus')?.value;if(query)runs=runs.filter(r=>String(r.experiment_name||r.experiment_id||'').toLowerCase().includes(query));if(status)runs=runs.filter(r=>r.status===status);return runs}
function errorLine(value){return value?`<span class="run-error" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`:''}
function runStatusTag(run){if(run.status==='running')return '<span class="status running">运行中</span>';if(run.status==='completed')return '<span class="status passed">已完成</span>';if(run.status==='failed')return `<span class="status failed">失败</span>${errorLine(run.error)}`;return `<span class="status">${escapeHtml(run.status||'未知')}</span>`}
function renderRunHistory(node,runs){if(!runs.length){node.innerHTML=`<div class="empty"><div class="empty-icon">≡</div><h3>还没有运行记录</h3><p>在「评测实验」点击运行后，每次执行会作为一条实验级记录显示在这里。</p></div>`;return}node.innerHTML=`<table class="data-table"><thead><tr><th>实验</th><th>运行批次</th><th>进度 / 结果</th><th>通过率</th><th>状态</th><th>时间</th></tr></thead><tbody>${runs.map(run=>{const prog=run.total?Math.min(100,Math.round(((run.terminal||0)/run.total)*100)):0;const running=run.status==='running';const progressCell=running?`<div class="run-progress"><i style="width:${prog}%"></i></div><span class="muted">${run.terminal||0}/${run.total} 条 case</span>`:`${run.passed} 通过 · ${run.failed} 未过`;const rate=run.pass_rate==null?'—':`${(run.pass_rate*100).toFixed(0)}%`;return `<tr data-exp-run="${run.experiment_id}" data-run-id="${run.run_id}"><td><b>${escapeHtml(run.experiment_name||run.experiment_id)}</b><br><span class="muted">${run.run_sequence?`第 ${run.run_sequence} 次运行`:'一次运行'}</span></td><td><span class="mono">${run.run_id.slice(0,12)}…</span></td><td>${progressCell}</td><td>${rate}</td><td>${runStatusTag(run)}</td><td>${nsDate(run.started_at_ns)}</td></tr>`}).join('')}</tbody></table>`;node.querySelectorAll('[data-run-id]').forEach(row=>row.onclick=()=>openRunBatch(row.dataset.expRun,row.dataset.runId))}
function scheduleRunPoll(){stopRunPoll();if(state.page!=='runs')return;runPollTimer=setInterval(async()=>{const runs=await api('/api/runs?limit=100').catch(()=>null);if(!runs||state.page!=='runs'){stopRunPoll();return}renderRunHistory($('#runList'),applyRunFilters(runs));if(!runs.some(r=>r.status==='running'))stopRunPoll()},2000)}
function status(value){const map={passed:['passed','通过'],failed:['failed','未通过'],ungraded:['','未评测'],running:['','运行中'],awaiting_trace:['','等待 Trace'],evaluating:['','评分中'],runtime_error:['failed','运行错误'],telemetry_missing:['failed','遥测缺失'],evaluation_error:['failed','评分错误']},item=map[value]||['',value||'未知'];return `<span class="status ${item[0]}">${item[1]}</span>`}
function empty(icon,title,desc,action,type){return `<div class="panel empty"><div class="empty-icon">${icon}</div><h3>${title}</h3><p>${desc}</p><button class="button primary" data-empty-action="${type}">${action}</button></div>`}

function field(name,label,type='text',placeholder='',required=true,extra=''){if(type==='textarea')return `<div class="field"><label>${label}</label><textarea name="${name}" placeholder="${placeholder}" ${required?'required':''}></textarea></div>`;return `<div class="field"><label>${label}</label><input name="${name}" type="${type}" placeholder="${placeholder}" ${required?'required':''} ${extra}></div>`}
function agentSnippet(protocol){const origin=location.origin;const map={claude_code:`CLAUDE_CODE_ENABLE_TELEMETRY=1\nCLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1\nOTEL_TRACES_EXPORTER=otlp\nOTEL_METRICS_EXPORTER=otlp\nOTEL_LOGS_EXPORTER=otlp\nOTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\nOTEL_EXPORTER_OTLP_ENDPOINT=${origin}\nOTEL_SERVICE_NAME=claude-code\n\nclaude --print "<任务>"`,dsh:`dsh plugin --profile headless add @loongsuite/dsh-plugin\nOTEL_SERVICE_NAME=dsh-agent\nOTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\nOTEL_EXPORTER_OTLP_ENDPOINT=${origin}\n\ndsh --profile headless "<任务>"`,openclaw:`# openclaw.json 里 diagnostics.otel.endpoint 指向：${origin}\n# 平台「评测实验」一键运行，或：\nopenclaw agent "<任务>"`,generic:`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\nOTEL_EXPORTER_OTLP_ENDPOINT=${origin}\n# 用 opentelemetry-instrumentation-openai 埋点产出标准 gen_ai.* span`};return map[protocol]||map.generic}
function renderAgentSnippet(protocol){const node=$('#agentSnippet');if(node)node.textContent=agentSnippet(protocol)}
function renderAgentSetup(runtime){const form=$('#modalForm');if(!form)return;const mode=form.elements.execution_mode;const modes={openclaw:[['gateway','Gateway'],['observe_only','仅观测']],claude_code:[['local_cli','本地 CLI（推荐）'],['observe_only','仅观测']],dsh:[['local_cli','本地 CLI（推荐）'],['observe_only','仅观测']],generic:[['observe_only','仅观测']]};mode.innerHTML=(modes[runtime]||modes.generic).map(item=>`<option value="${item[0]}">${item[1]}</option>`).join('');const option=form.querySelector('[data-agent-platform-option]'),label=option?.querySelector('label'),input=option?.querySelector('input');if(option){option.classList.toggle('hidden',!['claude_code','dsh'].includes(runtime));if(runtime==='dsh'){label.textContent='Profile（可选）';input.name='profile';input.placeholder='headless'}else{label.textContent='模型（可选）';input.name='model';input.placeholder='使用 Claude Code 默认模型'}}renderAgentMode(mode.value);renderAgentSnippet(runtime)}
function renderAgentMode(mode){const form=$('#modalForm');if(!form)return;const endpoint=form.querySelector('[data-agent-endpoint]'),local=form.querySelector('[data-agent-local]');if(endpoint)endpoint.classList.toggle('hidden',!['gateway','remote_worker'].includes(mode));if(local)local.classList.toggle('hidden',mode!=='local_cli')}
function copyAgentSnippet(){const node=$('#agentSnippet');if(!node||!node.textContent)return;if(navigator.clipboard?.writeText){navigator.clipboard.writeText(node.textContent).then(()=>toast('已复制接入配置')).catch(()=>toast('复制失败，请手动全选复制'))}else{toast('复制失败，请手动全选复制')}}
async function confirmDelete(path,label,after){if(!confirm(`确定删除${label}？此操作不可撤销。`))return;try{await api(path,{method:'DELETE'});toast(`${label}已删除`);if(after)await after()}catch(error){toast(error.message)}}
function openAgentEditor(id){const item=state.agents.find(value=>value.agent_id===id);if(!item)return;let config={};try{config=JSON.parse(item.config_json||'{}')}catch{}openModal('agent');const form=$('#modalForm');form.dataset.agentId=id;$('#modalTitle').textContent='编辑 Agent';$('#modalDesc').textContent='保存后，后续运行实验会使用最新配置；历史 Trace 保留不变。';form.elements['runtime_type'].value=item.runtime_type||item.protocol||'openclaw';renderAgentSetup(form.elements['runtime_type'].value);form.elements['name'].value=item.name||'';form.elements['description'].value=item.description||'';if(item.execution_mode&&[...form.elements['execution_mode'].options].some(option=>option.value===item.execution_mode))form.elements['execution_mode'].value=item.execution_mode;renderAgentMode(form.elements['execution_mode'].value);form.elements['endpoint'].value=item.endpoint||'';form.elements['executable'].value=item.executable||'';form.elements['working_directory'].value=item.working_directory||'';form.elements['extra_args'].value=config.extra_args||'';const platform=form.elements['model']||form.elements['profile'];if(platform)platform.value=(platform.name==='model'?config.model:config.profile)||''}
function openDatasetEditor(id,reopenDataset=''){const item=state.datasets.find(value=>value.dataset_id===id);if(!item)return;openModal('dataset');const form=$('#modalForm');form.dataset.datasetId=id;if(reopenDataset)form.dataset.reopenDataset=reopenDataset;$('#modalTitle').textContent='编辑数据集';$('#modalDesc').textContent='修改名称和描述不会影响已有 Case 与历史运行记录。';form.elements['name'].value=item.name||'';form.elements['description'].value=item.description||''}
function openJudgeEditor(id){const item=state.judgeModels.find(value=>value.judge_model_id===id);if(!item)return;openModal('judge');const form=$('#modalForm');form.dataset.judgeId=id;$('#modalTitle').textContent='编辑评测模型';$('#modalDesc').textContent='API Key 留空表示保持原 Key；修改配置后需要重新测试连通性。';form.elements['name'].value=item.name||'';form.elements['provider'].value=item.provider||'custom';form.elements['base_url'].value=item.base_url||'';form.elements['model'].value=item.model||'';const key=form.elements['api_key'];key.value='';key.required=false;key.placeholder=item.has_api_key?'已保存，留空保持不变':'输入模型服务 API Key'}
function openModal(kind){const forms={
  agent:{title:'接入 Agent',desc:'先选择平台与运行方式，再配置这个 Agent 实例需要的连接信息。',html:field('name','Agent 名称','text','例如：客服 Agent')+field('description','描述','textarea','说明 Agent 的用途',false)+`<div class="form-grid"><div class="field"><label>Agent 平台</label><select name="runtime_type" onchange="renderAgentSetup(this.value)"><option value="openclaw">OpenClaw</option><option value="claude_code">Claude Code</option><option value="dsh">DeepSeek Harness</option><option value="generic">通用 GenAI</option></select></div><div class="field"><label>运行方式</label><select name="execution_mode" onchange="renderAgentMode(this.value)"></select></div></div><div class="field" data-agent-endpoint><label>连接地址</label><input name="endpoint" type="url" placeholder="Gateway 地址"></div><div data-agent-local><div class="form-grid"><div class="field"><label>可执行文件</label><input name="executable" type="text" placeholder="使用 PATH 时可留空"></div><div class="field"><label>工作目录</label><input name="working_directory" type="text" placeholder="Agent 执行任务的目录"></div></div><div class="field" data-agent-platform-option><label>模型（可选）</label><input name="model" type="text" placeholder="使用 Agent 默认模型"></div><div class="field"><label>附加参数（可选）</label><input name="extra_args" type="text" placeholder="例如：--permission-mode default"></div></div><details class="agent-setup-details"><summary>查看遥测接入配置</summary><pre id="agentSnippet" class="snippet"></pre><button type="button" class="button small" onclick="copyAgentSnippet()">复制配置</button></details>`},
  dataset:{title:'创建数据集',desc:'数据集创建后，可以继续添加 Case 和断言。',html:field('name','数据集名称','text','例如：客服核心场景')+field('description','描述','textarea','说明该数据集覆盖的业务范围',false)},
  judge:{title:'添加评测模型',desc:'用于 LLM-as-a-Judge。API Key 仅发送到本机服务端，页面不会回显。',html:field('name','配置名称','text','例如：DeepSeek Judge')+`<div class="field"><label>模型服务</label><select name="provider" required><option value="deepseek">DeepSeek</option><option value="openai">OpenAI 兼容</option><option value="custom">自定义服务</option></select></div>`+field('base_url','API Base URL','url','https://api.deepseek.com',true,'value="https://api.deepseek.com"')+field('model','模型名称','text','例如：deepseek-chat',true,'value="deepseek-chat"')+field('api_key','API Key','password','输入模型服务 API Key',true,'autocomplete="new-password"')},
  evaluator:{title:'创建评测指标',desc:'定义这个指标检查什么、如何计算，以及多少分算通过。',html:field('name','指标名称','text','例如：数值答案一致性')+field('description','指标说明','textarea','用一句话说明检查目标和适用场景',false)+`<div class="form-grid"><div class="field"><label>执行方式</label><select name="evaluator_type" required><option value="llm">LLM Judge</option><option value="rule">规则</option><option value="trajectory_rule">轨迹规则</option><option value="code">Code（受限表达式）</option><option value="human">人工</option></select></div><div class="field"><label>指标维度</label><select name="dimension" required><option value="result">结果</option><option value="process">过程</option><option value="efficiency">效率</option><option value="risk">风险</option></select></div></div>`+`<div class="form-grid form-grid-three">${field('score_min','最低分','number','0',true,'value="0" step="0.1"')}${field('score_max','最高分','number','1',true,'value="1" step="0.1"')}${field('pass_threshold','通过阈值','number','0.8',true,'value="0.8" step="0.1"')}</div>`+selectField('judge_model_id','Judge 模型',state.judgeModels,'judge_model_id','name',false)+field('prompt_template','评分标准 / Prompt','textarea','LLM 可引用 {{query}}、{{reference}}、{{response}}、{{trajectory}}',false)+field('code_expression','Code 表达式','textarea','例如：normalize_number(response) == normalize_number(reference)',false)+`<div class="form-grid form-grid-three">${field('config_field','Trace 字段','text','如 total_tokens',false)}${field('config_operator','比较方式','text','lt / le / eq / ge / gt',false)}${field('config_value','比较值','number','如 10000',false,'step="0.1"')}</div>`+field('config_json','高级配置（JSON）','textarea','例如：{"gate": true}',false)},
  experiment:{title:'创建评测实验',desc:'实验固定 Agent、数据集、Judge 模型和评测指标组合。',html:field('name','实验名称','text','例如：客服 Agent v2 回归')+selectField('agent_id','选择 Agent',state.agents,'agent_id','name')+selectField('dataset_id','选择数据集',state.datasets,'dataset_id','name')+selectField('judge_model_id','默认 Judge 模型',state.judgeModels,'judge_model_id','name',false)+evaluatorChecklist(state.evaluators)}
  };const form=forms[kind];if(!form)return;$('#modalTitle').textContent=form.title;$('#modalDesc').textContent=form.desc;$('#modalForm').innerHTML=`<div class="form-body">${form.html}</div><div class="form-actions"><button type="button" class="button" data-cancel>取消</button><button class="button primary" type="submit">确认</button></div>`;$('#modalForm').dataset.kind=kind;delete $('#modalForm').dataset.evaluatorId;delete $('#modalForm').dataset.agentId;delete $('#modalForm').dataset.datasetId;delete $('#modalForm').dataset.judgeId;delete $('#modalForm').dataset.caseId;delete $('#modalForm').dataset.reopenDataset;if(kind==='agent')renderAgentSetup($('#modalForm').elements.runtime_type.value);$('#modalBackdrop').classList.remove('hidden');$('[data-cancel]').onclick=closeModal}
function selectField(name,label,items,key,text,required=true){return `<div class="field"><label>${label}${required?'':' <small>（可选）</small>'}</label><select name="${name}" ${required?'required':''}><option value="">请选择</option>${items.map(item=>`<option value="${item[key]}">${escapeHtml(item[text])}</option>`).join('')}</select></div>`}
function evaluatorChecklist(items){const dimensions={result:'结果',process:'过程',efficiency:'效率',risk:'风险'};return `<div class="field"><label>评测指标 <small>（可多选）</small></label><div class="check-list">${items.map(item=>`<label><input type="checkbox" name="evaluator_ids" value="${item.evaluator_id}"><span><b>${escapeHtml(item.name)}</b><small>${dimensions[item.dimension]||item.dimension} · 通过分 ≥ ${item.pass_threshold}</small></span></label>`).join('')}</div></div>`}
function openEvaluatorEditor(id){const item=state.evaluators.find(value=>value.evaluator_id===id);if(!item)return;openModal('evaluator');const form=$('#modalForm'),config=item.config||{};form.dataset.evaluatorId=id;$('#modalTitle').textContent='编辑评测指标';$('#modalDesc').textContent='预置指标和自定义指标都可以直接修改，保存后后续实验使用最新配置。';['name','description','evaluator_type','dimension','score_min','score_max','pass_threshold','judge_model_id','prompt_template'].forEach(name=>{if(form.elements[name])form.elements[name].value=item[name]??''});form.elements.code_expression.value=config.expression||'';form.elements.config_field.value=config.field||'';form.elements.config_operator.value=config.operator||'';form.elements.config_value.value=config.value??'';form.elements.config_json.value=JSON.stringify(config,null,2)}
function closeModal(){$('#modalBackdrop').classList.add('hidden')}
async function submitModal(event){event.preventDefault();const form=event.currentTarget,kind=form.dataset.kind,formData=new FormData(form),data=Object.fromEntries(formData);if(kind==='experiment')data.evaluator_ids=formData.getAll('evaluator_ids');try{if(kind==='evaluator'){let config={};if(data.config_json?.trim())config=JSON.parse(data.config_json);if(data.config_field)config.field=data.config_field;if(data.config_operator)config.operator=data.config_operator;if(data.config_value!=='')config.value=Number(data.config_value);if(data.code_expression)config.expression=data.code_expression;data.config=config;delete data.config_field;delete data.config_operator;delete data.config_value;delete data.code_expression;delete data.config_json}let path={agent:'/api/agents',dataset:'/api/datasets',judge:'/api/judge-models',evaluator:'/api/evaluators',experiment:'/api/experiments'}[kind],method='POST',message='创建成功';if(kind==='evaluator'&&form.dataset.evaluatorId){path+=`/${form.dataset.evaluatorId}`;method='PUT';message='评测指标已更新'}else if(kind==='agent'&&form.dataset.agentId){path+=`/${form.dataset.agentId}`;method='PUT';message='Agent 已更新'}else if(kind==='dataset'&&form.dataset.datasetId){path+=`/${form.dataset.datasetId}`;method='PUT';message='数据集已更新'}else if(kind==='judge'&&form.dataset.judgeId){path+=`/${form.dataset.judgeId}`;method='PUT';message='评测模型已更新'}await api(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});closeModal();toast(message);loadPage(state.page);if(kind==='dataset'&&form.dataset.reopenDataset)await openDataset(form.dataset.reopenDataset).catch(()=>{})}catch(error){toast(error instanceof SyntaxError?'高级配置不是有效 JSON':error.message)}}

let experimentPollTimer=null,activeExperimentId=null;
function stopExperimentPoll(){if(experimentPollTimer){clearInterval(experimentPollTimer);experimentPollTimer=null}}
function experimentButtonState(runState){
  if(!runState||runState.state==='idle')return{text:'运行实验',disabled:false,note:'逐条执行数据集中的 Case，采集 Trace 后自动评分'};
  if(runState.state==='running')return{text:'运行中…',disabled:true,note:`已完成 ${runState.completed}/${runState.total}${runState.current_case?`，当前：${runState.current_case}`:''}`};
  if(runState.state==='completed')return{text:'再次运行',disabled:false,note:`上次已完成 ${runState.completed}/${runState.total} 条；再次运行会保留新的独立记录`};
  return{text:'再次运行',disabled:false,note:`上次运行失败：${runState.error||'未知错误'}`};
}
function experimentRunMap(runs){const map={};runs.forEach(run=>{if(run.run_id)map[run.run_id]=run});return map}
function batchCaseRows(batch,runMap){return (batch.attempts||[]).map(attempt=>{const trace=runMap[attempt.run_id]||{},state=attempt.status||trace.business_status,id=attempt.trace_id||attempt.run_id||'',tokens=trace.total_tokens??attempt.total_tokens,duration=trace.duration_ms??attempt.duration_ms;return `<tr ${attempt.trace_id?`data-trace="${attempt.trace_id}"`:''}><td><b>${escapeHtml(attempt.case_name||attempt.case_id)}</b><br><span class="mono">${escapeHtml(String(id).slice(0,12))}…</span>${errorLine(attempt.error_message)}</td><td>${status(state)}</td><td>${attempt.score??'—'}</td><td>${tokens==null?'—':formatNumber(tokens)}</td><td>${duration==null?'—':formatDuration(duration)}</td><td>${nsDate(trace.started_at_ns||attempt.started_at_ns)}</td></tr>`}).join('')}
function batchStatusTag(batch){if(batch.status==='running')return '<span class="status running">运行中</span>';if(batch.status==='failed')return '<span class="status failed">失败</span>';if(batch.status==='completed')return '<span class="status passed">已完成</span>';return `<span class="status">${escapeHtml(batch.status||'未知')}</span>`}
function renderRunBatches(batches,runMap){
  if(!batches.length)return '<div class="content-empty"><b>实验尚未运行</b><p>点击“运行实验”后，每次执行都会作为一条独立记录保留在这里。</p></div>';
  return [...batches].reverse().map(batch=>`<div class="run-batch"><div class="run-batch-head"><div><b>第 ${batch.sequence||''} 次运行</b><span class="muted">${nsDate(batch.started_at_ns)} · ${batch.passed}/${batch.total} 通过</span>${errorLine(batch.error)}</div>${batchStatusTag(batch)}</div>${batch.attempts?.length?`<div class="experiment-table-wrap"><table class="data-table"><thead><tr><th>Case</th><th>结果</th><th>分数</th><th>Token</th><th>耗时</th><th>运行时间</th></tr></thead><tbody>${batchCaseRows(batch,runMap)}</tbody></table></div>`:'<p class="muted run-batch-empty">这次运行还没有 Case 记录。</p>'}</div>`).join('');
}
function resourceSeriesHtml(series){const rss={...(series?.rss_series||{})},cpu={...(series?.cpu_series||{})};return `<div class="process-series-title"><b>进程 RSS</b></div>${processChart(rss,{unit:'MiB',axisLabel:'进程 RSS（MiB）',scale:1/1048576,decimals:1,tone:'rss'})}<div class="process-series-title"><b>进程 CPU 利用率</b></div>${processChart(cpu,{unit:'%',axisLabel:'CPU 利用率（%）',scale:100,decimals:1,tone:'cpu'})}`}
function resourceRunSelect(batches,series){const selected=series?.run_id||(batches[batches.length-1]||{}).run_id;return `<div class="resource-run-picker"><label>运行批次</label><select id="resourceRunSelect">${[...batches].reverse().map(batch=>`<option value="${batch.run_id}" ${batch.run_id===selected?'selected':''}>第 ${batch.sequence} 次 · ${nsDate(batch.started_at_ns)} · ${batch.passed}/${batch.total} 通过</option>`).join('')}</select></div>`}
async function changeResourceRun(experimentId,runId){const body=$('#resourceSeriesBody');if(!body)return;body.innerHTML='<p class="muted">加载中…</p>';try{const series=await api(`/api/eval-experiments/${experimentId}/resource-series?run_id=${encodeURIComponent(runId)}`);activeResourceSeries=series;body.innerHTML=resourceSeriesHtml(series)}catch(error){body.innerHTML='<p class="muted">资源曲线加载失败</p>';toast(error.message)}}
function renderExperiment(detail,runState,resourceSeries){
  const exp=detail.experiment,s=detail.summary,btn=experimentButtonState(runState),batches=detail.run_batches||[],runMap=experimentRunMap(detail.runs);
  const covered=s.covered_cases??new Set(detail.runs.map(run=>run.case_id).filter(Boolean)).size,rate=s.pass_rate==null?'—':`${(s.pass_rate*100).toFixed(1)}%`,coverage=s.coverage_rate==null?'—':`${(s.coverage_rate*100).toFixed(1)}%`;
  $('#drawerTitle').textContent=exp.name;$('#drawerDesc').textContent=`${exp.agent_name} · ${exp.dataset_name}`;
  const metricHtml=detail.evaluators.length?detail.evaluators.map(item=>`<div class="evaluation-row"><div><b>${escapeHtml(item.name)}</b><span class="tag">${escapeHtml(item.evaluator_type)}</span></div><strong>通过分 ≥ ${item.pass_threshold}</strong><p>${escapeHtml(item.description)}</p></div>`).join(''):'<p class="muted">该实验没有配置评测指标。</p>';
  const runHtml=renderRunBatches(batches,runMap);
  const resourceHtml=batches.length?`<div class="detail-section"><div class="section-title"><h3>全程资源曲线</h3><span>按选定的一次运行连续展示</span></div>${resourceRunSelect(batches,resourceSeries)}<div id="resourceSeriesBody">${resourceSeriesHtml(resourceSeries)}</div></div>`:'';
  $('#drawerBody').innerHTML=`<div class="experiment-run-bar"><div><b>运行实验</b><p>${escapeHtml(btn.note)}</p></div><button class="button primary" id="runExperimentBtn" ${btn.disabled?'disabled':''}>${btn.text}</button></div><div class="detail-section"><h3>实验汇总</h3><div class="detail-grid detail-grid-four"><div><span>数据集样本</span><b>${detail.cases.length}</b></div><div><span>样本覆盖率</span><b>${coverage}</b></div><div><span>总执行次数</span><b>${s.total}</b></div><div><span>总体通过率</span><b>${rate}</b></div><div><span>运行错误</span><b>${s.errors??0}</b></div><div><span>总 Token</span><b>${formatNumber(s.total_tokens)}</b></div><div><span>平均耗时</span><b>${formatDuration(s.avg_duration_ms)}</b></div></div></div><div class="detail-section"><div class="section-title"><h3>本实验使用的评测指标</h3><span>Case 断言与全部自动指标均通过，Case 才算通过</span></div><div class="evaluation-list">${metricHtml}</div></div><div class="detail-section"><div class="section-title"><h3>运行记录</h3><span>每次运行一条记录，其中的 Case 归在同一次运行下</span></div>${runHtml}</div>${resourceHtml}`;
}
function bindExperimentControls(id){const button=$('#runExperimentBtn');if(button)button.onclick=()=>startExperiment(id);$$('#drawerBody [data-trace]').forEach(row=>row.onclick=()=>openRun(row.dataset.trace,()=>openExperiment(id)));const select=$('#resourceRunSelect');if(select)select.onchange=()=>changeResourceRun(id,select.value)}
let activeResourceSeries=null;
async function openRunBatch(experimentId,runId){
  stopExperimentPoll();activeExperimentId=null;setDrawerBack(null);
  const [detail,series]=await Promise.all([
    api('/api/eval-experiments/'+experimentId),
    api(`/api/eval-experiments/${experimentId}/resource-series?run_id=${encodeURIComponent(runId)}`).catch(()=>null)
  ]);
  const batch=(detail.run_batches||[]).find(item=>item.run_id===runId);
  if(!batch){toast('没有找到这次运行记录');return}
  const exp=detail.experiment,runMap=experimentRunMap(detail.runs);
  $('#drawerTitle').textContent=`${exp.name} · 第 ${batch.sequence} 次运行`;
  $('#drawerDesc').textContent=`${exp.dataset_name} · ${nsDate(batch.started_at_ns)}`;
  const statusText=batch.status==='failed'?'失败':batch.status==='completed'?'已完成':batch.status==='running'?'运行中':batch.status;
  const elapsed=batch.ended_at_ns?formatDuration((Number(batch.ended_at_ns)-Number(batch.started_at_ns))/1e6):'—';
  const caseHtml=batch.attempts?.length?`<div class="experiment-table-wrap"><table class="data-table"><thead><tr><th>Case</th><th>结果</th><th>分数</th><th>Token</th><th>耗时</th><th>运行时间</th></tr></thead><tbody>${batchCaseRows(batch,runMap)}</tbody></table></div>`:'<div class="content-empty"><b>这次运行还没有 Case 记录</b></div>';
  $('#drawerBody').innerHTML=`<div class="detail-section"><h3>运行汇总</h3><div class="detail-grid detail-grid-four"><div><span>运行序号</span><b>第 ${batch.sequence} 次</b></div><div><span>开始时间</span><b>${nsDate(batch.started_at_ns)}</b></div><div><span>通过 / 总数</span><b>${batch.passed} / ${batch.total}</b></div><div><span>状态</span><b>${escapeHtml(statusText)}</b></div><div><span>本次运行耗时</span><b>${elapsed}</b></div></div>${errorLine(batch.error)}</div><div class="detail-section"><div class="section-title"><h3>本次运行的 Case</h3><span>本次运行包含 ${batch.attempts?.length||0} 条 Case</span></div>${caseHtml}</div><div class="detail-section"><div class="section-title"><h3>本次运行资源曲线</h3><span>按本次运行时间连续展示</span></div><div id="resourceSeriesBody">${resourceSeriesHtml(series)}</div></div>`;
  openDrawer('run');
  $$('#drawerBody [data-trace]').forEach(row=>row.onclick=()=>openRun(row.dataset.trace,()=>openRunBatch(experimentId,runId)));
}
async function openExperiment(id){stopExperimentPoll();activeExperimentId=id;setDrawerBack(null);const [detail,runState,resourceSeries]=await Promise.all([api('/api/eval-experiments/'+id),api('/api/experiments/'+id+'/run').catch(()=>({state:'idle'})),api('/api/eval-experiments/'+id+'/resource-series').catch(()=>null)]);activeResourceSeries=resourceSeries;renderExperiment(detail,runState,resourceSeries);openDrawer('experiment');bindExperimentControls(id);if(runState.state==='running')pollExperiment(id)}
async function startExperiment(id){const button=$('#runExperimentBtn');if(button){button.disabled=true;button.textContent='启动中…'}try{await api('/api/experiments/'+id+'/run',{method:'POST'});toast('实验已开始运行');pollExperiment(id)}catch(error){toast(error.message);if(button){button.disabled=false;button.textContent='运行实验'}}}
function pollExperiment(id){stopExperimentPoll();experimentPollTimer=setInterval(async()=>{try{const [runState,detail]=await Promise.all([api('/api/experiments/'+id+'/run'),api('/api/eval-experiments/'+id)]);if(activeExperimentId===id&&$('#drawer').classList.contains('open')){const selectedRun=$('#resourceRunSelect')?.value;const series=await api('/api/eval-experiments/'+id+'/resource-series'+(selectedRun?`?run_id=${encodeURIComponent(selectedRun)}`:'')).catch(()=>activeResourceSeries);if(series)activeResourceSeries=series;renderExperiment(detail,runState,activeResourceSeries);bindExperimentControls(id)}if(runState.state!=='running'){stopExperimentPoll();loadPage(state.page);toast(runState.state==='completed'?'实验运行完成':'实验运行失败：'+(runState.error||'未知错误'))}}catch(error){stopExperimentPoll();toast(error.message)}},2000)}
async function startExperimentFromCard(id){try{await api('/api/experiments/'+id+'/run',{method:'POST'});toast('实验已开始运行')}catch(error){if(!/运行中/.test(error.message))toast(error.message)}await openExperiment(id).catch(error=>toast(error.message))}

async function openDataset(id){const detail=await api('/api/datasets/'+id),dataset=detail.dataset;$('#drawerTitle').textContent=dataset.name;$('#drawerDesc').textContent=dataset.description||'评测数据集';$('#drawerBody').innerHTML=`<div class="drawer-actions"><div><b>Cases</b><p class="muted">${detail.cases.length} 条评测用例</p></div><div class="card-actions"><button class="button small" id="editDataset">编辑数据集</button><button class="button primary" id="addCase">添加 Case</button></div></div><div class="case-list">${detail.cases.length?detail.cases.map(item=>`<div class="case-row"><div class="case-row-head"><h4>${escapeHtml(item.name)}</h4><div class="card-actions"><button class="button small" data-edit-case="${item.case_id}">编辑</button><button class="button small danger" data-delete-case="${item.case_id}">删除</button></div></div><p>${escapeHtml(item.input)}</p></div>`).join(''):'<div class="empty"><h3>还没有 Case</h3><p>添加输入、期望输出和验收标准。</p></div>'}</div>`;openDrawer();$('#addCase').onclick=()=>openCaseModal(id);$('#editDataset').onclick=()=>openDatasetEditor(id,id);$$('#drawerBody [data-edit-case]').forEach(button=>button.onclick=()=>openCaseModal(id,detail.cases.find(item=>item.case_id===button.dataset.editCase)));$$('#drawerBody [data-delete-case]').forEach(button=>button.onclick=()=>{const item=detail.cases.find(value=>value.case_id===button.dataset.deleteCase);confirmDelete(`/api/datasets/${id}/cases/${button.dataset.deleteCase}`,`Case「${item?.name||''}」`,async()=>{await openDataset(id);await loadDatasets()})})}
function openCaseModal(datasetId,item){const form=$('#modalForm');$('#modalTitle').textContent=item?'编辑 Case':'添加 Case';$('#modalDesc').textContent='一条 Case 包含任务输入和可选的期望输出。';form.innerHTML=`<div class="form-body">${field('name','Case 名称','text','例如：查询已发货订单')}${field('input','任务输入','textarea','输入给 Agent 的真实任务')}${field('expected_output','期望输出','textarea','可选：参考答案或目标状态',false)}</div><div class="form-actions"><button type="button" class="button" data-cancel>取消</button><button class="button primary" type="submit">${item?'保存':'添加'}</button></div>`;form.dataset.kind='case';form.dataset.datasetId=datasetId;if(item){form.dataset.caseId=item.case_id;form.elements['name'].value=item.name||'';form.elements['input'].value=item.input||'';form.elements['expected_output'].value=item.expected_output||''}else delete form.dataset.caseId;$('#modalBackdrop').classList.remove('hidden');$('[data-cancel]').onclick=closeModal}
async function submitCase(event){const form=event.currentTarget,data=Object.fromEntries(new FormData(form)),datasetId=form.dataset.datasetId,caseId=form.dataset.caseId;await api(`/api/datasets/${datasetId}/cases${caseId?'/'+caseId:''}`,{method:caseId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});closeModal();toast(caseId?'Case 已更新':'Case 已添加');openDataset(datasetId);loadDatasets()}

async function openRun(id,back){
  stopExperimentPoll();activeExperimentId=null;setDrawerBack(back);
  const detail=await api('/api/traces/'+id),run=detail.trace,evaluations=detail.evaluations||[];
  $('#drawerTitle').textContent=run.case_name||run.case_id||run.run_id||'运行详情';
  $('#drawerDesc').textContent=`Trace ${run.trace_id}`;
  const failedEvaluations=evaluations.filter(item=>!item.passed),passedEvaluations=evaluations.filter(item=>item.passed);
  const verdictTitle=run.business_status==='passed'?'本次评测通过':run.business_status==='failed'?'本次评测未通过':'本次运行尚未评测';
  const verdictReason=run.business_status==='passed'?`${passedEvaluations.length} 个自动评估器全部达到各自阈值。`:run.business_status==='failed'?`${failedEvaluations.length} 个评估器未达到阈值：${failedEvaluations.map(item=>item.evaluator).join('、')}。`:'已采集运行轨迹，但没有执行评估器。';
  const verdictHtml=`<div class="verdict-card ${run.business_status}"><div>${status(run.business_status)}</div><section><b>${verdictTitle}</b><p>${escapeHtml(verdictReason)}</p></section><aside><span>综合分</span><strong>${run.score==null?'—':run.score}</strong></aside></div>`;
  const evaluationHtml=evaluations.length?`<div class="detail-section"><div class="section-title"><h3>评测判定</h3><span>全部自动评估器通过，业务结果才算通过</span></div><div class="evaluation-list">${evaluations.map(item=>{const evidenceSpans=item.evidence?.evidence_span_ids||[];return`<div class="evaluation-row ${item.passed?'passed':'failed'}"><div><b>${escapeHtml(item.evaluator)}</b><span class="tag">${escapeHtml(item.evaluator_type)}</span></div><strong>${item.passed?'通过':'失败'} · ${item.score==null?'—':item.score}</strong><p>${escapeHtml(item.reason||'未提供理由')}</p>${evidenceSpans.length?`<div class="evaluation-evidence"><span>证据 Span</span>${evidenceSpans.map(spanId=>`<button class="button small" data-evidence-span="${escapeHtml(spanId)}">${escapeHtml(spanId.slice(0,12))}</button>`).join('')}</div>`:''}</div>`}).join('')}</div></div>`:'';
  const contextHtml=run.dataset_name?`<div class="detail-section"><h3>评测上下文</h3><div class="detail-grid"><div><span>数据集</span><b>${escapeHtml(run.dataset_name)}</b></div><div><span>实验</span><b>${escapeHtml(run.experiment_name||'未关联实验')}</b></div><div><span>Case</span><b>${escapeHtml(run.case_name||run.case_id||'未关联')}</b></div></div></div>`:`<div class="notice-card"><div><b>未关联评测集</b><p>这是一条独立采集的 Trace，没有绑定数据集、实验或 Case，因此只展示可观测指标，不计入实验跑分。</p></div></div>`;
  const trajectoryHtml=`<div class="detail-section trajectory-section"><div class="section-title"><h3>运行轨迹</h3><span>先看对话，再按 Span 下钻完整调用细节</span></div>${traceExplorer(detail)}</div>`;
  const runtime=detail.runtime_metrics||{},rss=runtime.rss_bytes,heap=runtime.heap_used_bytes,cpu=runtime.cpu_core_ratio;
  const resourceSamples=Math.max(0,...[rss,heap,cpu].filter(Boolean).map(item=>item.point_count||0));
  const resourceScope=runtime.scope==='process_tree'?'本次运行进程树':'Gateway 进程',resourceNote=resourceSamples?(runtime.scope==='process_tree'?`数据来自本次 Agent CLI 及其子进程，按 run_id 与运行精确关联。`:`数据来自 ${escapeHtml(runtime.service_name||run.service_name)} Gateway 进程，反映共享服务负载。`):'本次运行没有收到进程资源样本。';
  $('#drawerBody').innerHTML=`${verdictHtml}${contextHtml}${trajectoryHtml}${evaluationHtml}<div class="detail-section"><h3>运行指标</h3><div class="detail-grid detail-grid-four"><div><span>端到端耗时</span><b>${formatDuration(run.duration_ms)}</b></div><div><span>总 Token</span><b>${run.total_tokens==null?'未采集':formatNumber(run.total_tokens)}</b></div><div><span>首 Token / 字节</span><b>${formatDuration(run.ttft_ms)}</b></div><div><span>模型调用</span><b>${run.model_calls??'未采集'}</b></div><div><span>工具调用</span><b>${run.tool_calls??'未采集'}</b></div><div><span>工具错误</span><b>${run.tool_errors??'未采集'}</b></div><div><span>循环调用</span><b>${run.tool_loops??'未采集'}</b></div><div><span>Failover</span><b>${run.failovers??'未采集'}</b></div></div><div class="resource-metric-title"><b>${resourceScope}</b><span>${resourceSamples?`${resourceSamples} 个采样点`:'未采集'}</span></div><div class="detail-grid detail-grid-four"><div><span>RSS 峰值</span><b>${formatBytes(rss?.peak)}</b></div><div><span>Heap 峰值</span><b>${formatBytes(heap?.peak)}</b></div><div><span>CPU 峰值</span><b>${formatRatio(cpu?.peak)}</b></div></div><div class="process-series-title"><b>进程 RSS</b></div>${processChart(runtime.rss_series,{unit:'MiB',axisLabel:'进程 RSS（MiB）',scale:1/1048576,decimals:1,tone:'rss'})}<div class="process-series-title"><b>进程 CPU 利用率</b></div>${processChart(runtime.cpu_series,{unit:'%',axisLabel:'CPU 利用率（%）',scale:100,decimals:1,tone:'cpu'})}<details class="resource-metric-note"><summary>采样口径</summary><p>CPU 以逻辑核等价值展示：100% 约等于占满 1 个逻辑核，多线程进程可以超过 100%。${resourceNote}</p></details></div>`;
  openDrawer('run');
  bindTraceExplorer(detail);
}
function openDrawer(mode=''){$('#drawerBackdrop').classList.remove('hidden');$('#drawer').classList.toggle('run-drawer',mode==='run');$('#drawer').classList.toggle('experiment-drawer',mode==='experiment');$('#drawer').classList.add('open');$('#drawer').setAttribute('aria-hidden','false')}
let drawerBack=null;
function setDrawerBack(fn){drawerBack=fn||null;const button=$('#drawerBack');if(button)button.classList.toggle('hidden',!drawerBack)}
function closeDrawer(){setDrawerBack(null);stopExperimentPoll();activeExperimentId=null;$('#drawerBackdrop').classList.add('hidden');$('#drawer').classList.remove('open');$('#drawer').setAttribute('aria-hidden','true')}
function toast(message){const node=$('#toast');node.textContent=message;node.classList.add('show');clearTimeout(node.timer);node.timer=setTimeout(()=>node.classList.remove('show'),2400)}

async function checkOtlp(){const button=$('#checkOtlp'),dot=$('#otlpStatusDot'),text=$('#otlpStatusText'),detail=$('#otlpStatusDetail');button.disabled=true;button.textContent='检测中';dot.className='otlp-status-dot checking';text.textContent='端到端检测中';detail.textContent='发送、解析、落库、回查';try{const result=await api('/api/health/otlp/test',{method:'POST'});dot.className='otlp-status-dot active';text.textContent='OTLP 可用';detail.textContent=`完整链路通过 · ${result.latency_ms}ms`;toast('OTLP 端到端检测通过：发送、解析、落库和回查均正常')}catch(error){dot.className='otlp-status-dot error';text.textContent='OTLP 不可用';detail.textContent=error.message;toast(error.message)}finally{button.disabled=false;button.textContent='检测'}}

let guidePlatform='openclaw';
function guidePlatformList(origin){
  return [
    {
      key:'openclaw',name:'OpenClaw',icon:'O',tagline:'Gateway 长驻',
      summary:'通过 diagnostics-otel 插件上报 Trace 与指标。平台既可以直接观测 Gateway，也可以在「评测实验」里一键运行，并按 Gateway 进程采集 CPU / RSS。',
      tags:['Trace 观测','评测评分','一键跑实验','Gateway 进程资源'],
      steps:[
        {
          title:'配置 OTLP 上报',
          desc:'在 openclaw.json 的 diagnostics.otel 中把 endpoint 指向平台。endpoint 只写到服务地址，不要带 /v1/traces；captureContent 默认关闭，开启后才会采集 Prompt、响应和工具原文。',
          code:`// openclaw.json
{
  diagnostics: {
    enabled: true,
    otel: {
      endpoint: "${origin}",
      protocol: "http/protobuf",
      serviceName: "openclaw-eval",
      traces: true,
      metrics: true,
      logs: false,
      flushIntervalMs: 1000,
      captureContent: false,
    },
  },
}`
        },
        {
          title:'重启 Gateway 并执行一次真实任务',
          desc:'重启后确认日志中 diagnostics-otel 已加载、没有导出错误，再让 Gateway 完整走一次模型请求。',
          code:'openclaw agent "<任务>"'
        },
        {
          title:'在平台登记 Agent（用于一键跑实验）',
          desc:'打开「Agent」页 → 接入 Agent。只做观测可以选「仅观测」；要在平台里批量执行数据集，选 Gateway 并填写 Gateway 的 HTTP 地址。',
          code:`Agent 平台：OpenClaw
运行方式：Gateway
连接地址：http://127.0.0.1:18789`
        }
      ],
      verify:[
        '「运行记录」出现 service.name 为所配 serviceName 的 Trace，且 total_tokens > 0。',
        '只有健康检查、没有模型 Span 的 Trace 不能当作评测样本。',
        '资源曲线反映共享 Gateway 进程，不是单次请求独占内存。'
      ]
    },
    {
      key:'dsh',name:'DeepSeek Harness',icon:'D',tagline:'Local CLI 推荐',
      summary:'通过 @loongsuite/dsh-plugin 在进程内导出 OTLP。平台推荐用「本地 CLI」方式启动 dsh，逐个执行 Case，并采集整个进程树的 CPU / RSS。',
      tags:['Trace 观测','评测评分','一键跑实验','进程树资源'],
      steps:[
        {
          title:'安装 DSH 采集插件',
          desc:'插件在 DSH 进程内直接导出 OTLP/HTTP，不需要额外部署 Collector。',
          code:'dsh plugin --profile headless add @loongsuite/dsh-plugin'
        },
        {
          title:'设置遥测环境变量并运行',
          desc:'环境变量要对平台启动 dsh 的进程可见；只做观测时，在任意终端按同样配置运行即可。',
          code:`export OTEL_SERVICE_NAME=dsh-agent
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=${origin}

dsh --profile headless "<任务>"`
        },
        {
          title:'在平台登记 Agent',
          desc:'打开「Agent」页 → 平台选 DeepSeek Harness，运行方式选「本地 CLI」。平台负责启动进程、采集进程树资源并读取最终输出，因此 dsh 必须能被平台进程访问。',
          code:`Agent 平台：DeepSeek Harness
运行方式：本地 CLI（推荐）
可执行文件：dsh（留空时使用 PATH）
Profile：headless
工作目录：任务所在目录`
        }
      ],
      verify:[
        '「运行记录」出现 service.name=dsh-agent 的 Trace，且 total_tokens > 0。',
        'DSH 的 Token 口径是 input + output；cache_read 已包含在 input 内，不重复累加。',
        '本地 CLI 运行结束后，运行详情里能看到进程树 RSS / CPU 曲线。'
      ]
    },
    {
      key:'claude_code',name:'Claude Code',icon:'C',tagline:'Local CLI 推荐',
      summary:'Claude Code 内置 OTel 遥测，按 Anthropic 口径上报 token。平台推荐用「本地 CLI」方式执行 claude --print，并采集进程树资源。',
      tags:['Trace 观测','评测评分','一键跑实验','进程树资源'],
      steps:[
        {
          title:'开启 Claude Code 遥测',
          desc:'分布式 Trace 目前是 beta，需要同时打开两个开关。内容默认脱敏；只有显式开启 OTEL_LOG_USER_PROMPTS 等变量才会带原文。',
          code:`export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=${origin}
export OTEL_SERVICE_NAME=claude-code`
        },
        {
          title:'以非交互方式运行一次',
          desc:'--print 适合批量评测；运行结束时平台 Runner 会读取标准输出作为 Agent 最终回答。',
          code:'claude --print "<任务>"'
        },
        {
          title:'在平台登记 Agent',
          desc:'打开「Agent」页 → 平台选 Claude Code，运行方式选「本地 CLI」。可执行文件留空时使用 PATH 中的 claude，也可以用「附加参数」传入权限模式等参数。',
          code:`Agent 平台：Claude Code
运行方式：本地 CLI（推荐）
可执行文件：claude
模型：可选，留空使用 Claude Code 默认模型`
        }
      ],
      verify:[
        '「运行记录」出现 service.name=claude-code 的 Trace，且 total_tokens > 0。',
        'Claude Code 的 Token 口径是 input + output + cache_read + cache_write。',
        '默认不采集原文；需要按轨迹内容评分时，在受控环境开启内容采集变量。'
      ]
    },
    {
      key:'generic',name:'更多平台',icon:'+',tagline:'通用 GenAI',
      summary:'任何输出标准 gen_ai.* Span 的运行时都可以先按通用方式接入；专用适配器会在接收端自动识别，这个页面也会同步补充。',
      tags:['Trace 观测','评测评分','仅观测'],
      steps:[
        {
          title:'设置 OTLP 环境变量',
          desc:'适用于 OpenAI 兼容的 GenAI 应用，或任何已经接入 OpenTelemetry 的 Agent 运行时。',
          code:`export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=${origin}`
        },
        {
          title:'应用侧产出标准 gen_ai.* Span',
          desc:'使用 opentelemetry-instrumentation-openai 或同类埋点，模型、Token 与工具调用会被平台自动归一化。',
          code:'opentelemetry-instrumentation-openai'
        },
        {
          title:'在平台登记 Agent',
          desc:'打开「Agent」页 → 平台选「通用 GenAI」，运行方式只能是「仅观测」：平台接收并评测 Trace，但不会主动执行任务。'
        }
      ],
      verify:[
        '「运行记录」能按 gen_ai.* 字段识别 model / provider 并显示 Token。',
        '新增专用平台后，适配器会自动识别；页面会补上对应的接入步骤。'
      ]
    }
  ];
}

function renderGuide(){
  const origin=location.origin.replace(/\/$/,'');
  const platforms=guidePlatformList(origin);
  const current=platforms.find(item=>item.key===guidePlatform)||platforms[0];
  $('#guideContent').innerHTML=`
    <div class="notice-card guide-intro">
      <div>
        <b>接入 = 让 Agent 把 OTLP Trace 报到 <code>${escapeHtml(origin)}</code></b>
        <p>所有平台统一使用 OTLP/HTTP + http/protobuf，接收地址不要带 <code>/v1/traces</code>。只做观测时完成第 1 步即可；要一键跑实验，再到「Agent」页登记执行方式。</p>
      </div>
      <div class="guide-flow"><span>1 配置上报</span><em>→</em><span>2 登记 Agent</span><em>→</em><span>3 创建实验</span></div>
    </div>
    <div class="guide-tabs">${platforms.map(item=>`<button type="button" class="guide-tab ${item.key===current.key?'active':''}" data-guide-tab="${item.key}"><i>${escapeHtml(item.icon)}</i>${escapeHtml(item.name)}</button>`).join('')}</div>
    <article class="guide-card">
      <header class="guide-card-head">
        <div class="resource-icon">${escapeHtml(current.icon)}</div>
        <div class="guide-card-title"><div class="agent-title"><h3>${escapeHtml(current.name)}</h3><span class="tag">${escapeHtml(current.tagline)}</span></div><p>${escapeHtml(current.summary)}</p></div>
        <div class="guide-tags">${current.tags.map(tag=>`<span class="capability active">${escapeHtml(tag)}</span>`).join('')}</div>
      </header>
      <div class="guide-steps">${current.steps.map((step,index)=>`<section class="guide-step"><div class="guide-step-head"><span>${index+1}</span><h4>${escapeHtml(step.title)}</h4></div><p>${escapeHtml(step.desc)}</p>${step.code?`<div class="guide-code"><button type="button" class="button small guide-copy">复制</button><pre class="snippet">${escapeHtml(step.code)}</pre></div>`:''}</section>`).join('')}</div>
      <div class="guide-verify"><b>接入验证</b><ul>${current.verify.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul></div>
    </article>`;
  $$('[data-guide-tab]').forEach(button=>button.onclick=()=>{guidePlatform=button.dataset.guideTab;renderGuide()});
  $$('.guide-copy').forEach(button=>button.onclick=()=>{
    const text=button.parentElement.querySelector('pre')?.textContent||'';
    if(navigator.clipboard?.writeText){navigator.clipboard.writeText(text).then(()=>toast('已复制接入配置')).catch(()=>toast('复制失败，请手动复制'))}else{toast('复制失败，请手动复制')}
  });
}

function bind(){$$('#nav a').forEach(link=>link.onclick=event=>{event.preventDefault();route(link.dataset.page)});document.addEventListener('click',event=>{const runButton=event.target.closest('[data-run-experiment]');if(runButton){event.preventDefault();event.stopPropagation();startExperimentFromCard(runButton.dataset.runExperiment);return}const target=event.target.closest('[data-goto],[data-empty-action],[data-experiment]');if(target?.dataset.goto)route(target.dataset.goto);if(target?.dataset.emptyAction)handleAction(target.dataset.emptyAction);if(target?.dataset.experiment)openExperiment(target.dataset.experiment).catch(error=>toast(error.message))});document.addEventListener('keydown',event=>{const target=event.target.closest?.('[data-experiment]');if(target&&(event.key==='Enter'||event.key===' ')){event.preventDefault();openExperiment(target.dataset.experiment).catch(error=>toast(error.message))}if(event.key==='Escape'){closeModal();closeDrawer()}});$('#headerAction').onclick=()=>handleAction({agents:'agent',datasets:'dataset',judges:'judge',evaluators:'evaluator',experiments:'experiment'}[state.page]);$('#modalClose').onclick=closeModal;$('#modalBackdrop').onclick=event=>{if(event.target===$('#modalBackdrop'))closeModal()};$('#modalForm').onsubmit=async event=>{try{if(event.currentTarget.dataset.kind==='case'){event.preventDefault();await submitCase(event)}else await submitModal(event)}catch(error){toast(error.message)}};$('#drawerClose').onclick=closeDrawer;$('#drawerBack').onclick=()=>{const target=drawerBack;setDrawerBack(null);if(target)target()};$('#drawerBackdrop').onclick=closeDrawer;$('#runSearch').onkeydown=event=>{if(event.key==='Enter')loadRuns()};$('#runStatus').onchange=loadRuns;$('#evaluatorDimension').onchange=loadEvaluators;$('#evaluatorType').onchange=loadEvaluators;$('#checkOtlp').onclick=checkOtlp}
async function handleAction(kind){if(kind==='evaluator')await loadJudgeModels();if(kind==='experiment'){await Promise.all([loadAgents(),loadDatasets(),loadJudgeModels(),loadEvaluators()]);if(!state.agents.length||!state.datasets.length){toast('请先接入 Agent 并创建数据集');return}}openModal(kind)}
bind();route((location.hash||'#overview').slice(1));
