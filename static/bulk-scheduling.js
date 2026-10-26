'use strict';
const schedulingStatuses=new Set(['draft','queued','waiting','blocked','paused','failed','canceled']);
const schedulingFields={level:'执行级别',priority:'优先级',memory_mb:'峰值显存需求（MB）',max_utilization:'低干扰利用率峰值上限（%）',gpu_name:'GPU 型号包含',server_ids:'允许的服务器',max_retries:'OOM 最大自动重试次数',environment:'执行环境'};
let schedulingSelection=new Set(),schedulingScope='',schedulingDraft=null;
function schedulingTasks(){const [page,id]=route();return page==='tasks'?state.tasks||[]:page==='project'&&projectTab==='tasks'?(state.tasks||[]).filter(t=>t.project_id===id):[];}
function schedulingReconcile(){
  const scope=route().join('/')+'/'+projectTab;if(scope!==schedulingScope){schedulingSelection.clear();schedulingScope=scope;}
  const allowed=new Set(schedulingTasks().filter(t=>schedulingStatuses.has(t.status)).map(t=>t.id));
  for(const id of schedulingSelection)if(!allowed.has(id))schedulingSelection.delete(id);
}
function schedulingCheckbox(task,dialog=false){const eligible=!task.archived&&schedulingStatuses.has(task.status),selected=dialog?schedulingDraft?.ids:schedulingSelection;
  return `<input type="checkbox" class="scheduling-check" data-scheduling-${dialog?'draft':'select'}="${esc(task.id)}" aria-label="选择 ${esc(task.name)}" ${selected?.has(task.id)?'checked':''} ${eligible?'':'disabled'} title="${eligible?'选择此实验':'此状态已执行，不能修改调度策略'}">`;
}
const schedulingBaseTable=tasksTable;
tasksTable=function(tasks,compact=false){
  const html=schedulingBaseTable(tasks,compact);if(compact||!tasks.length)return html;schedulingReconcile();let i=0;
  return `<div class="scheduling-toolbar"><span>已选 <b data-scheduling-count>${schedulingSelection.size}</b> 个实验</span>${state.runtime_features?.batch_queue?button('content-selected-queue','所选入队','','small primary'):''}${button('bulk-scheduling','修改所选策略','','small')}${button('scheduling-clear','清空选择','','small')}<small>失败／中断实验仅可改环境</small></div>`+
    html.replace('<thead><tr>','<thead><tr><th class="scheduling-select-cell"><input type="checkbox" class="scheduling-check" data-scheduling-all aria-label="选择本列表全部可编辑实验"></th>').replace(/<tbody>([\s\S]*?)<\/tbody>/,(_,body)=>'<tbody>'+body.replace(/<tr>/g,()=>'<tr><td class="scheduling-select-cell">'+schedulingCheckbox(tasks[i++])+'</td>')+'</tbody>');
};
function schedulingRefreshSelection(){
  const tasks=schedulingTasks().filter(t=>schedulingStatuses.has(t.status)),all=$('[data-scheduling-all]');
  if(all){all.checked=tasks.length>0&&tasks.every(t=>schedulingSelection.has(t.id));all.indeterminate=tasks.some(t=>schedulingSelection.has(t.id))&&!all.checked;all.disabled=!tasks.length;}
  document.querySelectorAll('[data-scheduling-select]').forEach(el=>el.checked=schedulingSelection.has(el.dataset.schedulingSelect));
  document.querySelectorAll('[data-scheduling-count]').forEach(el=>el.textContent=schedulingSelection.size);
}
const schedulingBaseRender=render;
render=function(){schedulingReconcile();schedulingBaseRender();const [page]=route();if(page==='tasks'||page==='project'&&projectTab==='tasks')$('#main .page-title .actions')?.insertAdjacentHTML('beforeend',button('bulk-scheduling-open','批量修改策略'));schedulingRefreshSelection();};
function schedulingCommon(tasks,key,fallback){return tasks.length&&tasks.every(t=>JSON.stringify(t[key])===JSON.stringify(tasks[0][key]))?tasks[0][key]:fallback;}
function schedulingOpen(useSelection=false){
  schedulingReconcile();const tasks=schedulingTasks();if(!tasks.some(t=>schedulingStatuses.has(t.status))){toast('当前没有可修改调度策略的实验',true);return;}
  if(useSelection&&!schedulingSelection.size){toast('请先勾选要修改的实验',true);return;}
  const ids=new Set(schedulingSelection);schedulingDraft={ids,tasks};const selected=tasks.filter(t=>ids.has(t.id));
  function item(key,control){return `<section class="scheduling-field"><label class="scheduling-field-toggle"><input class="scheduling-check" type="checkbox" data-scheduling-field="${key}"><span>${schedulingFields[key]}</span></label><div data-scheduling-controls="${key}">${control}</div></section>`;}
  function number(key,fallback,min,max,step=1){return `<input type="number" name="${key}" disabled value="${esc(schedulingCommon(selected,key,fallback))}" min="${min}" max="${max}" step="${step}" required aria-label="${schedulingFields[key]}">`;}
  const level=schedulingCommon(selected,'level','idle_only'),serverIds=schedulingCommon(selected,'server_ids',[]),taskEnvs=schedulingCommon(selected,'server_conda_envs',{}),commonEnv=schedulingCommon(selected,'conda_env',''),envMode=Object.keys(taskEnvs||{}).length?'servers':commonEnv?'common':'inherit';
  modal('批量修改调度策略',`<form id="bulk-scheduling-form"><p class="muted">勾选实验，再勾选需要统一修改的字段。未勾选的字段保留各实验原值；保存不会自动入队或重新执行。</p><details class="scheduling-selection" ${ids.size?'':'open'}><summary>选择实验 · <span data-scheduling-draft-count>${ids.size}</span> / ${tasks.filter(t=>schedulingStatuses.has(t.status)).length} 个可编辑</summary><label class="scheduling-field-toggle"><input type="checkbox" class="scheduling-check" data-scheduling-draft-all>全选可编辑实验</label><div class="scheduling-task-list">${tasks.map(t=>`<label class="scheduling-task-row">${schedulingCheckbox(t,true)}<span><b>${esc(t.name)}</b><small>${esc(projectBy(t.project_id)?.name||'')} · #${t.seq}</small></span>${badge(t.status)}</label>`).join('')}</div></details><div class="scheduling-grid">`+
    item('level',`<select name="level" disabled>${[['idle_only','空闲卡'],['low_interference','低干扰：按峰值利用率共享'],['emergency','紧急：可共享 GPU']].map(([v,label])=>`<option value="${v}" ${v===level?'selected':''}>${label}</option>`).join('')}</select>`)+
    item('priority',number('priority',50,1,999)+'<small>1 最优先，999 最低。</small>')+
    item('memory_mb',number('memory_mb',0,0,1000000)+'<small>0 使用调度设置中的默认显存预留。</small>')+
    item('max_utilization',number('max_utilization',30,0,100,'any')+'<small>仅对低干扰执行级别生效。</small>')+
    item('gpu_name',`<input name="gpu_name" disabled value="${esc(schedulingCommon(selected,'gpu_name',''))}" placeholder="如 A100；留空取消型号限制">`)+
    item('max_retries',number('max_retries',2,0,20)+'<small>仅调整 OOM 重试上限，已尝试次数保留。</small>')+
    item('environment',`<select name="environment_mode" disabled><option value="inherit" ${envMode==='inherit'?'selected':''}>清除实验配置，使用项目／服务器默认环境</option><option value="servers" ${envMode==='servers'?'selected':''}>分别选择每台服务器的环境</option><option value="common" ${envMode==='common'?'selected':''}>所有服务器使用同一环境名称</option></select><div data-bulk-env-common hidden><input name="conda_env" disabled value="${esc(schedulingCommon(selected,'conda_env',''))}" placeholder="如 pytorch；不要填写另一台服务器的绝对路径"></div><div data-bulk-env-servers hidden>${state.servers.map(s=>`<div class="scheduling-env-server"><label>${esc(s.name)}<select name="task_env_${esc(s.id)}" disabled data-environment-server="${esc(s.id)}" data-fallback="继承项目／服务器默认环境">${environmentOptions(s,taskEnvs?.[s.id]||'','继承项目／服务器默认环境')}</select></label><small data-environment-hint="${esc(s.id)}">${esc(environmentHint(s))}</small>${environmentRefresh(s)}</div>`).join('')}</div><small>失败或已中断实验也可批量修改执行环境。保存不自动重跑，原运行记录和环境快照保留。</small>`)+
    item('server_ids',`<div class="scheduling-servers">${(state.servers||[]).map(s=>`<label class="scheduling-field-toggle"><input type="checkbox" class="scheduling-check" name="server_ids" value="${esc(s.id)}" ${serverIds.includes(s.id)?'checked':''} disabled><span>${esc(s.name)}${s.enabled===false?'（已停用）':''}</span></label>`).join('')||'<small>尚未配置服务器</small>'}</div><small>不选任何服务器表示取消限制，交由调度器自动选择。</small>`)+
    `</div><div class="scheduling-review" aria-live="polite" id="scheduling-review"></div><p id="scheduling-error" class="error-text" role="alert"></p><div class="form-footer" data-dialog-summary="请选择实验和修改字段">${button('close-dialog','取消')}<button type="submit" class="primary" disabled>保存调度策略</button></div></form>`,'bulk-scheduling');schedulingDraftRefresh();
}
function schedulingDraftRefresh(){
  const form=$('#bulk-scheduling-form');if(!form||!schedulingDraft)return;const fields=[...form.querySelectorAll('[data-scheduling-field]:checked')].map(el=>el.dataset.schedulingField),eligible=schedulingDraft.tasks.filter(t=>schedulingStatuses.has(t.status));
  const ended=schedulingDraft.tasks.some(t=>schedulingDraft.ids.has(t.id)&&['failed','canceled'].includes(t.status));
  for(const toggle of form.querySelectorAll('[data-scheduling-field]')){toggle.disabled=ended&&toggle.dataset.schedulingField!=='environment';if(toggle.disabled)toggle.checked=false;}
  const activeFields=[...form.querySelectorAll('[data-scheduling-field]:checked')].map(el=>el.dataset.schedulingField);
  form.querySelectorAll('[data-scheduling-draft-count]').forEach(el=>el.textContent=schedulingDraft.ids.size);
  const all=$('[data-scheduling-draft-all]',form);all.checked=eligible.length>0&&eligible.every(t=>schedulingDraft.ids.has(t.id));all.indeterminate=eligible.some(t=>schedulingDraft.ids.has(t.id))&&!all.checked;
  for(const field of Object.keys(schedulingFields)){const enabled=activeFields.includes(field);$(`[data-scheduling-controls="${field}"]`,form).querySelectorAll('input,select').forEach(el=>el.disabled=!enabled);$(`[data-scheduling-controls="${field}"]`,form).closest('section').classList.toggle('enabled',enabled);}
  const mode=form.elements.environment_mode.value,envEnabled=activeFields.includes('environment');
  $('[data-bulk-env-common]',form).hidden=mode!=='common';$('[data-bulk-env-servers]',form).hidden=mode!=='servers';
  form.elements.conda_env.disabled=!envEnabled||mode!=='common';$('[data-bulk-env-servers]',form).querySelectorAll('select').forEach(el=>el.disabled=!envEnabled||mode!=='servers');
  $('#scheduling-review').textContent=(ended?'所选包含失败／中断实验，仅可修改执行环境；其他调度字段未提交。 ':'')+(activeFields.length?`将更新 ${schedulingDraft.ids.size} 个实验：${activeFields.map(f=>schedulingFields[f]).join('、')}。其他字段保留原值。`:'尚未勾选修改字段。');
  $('.form-footer',form).dataset.dialogSummary=`${schedulingDraft.ids.size} 个实验 · ${activeFields.length} 项修改`;
  $('button[type=submit]',form).disabled=!schedulingDraft.ids.size||!activeFields.length;
}
const schedulingBaseAction=handleAction;
handleAction=async function(el){if(el.dataset.action==='bulk-scheduling'||el.dataset.action==='bulk-scheduling-open'){schedulingOpen(el.dataset.action==='bulk-scheduling');return;}if(el.dataset.action==='scheduling-clear'){schedulingSelection.clear();schedulingRefreshSelection();return;}return schedulingBaseAction(el);};
const schedulingBaseSubmit=submitForm;
submitForm=async function(form){
  if(form.id!=='bulk-scheduling-form')return schedulingBaseSubmit(form);
  const fd=new FormData(form),changes={};for(const toggle of form.querySelectorAll('[data-scheduling-field]:checked')){const key=toggle.dataset.schedulingField;if(key==='environment'){changes.conda_env=fd.get('environment_mode')==='common'?fd.get('conda_env'):'';changes.server_conda_envs={};if(fd.get('environment_mode')==='servers')for(const s of state.servers)changes.server_conda_envs[s.id]=fd.get('task_env_'+s.id)||'';}else changes[key]=key==='server_ids'?fd.getAll(key):fd.get(key);}
  const ids=[...(schedulingDraft?.ids||[])];if(!ids.length||!Object.keys(changes).length)throw Error('请选择实验并勾选修改字段');
  const error=$('#scheduling-error');error.textContent='';
  try{const result=await api('/api/tasks/scheduling',{task_ids:ids,changes});schedulingSelection.clear();schedulingDraft=null;closeDialogFor(form);toast(`已保存 ${result.count} 个实验的调度策略（${result.changed_count} 个有变化）`);await load();}
  catch(e){error.textContent=e.message;throw e;}
};
document.addEventListener('change',event=>{
  const el=event.target;if(el.matches('[data-scheduling-select]')){el.checked?schedulingSelection.add(el.dataset.schedulingSelect):schedulingSelection.delete(el.dataset.schedulingSelect);schedulingRefreshSelection();}
  if(el.matches('[data-scheduling-all]')){for(const t of schedulingTasks().filter(t=>schedulingStatuses.has(t.status)))el.checked?schedulingSelection.add(t.id):schedulingSelection.delete(t.id);schedulingRefreshSelection();}
  if(el.closest('#bulk-scheduling-form')){if(el.matches('[data-scheduling-draft]'))el.checked?schedulingDraft.ids.add(el.dataset.schedulingDraft):schedulingDraft.ids.delete(el.dataset.schedulingDraft);if(el.matches('[data-scheduling-draft-all]')){for(const t of schedulingDraft.tasks.filter(t=>schedulingStatuses.has(t.status)))el.checked?schedulingDraft.ids.add(t.id):schedulingDraft.ids.delete(t.id);document.querySelectorAll('[data-scheduling-draft]').forEach(box=>box.checked=schedulingDraft.ids.has(box.dataset.schedulingDraft));}schedulingDraftRefresh();}
});
