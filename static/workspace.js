'use strict';
let graphMode=true, cleanupPreview=null;
const baseOverview=overview,baseGPU=gpuCard,baseProject=projectPage,baseReplicas=replicasPage,baseServers=serversPage,baseRender=render,baseAction=handleAction,baseDetail=taskDetail,baseSubmit=submitForm;
const num=v=>Number.isFinite(v)?v.toFixed(1):'—';
const notesFor=id=>(state.notes||[]).filter(n=>n.task_id===id);
const meter=(value,detail)=>`<div class="resource-value"><strong>${value}</strong><small>${detail}</small></div>`;
function resourceCard(server){
  const s=server.snapshot||{},m=s.memory||{},w=s.work_disk||{},d=s.data_disk||{};
  return `<article class="card resource-card"><div class="card-head"><div><div class="eyebrow">SERVER / ${esc(s.hostname||server.host)}</div><h3>${esc(server.name)}</h3></div>${badge(server.connected?'success':'lost',server.connected?'在线':'离线')}</div><div class="resource-metrics"><div><span>CPU</span>${meter(num(s.cpu_percent)+'%',`${s.cpu_count||'—'} 核 · 负载 ${(s.load_average||[]).join(' / ')||'—'}`)}<div class="bar"><i style="width:${Math.min(100,s.cpu_percent||0)}%"></i></div></div><div><span>内存</span>${meter(num(m.used_percent)+'%',m.total_bytes?`${size(m.used_bytes)} / ${size(m.total_bytes)}`:'等待采样')}<div class="bar"><i style="width:${Math.min(100,m.used_percent||0)}%"></i></div></div></div><div class="disk-row"><span>工作磁盘</span><b>${w.free_bytes!=null?size(w.free_bytes):num(s.work_free_gb)+' GB'} 可用</b><small>${w.total_bytes?'共 '+size(w.total_bytes)+' · '+num(w.used_percent)+'% 已用':esc(server.work_root)}</small></div><div class="disk-row"><span>数据磁盘</span><b>${d.free_bytes!=null?size(d.free_bytes):num(s.data_free_gb)+' GB'} 可用</b><small>${d.total_bytes?'共 '+size(d.total_bytes)+' · '+num(d.used_percent)+'% 已用':esc(server.data_root)}</small></div><div class="resource-foot"><span>${(s.gpus||[]).length} 张 GPU · 采样 ${fmt(s.sampled_at)}</span><a href="#servers">管理 →</a></div>${!server.connected&&server.error?`<p class="error-text">${esc(server.error)}</p>`:''}</article>`;
}
overview=function(){const html=baseOverview();const cards=`<div class="section-title"><h2>服务器资源</h2><span>CPU · 内存 · 工作磁盘 · 数据磁盘</span></div><div class="grid resources">${state.servers.map(resourceCard).join('')}</div>`;return html.replace('<div class="section-title"><h2>GPU 资源池',cards+'<div class="section-title"><h2>GPU 资源池');};
const availability=(server,gpu)=>benchmarkAvailability(server,gpu,state.jobs,state.tasks,state.servers);
function benchmarkButton(server,gpu,cls='primary small'){const value=availability(server,gpu);return button('benchmark',value.label,`data-server="${server.id}" data-index="${gpu.index}" title="${esc(value.reason)}" ${value.disabled?'disabled':''}`,cls);}
gpuCard=function(server,gpu){return baseGPU(server,gpu).replace('badge green">使用中','badge blue">使用中').replace('class="card-foot">',`class="card-foot">${benchmarkButton(server,gpu,'small benchmark-button')}`);};
function benchmarksPage(){
  const cards=state.servers.flatMap(s=>(s.snapshot?.gpus||[]).map(g=>{const r=s.benchmarks?.[g.uuid||String(g.index)],value=availability(s,g);return `<article class="card benchmark-card"><div class="card-head"><span class="eyebrow">${esc(s.name)} / GPU ${g.index}</span>${badge(value.status,value.disabled?value.label:'空闲')}</div><h3>${esc(g.name)}</h3><p class="server-subtitle">${esc(g.uuid)}</p><div class="benchmark-results"><div><strong>${num(r?.tensor_tflops)}</strong><span>FP16 TFLOPS</span></div><div><strong>${num(r?.fp32_tflops)}</strong><span>FP32 TFLOPS</span></div></div><p class="muted">${r?'上次测试 '+fmt(r.measured_at):'尚未测试'}${r?.matrix_size?' · 矩阵 '+r.matrix_size+'²':''}</p><p class="benchmark-availability ${value.disabled?'unavailable':''}">${esc(value.reason)}</p><div class="card-foot">${button('gpu-detail','查看占用进程',`data-server="${s.id}" data-index="${g.index}"`,'small')}${benchmarkButton(s,g)}</div><small class="muted benchmark-environment"><span>测试环境</span><code title="${esc(s.benchmark_env||'Python')}">${esc(s.benchmark_env||'服务器默认 Python')}</code></small></article>`;}));
  return pageHead('GPU 性能测试','用相同的矩阵乘法测试显卡，实测结果参与自动调度。')+`<div class="benchmark-intro"><div><span class="eyebrow">MEASURE YOUR COMPUTE</span><h2>让调度依据真实算力</h2><p>测试 FP16 / FP32 矩阵吞吐，每张卡约 5 秒计算。选择空闲 GPU，结果自动保存。</p></div><div class="benchmark-icon">TF<span>LOPS</span></div></div><div class="notice">需要服务器 Python 环境安装 PyTorch 和 CUDA。可在服务器编辑页配置“基准测试 Conda 环境”。测试前会再次核验空闲状态，运行中的实验会阻止测试。矩阵吞吐用于相对比较，实际训练速度仍取决于模型与数据。</div><div class="grid">${cards.join('')||empty('尚无可测试 GPU','连接服务器后，显卡会出现在这里。')}</div>`;
}
projectTasksMarkup=function(tasks){return `<div class="section-title"><div><h2>实验调度图</h2><span>连线表示前序依赖；前序成功并回传后解锁后续节点。</span></div><div class="view-switch">${button('graph-view','调度图',`data-mode="graph"`,graphMode?'active':'')}${button('graph-view','列表',`data-mode="list"`,graphMode?'':'active')}</div></div>`+(graphMode?scheduleGraph(tasks):tasksTable(tasks));};
function sharingPanel(scope){const value=(state.storage||[]).find(s=>s.scope===scope),servers=state.servers;return `<article class="card sharing-card"><div class="card-head"><h3>${scope==='work'?'工作目录':'数据目录'}共享</h3>${button('storage-scan','重新探测',`data-scope="${scope}"`,'small')}</div><p class="muted">双向随机文件核验 · ${fmt(value?.checked_at)}</p>${value?(value.groups||[]).map((g,i)=>`<div class="sharing-group"><span class="badge ${g.length>1?'blue':''}">${g.length>1?'共享组 '+(i+1):'独立目录'}</span><span>${g.map(id=>esc(serverBy(id)?.name||id)).join(' + ')}</span></div>`).join(''):'<p class="muted">等待探测，路径相同不会被直接视为共享。</p>'}${servers.filter(s=>value?.states?.[s.id]?.status==='unverified').map(s=>`<p class="error-text">${esc(s.name)} · 未核验：${esc(value.states[s.id].error)}</p>`).join('')}</article>`;}
replicasPage=function(p){return `<div class="grid storage-grid">${sharingPanel('work')}${sharingPanel('data')}</div>`+baseReplicas(p);};
serversPage=function(){return baseServers()+`<div class="section-title"><h2>存储共享关系</h2><span>配置变化时探测 · 可手动重新探测</span></div><div class="grid">${sharingPanel('work')}${sharingPanel('data')}</div>`;};
render=function(){if(route()[0]!=='benchmarks'){baseRender();return;}$('#breadcrumb').textContent='GPU 性能测试';document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav==='benchmarks'));$('#connection-state').textContent='已更新 '+new Date().toLocaleTimeString('zh-CN');$('#scheduler-button').textContent=state.settings.paused?'恢复调度':'暂停调度';$('#main').innerHTML=(state.settings.demo?'<div class="notice warning">演示数据 · 性能结果为模拟值。</div>':'')+benchmarksPage();};
async function inspectionPanel(id){const t=await api(`/api/tasks/${id}/inspection`);if(!$('#dialog').open||dialogKind!=='task-detail'||selectedTask!==id)return;let el=$('#node-inspection');if(!el){el=document.createElement('section');el.id='node-inspection';$('#task-log').before(el);}const signature=JSON.stringify([id,t.notes.map(n=>[n.id,n.updated_at,n.body_chars]),t.outputs,t.synced_at]);if(el.dataset.signature===signature)return;el.dataset.signature=signature;el.innerHTML=`<div class="section-title"><h3>实验输出</h3><span>${t.outputs.length} 个新增 / 修改文件</span></div><p class="muted">${t.synced_at?'已回传并通过 SHA-256 校验 · '+fmt(t.synced_at):'实验结束并完成回传后显示输出；原始代码和未修改的前序文件不计入输出。'}</p><div class="output-list">${t.outputs.map(v=>`<div class="file-row"><span>${esc(v.path)} <small>${v.change==='created'?'新增':'修改'} · ${size(v.size)}</small></span>${t.synced_at?`<a href="/api/tasks/${id}/artifacts?attempt_id=${t.attempt_id}&path=${encodeURIComponent(v.path)}">下载</a>`:badge('syncing','未回传')}</div>`).join('')}</div><div class="section-title"><h3>节点 Notes</h3><span>执行过程中也可记录</span></div><div class="notes-list">${t.notes.map(n=>`<article class="note">${noteDocumentMarkup(n)}<div class="note-meta"><time>${fmt(n.updated_at)}</time>${button('note-download','MD',`data-note="${n.id}" title="Markdown"`,'small')}${button('edit-note','编辑',`data-id="${id}" data-note="${n.id}"`,'small')}${button('delete-note','删除',`data-id="${id}" data-note="${n.id}"`,'small danger')}</div></article>`).join('')||'<p class="muted">记录参数、观察、待办或实验结论。</p>'}</div>${noteEditorMarkup(id)}<div class="section-title"><h3>运行日志</h3></div>`;noteEditorMount($('#note-form'));}
taskDetail=async function(id){const detail=baseDetail(id);await inspectionPanel(id);await detail;};
async function waitJob(job){const deadline=Date.now()+240000;while(Date.now()<deadline){const snapshot=await api('/api/state'),value=snapshot.jobs.find(j=>j.id===job.id);if(value?.status==='failed')throw Error(value.error);if(value?.status==='success')return value.result;await new Promise(resolve=>setTimeout(resolve,800));}throw Error('核验仍在后台执行，请在操作记录查看结果并重试预览');}
handleAction=async function(el){const a=el.dataset.action,id=el.dataset.id;
  if(a.startsWith('note-')&&await noteEditorAction(el))return;
  if(a==='graph-view'){graphMode=el.dataset.mode==='graph';render();return;}
  if(a==='storage-scan'){if(state.settings.demo){toast('演示模式展示模拟共享组；真实服务器使用双向文件探测。');return;}await api('/api/storage/scan',{scope:el.dataset.scope});toast('共享目录核验已加入后台队列');await load();return;}
  if(a==='clean-all'||a==='clean-one'){const project=el.dataset.project,ids=a==='clean-one'?[el.dataset.server]:null;cleanupPreview=null;modal('核验清理范围','<p class="muted">正在检测所有服务器的工作目录共享关系…</p>','confirm');const revision=dialogRevision;try{const plan=await waitJob(await api(`/api/projects/${project}/clean-preview`,{server_ids:ids}));if(revision!==dialogRevision||!$('#dialog').open)return;cleanupPreview=plan;modal('确认清理范围',`<p>以下服务器上的项目副本将被清理：</p><ul>${plan.affected_server_ids.map(s=>`<li>${esc(serverBy(s)?.name)}</li>`).join('')}</ul>${plan.groups.some(g=>g.length>1)?'<div class="notice warning">存在共享工作目录。共享组只归档、删除一次，组内所有服务器的副本记录同步更新。</div>':''}<p class="muted">删除前会完整归档到本机。运行、回传或状态未知的实验会阻止清理；共享关系变化时需要重新预览。</p>${button('confirm-clean','确认清理',`data-project="${project}"`,'danger')}`,'confirm');}catch(error){if(revision!==dialogRevision||!$('#dialog').open)return;modal('无法安全清理',`<p class="error-text">${esc(error.message)}</p>`,'confirm');}return;}
  if(a==='confirm-clean'){if(!cleanupPreview)throw Error('请先核验清理范围');const plan=cleanupPreview;await api(`/api/projects/${plan.project_id}/clean`,{server_ids:plan.requested_server_ids,confirmed_server_ids:plan.affected_server_ids});closeDialogFor(el);if(cleanupPreview===plan)cleanupPreview=null;toast('清理操作已提交');return;}
  if(a==='benchmark'&&state.settings.demo){toast('演示模式展示模拟结果；连接真实服务器后可执行测试。');return;}
  if(a==='delete-note'){await api(`/api/tasks/${id}/notes`,{id:el.dataset.note,delete:true});await load(false);await inspectionPanel(id);return;}
  if(a==='edit-note'){const n=(state.notes||[]).find(n=>n.id===el.dataset.note),form=$('#note-form');if(n&&form)await noteLoadForEdit(n,form);return;}
  const result=await baseAction(el);
  if(a==='gpu-detail'){
    const server=serverBy(el.dataset.server),gpu=server?.snapshot?.gpus?.find(g=>g.index===Number(el.dataset.index));
    const btn=$('#dialog [data-action=benchmark]');
    if(btn&&gpu){const value=availability(server,gpu);btn.disabled=value.disabled;btn.textContent=value.label;btn.title=value.reason;const message=document.createElement('p');message.className='benchmark-availability';message.textContent=value.reason;btn.before(message);}
  }
  return result;
};
submitForm=async function(form){if(form.id!=='note-form')return baseSubmit(form);await noteSaveForm(form);toast('Note 已保存');await load(false);await inspectionPanel(form.dataset.task);};
if(document.body.dataset.authorized==='true')setInterval(()=>{const form=$('#note-form');if($('#dialog').open&&dialogKind==='task-detail'&&selectedTask&&form&&!form.noteUploadFile&&!form.dataset.note&&!$('[name=body]',form).value&&!form.contains(document.activeElement))inspectionPanel(selectedTask).catch(()=>{});},5000);


const scheduleGraphBaseAction=handleAction,scheduleGraphBaseRender=render;
const graphScrollPositions=new Map();
let graphScale=1,graphMountedProject=null,graphAutoFit=true;
let graphLayoutMode='dependencies',graphShowAllEdges=false,graphOrientation='vertical';
let graphComponentId='',graphComponentProject='';
function graphComponents(tasks){
  const links=new Map(tasks.map(t=>[t.id,[]])),map=new Map(tasks.map(t=>[t.id,t]));
  for(const t of tasks)for(const id of t.depends_on||[])if(links.has(id)){links.get(id).push(t.id);links.get(t.id).push(id);}
  const seen=new Set(),groups=[];
  for(const task of tasks){if(seen.has(task.id))continue;const ids=[],pending=[task.id];while(pending.length){const id=pending.pop();if(seen.has(id))continue;seen.add(id);ids.push(id);pending.push(...links.get(id));}const root=ids.map(id=>map.get(id)).sort((a,b)=>(a.seq||0)-(b.seq||0))[0];groups.push({id:root.id,name:root.name,ids:new Set(ids)});}
  return groups;
}
function graphState(task){
  if(task.status==='success'&&task.synced_at)return 'complete';
  if(task.status==='canceled')return 'canceled';
  if(['failed','lost','sync_failed'].includes(task.status))return 'failed';
  if(['running','staging','launching','syncing','canceling','suspended'].includes(task.status))return 'running';
  if(['queued','waiting','blocked','paused','success'].includes(task.status))return 'waiting';
  return 'draft';
}
function graphElapsed(task){
  if(!task.started_at)return '';
  const seconds=Math.max(0,Math.floor((task.finished_at||Date.now()/1000)-task.started_at));
  return seconds<60?seconds+' 秒':seconds<3600?Math.floor(seconds/60)+' 分钟':Math.floor(seconds/3600)+' 小时 '+Math.floor(seconds%3600/60)+' 分钟';
}
// Rounded orthogonal connectors keep the flow readable in either direction.
function graphConnector(points){
  let d=`M ${points[0][0]} ${points[0][1]}`;
  for(let i=1;i<points.length-1;i++){
    const previous=points[i-1],corner=points[i],next=points[i+1],before=Math.hypot(corner[0]-previous[0],corner[1]-previous[1]),after=Math.hypot(next[0]-corner[0],next[1]-corner[1]);
    if(!before||!after)continue;
    const r=Math.min(5,before/2,after/2),enter=corner.map((v,j)=>v+(previous[j]-v)*r/before),leave=corner.map((v,j)=>v+(next[j]-v)*r/after);
    d+=` L ${enter[0]} ${enter[1]} Q ${corner[0]} ${corner[1]} ${leave[0]} ${leave[1]}`;
  }
  return d+` L ${points.at(-1)[0]} ${points.at(-1)[1]}`;
}
function scheduleGraph(tasks){
  if(!tasks.length)return empty('创建第一个实验节点','设置前序依赖，即可形成实验流程图。');
  if(graphComponentProject!==route()[1]){graphComponentProject=route()[1];graphComponentId='';}
  const totalTasks=tasks.length,components=graphComponents(tasks),component=components.find(c=>c.id===graphComponentId);
  if(component)tasks=tasks.filter(t=>component.ids.has(t.id));else graphComponentId='';
  const componentControl=components.length>1?`<label class="graph-component-picker"><span>流程</span><select id="graph-component" aria-label="选择要查看的独立流程"><option value="">全部 · ${totalTasks} 个实验</option>${components.map(c=>`<option value="${esc(c.id)}" ${c.id===graphComponentId?'selected':''}>${esc(c.name)} · ${c.ids.size} 个</option>`).join('')}</select></label>`:'';
  const main=$('#main'),style=getComputedStyle(main),available=Math.max(280,main.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)),horizontal=graphOrientation==='horizontal';
  let graph;
  try{
    const base=layoutTasks(tasks,3,{mode:graphLayoutMode});
    const width=horizontal?Math.max(180,Math.min(224,Math.floor((available-84-(base.rows.length-1)*48)/base.rows.length))):Math.max(192,Math.min(248,Math.floor((available-56-(base.maxParallel-1)*32)/base.maxParallel)));
    const projectedWidth=56+(horizontal?base.rows.length:base.maxParallel)*(width+(horizontal?64:32))-(horizontal?64:32);
    const overview=graphAutoFit&&available/projectedWidth<.85;
    graph=layoutTasks(tasks,3,{nodeWidth:width,nodeHeight:overview?148:124,gapX:horizontal?64:32,gapY:horizontal?32:54,axis:24,top:28,mode:graphLayoutMode,orientation:graphOrientation});
  }catch(error){return `<div class="notice warning">${esc(error.message)}</div>`;}
  const map=new Map(graph.nodes.map(n=>[n.id,n])),counts={complete:0,running:0,waiting:0,draft:0,failed:0,canceled:0};
  tasks.forEach(t=>counts[graphState(t)]++);
  const edges=routeTaskEdges(graph,assignTaskPorts(graph)),secondaryCount=edges.filter(e=>e.secondary).length;
  if(graphAutoFit)graphScale=Math.max(.05,Math.min(1,Math.floor((available-26)/graph.width*100)/100));
  const paths=edges.map(edge=>{
    const p=map.get(edge.source),n=map.get(edge.target),points=edge.points;
    const kind=graphState(p)==='complete'?'done':graphState(p)==='failed'?'error':'wait';
    return `<path class="graph-edge ${kind} ${edge.secondary?'secondary':''}" data-source="${esc(p.id)}" data-target="${esc(n.id)}" d="${graphConnector(points)}" marker-end="url(#schedule-arrow-${kind})"><title>${esc(p.name)} → ${esc(n.name)}${edge.secondary?'（另有间接依赖路径）':''}</title></path>`;
  }).join('');
  const markers=['done','wait','error'].map((kind,i)=>`<marker id="schedule-arrow-${kind}" markerWidth="5" markerHeight="5" refX="4" refY="2.5" orient="auto"><path d="M 0 0 L 5 2.5 L 0 5" fill="${['#369c81','#93a2b6','#cf7180'][i]}"/></marker>`).join('');
  const statusNames={complete:'完成',running:'执行中',waiting:'等待',draft:'草稿',failed:'异常',canceled:'已中断'};
  const summary=Object.entries(counts).filter(([,count])=>count).map(([status,count])=>`<span class="schedule-count ${status}"><i></i>${statusNames[status]} <b>${count}</b></span>`).join('');
  const ports=(id,side)=>edges.filter(e=>e[side==='in'?'target':'source']===id).map(e=>`<span class="flow-port ${side} ${e.secondary?'secondary':''}" data-source="${esc(e.source)}" data-target="${esc(e.target)}" style="${horizontal?'top':'left'}:${e[side==='in'?'targetOffset':'sourceOffset']-2}px"></span>`).join('');
  const cards=graph.nodes.map(t=>{
    const notes=notesFor(t.id),status=graphState(t),files=t.outputs||[],server=serverBy(t.server_id)?.name||'自动选择服务器',elapsed=graphElapsed(t),outputCount=t.output_count||files.length,project=projectBy(t.project_id||route()[1]);
    const prefix=project?.name+'_',displayName=(prefix&&t.name.startsWith(prefix)?t.name.slice(prefix.length):t.name).replace(/_/g,' ');
    const metadata=[t.name,labels[t.status]||t.status,server+(t.gpu_index!=null?' · GPU '+t.gpu_index:''),elapsed,t.reason,t.exit_code!=null?'退出码 '+t.exit_code:'',graphLayoutMode==='time'?fmt(t.started_at||t.created_at):''].filter(Boolean).join('\n');
    return `<article class="graph-node schedule-node flow-node ${status}" data-node-id="${esc(t.id)}" title="${esc(metadata)}" style="left:${t.x}px;top:${t.y}px;width:${graph.nodeWidth}px;height:${graph.nodeHeight}px">${ports(t.id,'in')}<div class="flow-node-top"><span class="flow-sequence">#${esc(t.seq)}</span><span class="flow-status-text"><i class="flow-status-dot"></i>${esc(labels[t.status]||t.status)}</span></div><div class="flow-node-name"><button class="node-title" data-action="task-detail" data-id="${esc(t.id)}" title="${esc(t.name)}">${esc(displayName)}</button></div><div class="flow-node-duration">${taskDurationMarkup(t)}</div><div class="node-footer"><span class="flow-location" title="${esc(server)}">${esc(t.server_id?server:'自动调度')}</span><div class="node-actions">${button('task-detail','输出 '+outputCount,`data-id="${esc(t.id)}" title="${esc(files.map(v=>v.path).join('\n')||'运行后显示输出文件')}"`,'small output-button')}${button('graph-notes',notes.length?'Notes '+notes.length:'Notes',`data-id="${esc(t.id)}"`,'small note-button')}</div></div>${ports(t.id,'out')}</article>`;
  }).join('');
  return `<section class="schedule-board flow-board compact-flow ${horizontal?'horizontal':'vertical'} ${graphShowAllEdges?'show-all-edges':''}"><header class="flow-board-header"><div><strong>实验流程 <small>${tasks.length}${component?'/'+totalTasks:''} 个节点 · ${edges.length} 条依赖</small></strong><span>${graph.maxParallel>1?'最多 '+graph.maxParallel+' 个并列节点':'当前依赖为串行'}</span></div><div class="flow-controls">${componentControl}<div class="flow-layout-switch" aria-label="流程方向">${button('graph-orientation','纵向','data-orientation="vertical"',!horizontal?'active':'')}${button('graph-orientation','横向','data-orientation="horizontal"',horizontal?'active':'')}</div><div class="flow-layout-switch">${button('graph-layout-mode','依赖层级','data-mode="dependencies"',graphLayoutMode==='dependencies'?'active':'')}${button('graph-layout-mode','执行时间','data-mode="time"',graphLayoutMode==='time'?'active':'')}</div>${secondaryCount?button('graph-edge-mode',graphShowAllEdges?'全部依赖':'精简依赖','title="显示或隐藏间接路径中的重复依赖，不改变实验配置"','small flow-edge-toggle'):''}${button('graph-fit','适应宽度','','small graph-fit-button '+(graphAutoFit?'active':''))}<div class="schedule-zoom">${button('graph-zoom-out','−',`${graphScale<=.2?'disabled':''} aria-label="缩小调度图"`,'small')}${button('graph-zoom-reset',Math.round(graphScale*100)+'%','title="恢复原始大小"','small')}${button('graph-zoom-in','＋',`${graphScale>=1.5?'disabled':''} aria-label="放大调度图"`,'small')}</div></div></header><div class="flow-overview"><div>${summary}</div><span>已完成并回传 <b>${counts.complete} / ${tasks.length}</b></span></div><div class="graph-scroll" tabindex="0" aria-label="实验流程图，可滚动查看"><div class="schedule-scaled-space" style="width:${graph.width*graphScale}px;height:${graph.height*graphScale}px"><div class="schedule-canvas" style="width:${graph.width}px;height:${graph.height}px;transform:scale(${graphScale});transform-origin:top left;--graph-text-boost:${graphAutoFit?Math.min(1.4,1/graphScale):1}"><svg class="graph-lines" width="${graph.width}" height="${graph.height}" aria-label="实验依赖关系"><defs>${markers}</defs>${paths}</svg>${cards}</div></div></div><footer class="schedule-foot"><span>${graphLayoutMode==='dependencies'?(horizontal?'同列':'同层')+'节点可独立调度':horizontal?'时间从左向右':'时间从上到下'}${secondaryCount&&!graphShowAllEdges?' · 隐藏 '+secondaryCount+' 条间接路径（配置保留）':''}</span><span>点击节点查看详情 · 输出 · Notes</span></footer></section>`;
}


render=function(){
  const old=$('.graph-scroll'),key=route()[1];if(old&&graphMountedProject)graphScrollPositions.set(graphMountedProject,{left:old.scrollLeft,top:old.scrollTop});
  scheduleGraphBaseRender();const next=$('.graph-scroll'),position=graphScrollPositions.get(key);if(next&&position){next.scrollLeft=position.left;next.scrollTop=position.top;}graphMountedProject=next?key:null;
};
handleAction=async function(el){
  if(el.dataset.action==='graph-orientation'){graphOrientation=el.dataset.orientation;graphScrollPositions.delete(route()[1]);graphMountedProject=null;render();return;}
  if(el.dataset.action==='graph-layout-mode'){graphLayoutMode=el.dataset.mode;graphScrollPositions.delete(route()[1]);graphMountedProject=null;render();return;}
  if(el.dataset.action==='graph-edge-mode'){graphShowAllEdges=!graphShowAllEdges;render();return;}
  if(el.dataset.action==='graph-fit'){graphAutoFit=true;graphScrollPositions.delete(route()[1]);graphMountedProject=null;render();return;}
  if(el.dataset.action.startsWith('graph-zoom-')){graphAutoFit=false;graphScale=el.dataset.action==='graph-zoom-reset'?1:Math.max(.2,Math.min(1.5,Math.round((graphScale+(el.dataset.action==='graph-zoom-in'?.1:-.1))*100)/100));render();return;}
  if(el.dataset.action==='graph-notes'){await taskDetail(el.dataset.id);noteEditorSetView($('#note-form'),'split');$('#note-form textarea')?.focus();return;}
  return scheduleGraphBaseAction(el);
};
function graphHighlight(node){
  const canvas=node?.closest('.schedule-canvas');if(!canvas)return;
  const id=node.dataset.nodeId,related=new Set([id]);
  canvas.querySelectorAll('.graph-edge,.flow-port').forEach(edge=>{const active=edge.dataset.source===id||edge.dataset.target===id;edge.classList.toggle('highlighted',active);if(active){related.add(edge.dataset.source);related.add(edge.dataset.target);}});
  canvas.classList.add('highlighting');canvas.querySelectorAll('.graph-node').forEach(card=>card.classList.toggle('related',related.has(card.dataset.nodeId)));
}
document.addEventListener('pointerover',e=>{const node=e.target.closest('.schedule-node');if(node)graphHighlight(node);});
document.addEventListener('pointerout',e=>{const node=e.target.closest('.schedule-node');if(!node||node.contains(e.relatedTarget))return;const canvas=node.closest('.schedule-canvas');canvas?.classList.remove('highlighting');});
document.addEventListener('focusin',e=>{const node=e.target.closest('.schedule-node');if(node)graphHighlight(node);});
document.addEventListener('focusout',e=>{const node=e.target.closest('.schedule-node');if(node&&!node.contains(e.relatedTarget))node.closest('.schedule-canvas')?.classList.remove('highlighting');});
let scheduleResizeTimer;
window.addEventListener('resize',()=>{clearTimeout(scheduleResizeTimer);scheduleResizeTimer=setTimeout(()=>{if($('.schedule-board'))render();},150);});

document.addEventListener('change',event=>{if(event.target.id==='graph-component'){graphComponentId=event.target.value;graphAutoFit=true;graphScrollPositions.delete(route()[1]);graphMountedProject=null;render();}});
