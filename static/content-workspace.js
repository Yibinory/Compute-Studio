'use strict';
const contentLabels={queue:'批量入队',cancel:'批量中断',pause:'批量暂停',resume:'批量恢复',retry:'批量重新执行',restart:'中断后安全重启'};
const contentCanQueue=t=>!t.archived&&['draft','paused'].includes(t.status)&&!t.attempt_id;
let contentBatch=null,contentPreviewSequence=0,contentStorageReports=new Map();
const contentLines=value=>String(value||'').split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
function contentMetric(title,value,detail){return `<article class="content-metric"><span>${title}</span><strong>${value}</strong><small>${detail}</small></article>`;}
const contentBaseProjectPage=projectPage;
projectPage=function(id){
 let html=contentBaseProjectPage(id);if(!state.runtime_features?.content_store)return html;
 const tab=button('project-tab','存储与复用','data-tab="content"',projectTab==='content'?'active':'');
 html=html.replace(/(<div class="tabs">[\s\S]*?)(<\/div>)/,`$1${tab}$2`);
 if(projectTab==='content'){
  const marker=html.indexOf('<div class="tabs">'),end=html.indexOf('</div>',marker)+6;
  html=html.slice(0,end)+contentStorageMarkup(id);
 }
 return html;
};
function contentStorageMarkup(id){
 const r=contentStorageReports.get(id)?.report,caches=contentStorageReports.get(id)?.caches||[];
 return `<section class="content-storage"><div class="section-title"><div><h2>项目内容库</h2><span>相同 hash 只保存一份，实验使用文件引用</span></div><div class="actions">${button('content-storage-refresh','统计与刷新',`data-id="${id}"`)}${button('content-compact','整理历史重复文件',`data-id="${id}"`,'primary')}</div></div>`+
 `<div class="content-metrics">${contentMetric('内容库实际大小',r?size(r.object_bytes):'待统计',r?`${r.objects} 个唯一内容`:'统计在后台进行')}${contentMetric('旧版运行目录',r?size(r.legacy_bytes):'待统计','input / result 的历史完整副本')}${contentMetric('文件引用总量',r?size(r.referenced_bytes):'待统计','引用不重复存储文件内容')}${contentMetric('通过复用减少',r?size(r.saved_bytes):'待统计','根据已整理的引用估算')}</div>`+
 `<article class="card content-explanation"><h3>现在的目录里是什么</h3><div class="content-directory"><code>code/</code><p>可编辑的公共代码；实验可以指定包含和排除规则。</p><code>objects/</code><p>按 SHA-256 保存的唯一代码与结果内容，跨运行复用。</p><code>runs/&lt;运行 ID&gt;/</code><p>输入与输出清单、运行日志和控制记录。新实验不在本机复制整个前序目录。</p></div><p class="muted">服务器运行工作区保留独立代码；前序文件使用只读内容引用。回传只收集本节点新增／修改且符合选择规则的输出。旧实验的历史文件需要先整理才能减少已有占用。</p><small class="muted">${r?'最近统计：'+fmt(r.checked_at):'点击统计与刷新读取文件大小；不会重新计算全部 hash。'}</small></article>`+
 `<div class="section-title"><h2>每次运行的文件</h2><span>区分输入引用、自己的输出与尚未整理的旧副本</span></div><div class="content-run-list">${(r?.run_rows||[]).map(run=>`<article><div><b>${esc(run.name)}</b><code>${esc(run.attempt_id.slice(0,12))}</code></div><span>输入引用 ${size(run.input_bytes)}<small>自身输出 ${size(run.output_bytes)} · 旧副本 ${size(run.legacy_bytes)}</small></span>${button('content-run-files','查看清单',`data-project="${id}" data-attempt="${run.attempt_id}"`,'small')}</article>`).join('')||'<p class="muted">刷新统计后显示各运行目录。</p>'}</div>`+
 `<div class="section-title"><h2>服务器内容索引</h2><span>使用前检查所需文件是否存在，完整核验每 ${state.settings.cache_scan_hours||24} 小时一次</span></div><div class="grid">${state.servers.map(s=>{const c=caches.find(v=>v.server_id===s.id);return `<article class="card content-cache"><div class="card-head"><h3>${esc(s.name)}</h3>${badge(s.connected?'success':'lost',s.connected?'在线':'离线')}</div><code class="break">${esc(s.work_root)}/.compute-projects/${esc(id)}/objects/</code><p>${c?`${c.objects} 个内容 · ${size(c.bytes)} · 完整核验 ${fmt(c.checked_at)}`:'尚无完整核验记录；调度时会逐个检查所需 hash。'}</p>${c?.invalid?.length?`<p class="error-text">${c.invalid.length} 个缓存无效，后续使用时会补传或提示。</p>`:''}<div class="actions">${button('content-scan','核验缓存',`data-project="${id}" data-server="${s.id}"`,'small')}</div></article>`;}).join('')}</div></section>`;
}
async function contentStorageLoad(id){
 const target=$('.content-storage');if(!target)return;
 const value=await api(`/api/projects/${id}/storage`);if(route()[1]!==id||projectTab!=='content'||!target.isConnected)return;
 contentStorageReports.set(id,value);target.outerHTML=contentStorageMarkup(id);
}
const contentBaseRender=render;
render=function(){contentBaseRender();if(!state.runtime_features?.content_store)return;
 const queued=(state.tasks||[]).filter(t=>['queued','waiting','blocked'].includes(t.status)).length;
 if(state.settings.paused&&queued)$('#main')?.insertAdjacentHTML('afterbegin',`<aside class="content-dispatch-paused" role="status"><div><strong>全局调度已暂停</strong><p>${queued} 个实验已入队，等待恢复调度；入队不会自动解除全局暂停。</p></div>${button('content-resume-dispatch','恢复调度','','small')}</aside>`);
 const [page,id]=route();if(page==='tasks'||page==='project'&&projectTab==='tasks')$('#main .page-title .actions')?.insertAdjacentHTML('beforeend',(state.runtime_features.batch_queue?button('content-batch-queue','批量入队'):'')+button('content-batch-open','批量状态操作'));
 if(page==='project'&&projectTab==='content')contentStorageLoad(id).catch(e=>toast(e.message,true));
};
const contentBaseSettingsPage=settingsPage;
settingsPage=function(){let html=contentBaseSettingsPage();if(state.runtime_features?.content_store)html=html.replace('<div class="full notice">',field('内容缓存完整核验间隔（小时）','cache_scan_hours',state.settings.cache_scan_hours||24,'number')+'<div class="full notice">');return html;};
function contentBatchOpen(action='cancel',taskIds=null){
 const [page,id]=route(),tasks=(state.tasks||[]).filter(t=>!t.archived&&(page!=='project'||t.project_id===id));
 contentBatch={tasks,ids:new Set(taskIds||tasks.filter(t=>action==='queue'?contentCanQueue(t):['queued','waiting','blocked','staging','launching','running','suspended','canceling'].includes(t.status)).map(t=>t.id)),action};
 modal(action==='queue'?'批量入队':'批量实验状态',`<form id="content-batch-form"><div class="content-batch-heading"><label>操作<select name="batch_action">${Object.entries(contentLabels).filter(([value])=>value!=='queue'||state.runtime_features?.batch_queue).map(([value,label])=>`<option value="${value}" ${value===action?'selected':''}>${label}</option>`).join('')}</select></label><div class="actions">${button('content-select-pending','选择待入队','','small')}${button('content-select-active','选择未结束','','small')}${button('content-select-ended','选择失败／中断','','small')}${button('content-select-all','全选','','small')}</div></div><p class="muted">批量入队可一次提交草稿和未运行的暂停排队实验，前序与后续可一起选择，随后按依赖调度。已入队、运行中和已结束的实验会跳过。重新执行保留历史结果；中断会停止整个实验及其子进程。操作不改变全局调度开关。</p><div class="content-task-selection">${tasks.map(t=>`<label><input type="checkbox" data-content-task="${t.id}" ${contentBatch.ids.has(t.id)?'checked':''}><span><b>${esc(t.name)}</b><small>${esc(projectBy(t.project_id)?.name||'')} · #${t.seq}</small></span>${badge(t.status)}</label>`).join('')}</div><div id="content-batch-preview" class="content-review">请选择实验</div><div class="form-footer"><button type="submit" class="primary" disabled>提交批量操作</button></div></form>`,'content-batch');contentBatchPreview();
}
async function contentBatchPreview(){
 const seq=++contentPreviewSequence,form=$('#content-batch-form'),target=$('#content-batch-preview');if(!form||!contentBatch)return;
 contentBatch.action=$('[name=batch_action]',form).value;const ids=[...contentBatch.ids];$('button[type=submit]',form).disabled=true;
 if(!ids.length){target.textContent=contentBatch.action==='queue'&&!contentBatch.tasks.some(contentCanQueue)?'当前没有待入队的草稿。待调度、等待资源或等待前序的实验已经入队；失败／中断实验请使用重新执行。':'请选择实验';return;}
 target.textContent='正在核对状态和依赖保护…';
 try{
  const plan=await api('/api/tasks/batch-action/preview',{task_ids:ids,action:contentBatch.action});
  if(!target.isConnected||seq!==contentPreviewSequence)return;
  target.innerHTML=`<b>可操作 ${plan.eligible.length} 个 · 跳过 ${plan.excluded.length} 个</b><p>${esc(contentLabels[plan.action])} · 调度${plan.scheduler_paused?'已暂停，入队后等待你恢复调度':'开启，入队后将按依赖和 GPU 条件自动调度'}</p>${plan.excluded.map(t=>`<div class="content-excluded"><strong>${esc(t.name)}</strong><span>${esc(t.reason)}</span></div>`).join('')}`;
  const submit=$('button[type=submit]',form);submit.textContent=plan.action==='queue'?`确认入队 ${plan.eligible.length} 个实验`:'提交批量操作';submit.disabled=!plan.eligible.length;
 }catch(e){if(target.isConnected&&seq===contentPreviewSequence)target.textContent=e.message;}
}
async function contentJob(job,complete){
 modal(job.label,`<div class="content-job"><p>已加入后台，关闭窗口不会取消操作。可在操作记录查看或中断。</p><div id="content-job-progress" class="content-review">正在等待后台处理…</div></div>`,'content-job');
 const target=$('#content-job-progress');
 while(target.isConnected&&$('#dialog').open){
  const value=(await api('/api/jobs/'+job.id)).job;if(!target.isConnected)return;
  target.textContent=value.progress?.label||value.progress?.phase||labels[value.status]||value.status;
  if(value.status==='success'){await complete(value.result,target);return;}
  if(['failed','canceled'].includes(value.status)){target.textContent=value.error||'已中断';return;}
  await new Promise(resolve=>setTimeout(resolve,700));
 }
}
function contentPolicyOpen(id){
 const t=state.tasks.find(t=>t.id===id);if(!t)return;
 modal('实验文件选择',`<form id="content-policy-form" data-task="${id}"><p class="muted">每行一个相对路径或 glob。留空的代码／输出选择表示默认全部；默认前序只提供它自己新增或修改的输出。</p><div class="form-grid"><label>公共代码包含（可选）<textarea name="code_files" rows="3" placeholder="scripts/**&#10;*.py">${esc((t.code_files||[]).join('\n'))}</textarea></label><label>公共代码排除<textarea name="code_exclude" rows="3" placeholder="outputs/**&#10;checkpoints/**">${esc((t.code_exclude||[]).join('\n'))}</textarea></label><label class="full">回传输出选择（可选）<textarea name="output_files" rows="3" placeholder="outputs/metrics.json&#10;outputs/model.pt">${esc((t.output_files||[]).join('\n'))}</textarea></label></div><h3>直接前序的文件</h3>${t.depends_on.map(d=>`<section class="content-dependency"><label><input type="checkbox" data-content-dependency="${d}" ${Object.hasOwn(t.dependency_files||{},d)?'checked':''}>限制 ${esc(state.tasks.find(v=>v.id===d)?.name||d)} 的输出</label><textarea data-content-patterns="${d}" rows="3" placeholder="outputs/features.npz&#10;outputs/metrics.json">${esc((t.dependency_files?.[d]||[]).join('\n'))}</textarea><small>不勾选使用该前序全部自身输出；勾选并留空表示只有控制依赖，不传文件。</small></section>`).join('')||'<p class="muted">没有前序依赖。</p>'}<div class="form-footer"><button type="submit" class="primary">保存文件选择</button></div></form>`,'content-policy');
}
let contentRunView=null;
async function contentRunFilesLoad(){
 const view=contentRunView,target=$('#content-run-file-list');if(!view||!target)return;
 const result=await api(`/api/projects/${view.project}/runs/${view.attempt}/files?scope=${view.scope}&offset=${view.offset}&limit=100`);
 if(!target.isConnected||view!==contentRunView)return;
 target.innerHTML=`<p class="muted">共 ${result.total} 个文件 · ${view.scope==='inputs'?'输入快照引用':view.scope==='workspace'?'完整快照引用':'本节点自身输出'}，引用不会重复保存内容。</p><div class="content-plan-files">${result.files.map(v=>`<div><span>${esc(v.path)}</span><code title="${esc(v.sha256||'旧目录尚未建立 hash')}">${v.sha256?v.sha256.slice(0,12):'旧副本'} · ${size(v.size)}</code><a href="/api/projects/${view.project}/runs/${view.attempt}/files?scope=${view.scope}&path=${encodeURIComponent(v.path)}">下载</a></div>`).join('')||'<p class="muted">这个范围没有文件。</p>'}</div><div class="actions">${button('content-run-prev','上一页','',view.offset?'small':'small disabled')}${button('content-run-next','下一页','',view.offset+100<result.total?'small':'small disabled')}</div>`;
 $('[data-action=content-run-prev]',target).disabled=view.offset===0;$('[data-action=content-run-next]',target).disabled=view.offset+100>=result.total;
}
const contentBaseDetail=taskDetail;
taskDetail=async function(id){await contentBaseDetail(id);if(!state.runtime_features?.content_store||!$('#dialog').open||selectedTask!==id||dialogKind!=='task-detail')return;
 const t=state.tasks.find(v=>v.id===id),transfer=t.content_transfer,returned=t.content_return;
 $('#node-inspection')?.insertAdjacentHTML('afterbegin',`<section class="content-inspection"><div class="section-title"><h3>文件复用与输入</h3><div class="actions">${button('content-input-plan','查看传输计划',`data-id="${id}"`,'small')}${['draft','queued','waiting','blocked','paused','failed','canceled'].includes(t.status)?button('content-policy','选择文件',`data-id="${id}"`,'small'):''}</div></div><p>${t.storage_version===2?'内容复用模式：本机不建立完整 input / result 副本':'历史运行：整理后可以释放重复副本'}</p>${transfer?`<div class="content-inline-stats"><span>本次上传 <b>${size(transfer.uploaded_bytes)}</b></span><span>服务器复用 <b>${size(transfer.reused_bytes)}</b></span>${returned?`<span>本次回传 <b>${size(returned.downloaded_bytes)}</b></span>`:''}</div>`:''}<small class="muted">默认只引用前序自身输出，不递归复制前序的 deps 或代码。运行日志独立回传。</small></section>`);
};
const contentBaseAction=handleAction;
handleAction=async function(el){
 const action=el.dataset.action,id=el.dataset.id,revision=dialogRevision;
 if(action==='content-resume-dispatch'){await api('/api/settings',{paused:false});await load();toast('调度已恢复，已入队实验将按规则派发');return;}
 if(action==='content-batch-open'){contentBatchOpen();return;}
 if(action==='content-batch-queue'){contentBatchOpen('queue');return;}
 if(action==='content-selected-queue'){if(!schedulingSelection.size){toast('请先勾选要入队的实验');return;}contentBatchOpen('queue',[...schedulingSelection]);return;}
 if(action.startsWith('content-select-')){
  const selected=contentBatch.tasks.filter(t=>action==='content-select-all'||action==='content-select-pending'&&contentCanQueue(t)||action==='content-select-active'&&['draft','queued','waiting','blocked','paused','staging','launching','running','suspended','canceling'].includes(t.status)||action==='content-select-ended'&&['failed','canceled'].includes(t.status));
  contentBatch.ids=new Set(selected.map(t=>t.id));document.querySelectorAll('[data-content-task]').forEach(box=>box.checked=contentBatch.ids.has(box.dataset.contentTask));await contentBatchPreview();return;
 }
 if(action==='content-policy'){contentPolicyOpen(id);return;}
 if(action==='content-run-files'){
  contentRunView={project:el.dataset.project,attempt:el.dataset.attempt,scope:'outputs',offset:0};modal('运行文件清单',`<label>文件范围<select id="content-run-scope"><option value="outputs">本节点自身输出</option><option value="inputs">执行时的输入快照</option><option value="workspace">完整工作区快照</option></select></label><div id="content-run-file-list">正在读取清单…</div>`,'content-run-files');await contentRunFilesLoad();return;
 }
 if(action==='content-run-prev'||action==='content-run-next'){contentRunView={...contentRunView,offset:Math.max(0,contentRunView.offset+(action==='content-run-next'?100:-100))};await contentRunFilesLoad();return;}
 if(action==='content-storage-refresh'){
  const job=await api(`/api/projects/${id}/storage/refresh`,{});if(revision!==dialogRevision){toast('统计已加入后台');return;}await contentJob(job,async(result,target)=>{target.innerHTML=`<b>统计完成</b><p>内容库 ${size(result.object_bytes)} · 旧目录 ${size(result.legacy_bytes)}</p>`;contentStorageReports.set(id,{report:result,caches:contentStorageReports.get(id)?.caches||[]});await load();});return;
 }
 if(action==='content-compact'){
  modal('整理历史重复文件',`<form id="content-compact-form" data-project="${id}"><p>核验历史文件，建立内容引用后移除重复的 input / result 实体副本。历史下载、输出和日志保留。整理可中断并继续。</p><p class="muted">本机始终整理；可同时选择服务器。共享工作目录会按组处理一次。项目有活动实验时拒绝整理。</p><div class="content-server-choice">${state.servers.map(s=>`<label><input type="checkbox" name="server_ids" value="${s.id}"><span>${esc(s.name)}</span></label>`).join('')}</div><div class="form-footer"><button type="submit" class="primary">开始后台整理</button></div></form>`,'content-compact');return;
 }
 if(action==='content-scan'){
  const job=await api(`/api/projects/${el.dataset.project}/storage/scan`,{server_id:el.dataset.server});if(revision!==dialogRevision){toast('核验已加入后台');return;}await contentJob(job,async(result,target)=>{target.innerHTML=`<b>核验完成</b><p>${result.objects} 个内容 · ${size(result.bytes)} · ${result.invalid.length} 个无效条目</p>`;await load();});return;
 }
 if(action==='content-input-plan'){
  const job=await api(`/api/tasks/${id}/input-plan`,{});if(revision!==dialogRevision){toast('计划计算已加入后台');return;}await contentJob(job,async(result,target)=>{
   const value=await api(`/api/tasks/${id}/input-plan`);if(!target.isConnected)return;
   target.innerHTML=`<b>${Object.keys(value.files).length} 个路径 · ${value.unique_objects} 个唯一内容</b><p>文件引用 ${size(value.logical_bytes)} · 实际内容 ${size(value.unique_bytes)}</p><div class="content-plan-servers">${value.servers.map(s=>`<article><b>${esc(s.server_name)}</b><span>${s.error?esc(s.error):`只需上传 ${size(s.upload_bytes)} · ${s.missing_objects} 个缺失 hash`}</span></article>`).join('')}</div><div class="content-plan-files">${Object.entries(value.files).slice(0,200).map(([p,v])=>`<div><span>${esc(p)}</span><code title="${v.sha256}">${v.sha256.slice(0,12)} · ${size(v.size)}</code></div>`).join('')}</div><small>显示前 200 个路径。不同路径但相同 hash 的内容只传一次。</small>`;
  });return;
 }
 return contentBaseAction(el);
};
const contentBaseSubmit=submitForm;
submitForm=async function(form){
 if(form.id==='content-batch-form'){
  const ids=[...(contentBatch?.ids||[])],action=$('[name=batch_action]',form).value;const job=await api('/api/tasks/batch-action',{task_ids:ids,action});const closed=closeDialogFor(form);await load();
  if(!closed||$('#dialog').open){toast('批量操作已加入后台');return;}await contentJob(job,async(result,target)=>{target.innerHTML=`<b>已提交 ${result.accepted_count} 个 · 未执行 ${result.failed_count} 个 · 跳过 ${result.excluded.length} 个</b><div class="content-result-list">${result.results.map(r=>`<p><b>${esc(state.tasks.find(t=>t.id===r.id)?.name||r.id)}</b><span>${r.accepted?(action==='queue'?'已入队，按依赖和 GPU 条件等待调度':'已提交，实际状态随后台确认更新'):esc(r.error)}</span></p>`).join('')}</div><small>全局调度开关保持原设置。${result.scheduler_paused?'当前已暂停，入队任务不会开始运行。':''}</small>`;await load();});return;
 }
 if(form.id==='content-policy-form'){
  const fd=new FormData(form),changes={code_files:contentLines(fd.get('code_files')).length?contentLines(fd.get('code_files')):null,code_exclude:contentLines(fd.get('code_exclude')),output_files:contentLines(fd.get('output_files')).length?contentLines(fd.get('output_files')):null,dependency_files:{}};
  form.querySelectorAll('[data-content-dependency]:checked').forEach(box=>changes.dependency_files[box.dataset.contentDependency]=contentLines(form.querySelector(`[data-content-patterns="${box.dataset.contentDependency}"]`).value));
  await api('/api/tasks/scheduling',{task_ids:[form.dataset.task],changes});closeDialogFor(form);toast('文件选择已保存，下次执行使用新规则');await load();return;
 }
 if(form.id==='content-compact-form'){
  const fd=new FormData(form),id=form.dataset.project,job=await api(`/api/projects/${id}/storage/compact`,{server_ids:fd.getAll('server_ids')});const closed=closeDialogFor(form);if(!closed||$('#dialog').open){toast('整理已加入后台');return;}await contentJob(job,async(result,target)=>{target.innerHTML=`<b>本机整理完成</b><p>内容库 ${size(result.local.object_bytes)} · 重复目录剩余 ${size(result.local.legacy_bytes)}</p>${result.servers.map(s=>`<p>${s.server_ids.map(id=>esc(serverBy(id)?.name||id)).join(' + ')}：${s.error?esc(s.error):'整理完成'}</p>`).join('')}`;await api(`/api/projects/${id}/storage/refresh`,{});await load();});return;
 }
 return contentBaseSubmit(form);
};
document.addEventListener('change',event=>{const el=event.target;if(el.matches('[data-content-task]')){el.checked?contentBatch.ids.add(el.dataset.contentTask):contentBatch.ids.delete(el.dataset.contentTask);contentBatchPreview();}if(el.name==='batch_action'&&el.closest('#content-batch-form'))contentBatchPreview();if(el.id==='content-run-scope'){contentRunView={...contentRunView,scope:el.value,offset:0};contentRunFilesLoad().catch(e=>toast(e.message,true));}});
