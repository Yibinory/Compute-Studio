'use strict';
const taskArchiveViews=new Map();let taskArchiveDialog=null,taskArchiveSequence=0;
const taskArchiveScope=()=>route()[0]==='project'?'project/'+route()[1]:'tasks';
function taskArchiveVisible(){const key=taskArchiveScope();if(!taskArchiveViews.has(key)){let value=false;try{value=sessionStorage.getItem('compute-task-archives:'+key)==='true';}catch{}taskArchiveViews.set(key,value);}return taskArchiveViews.get(key);}
function taskArchiveToolbar(tasks){
 if(!state.runtime_features?.task_archive)return '';
 const archived=tasks.filter(t=>t.archived).length;
 return `<div class="task-archive-toolbar"><span>当前实验 <b>${tasks.length-archived}</b><small>已归档 ${archived}</small></span><div class="actions"><label><input type="checkbox" data-task-archives-visible ${taskArchiveVisible()?'checked':''}>显示归档实验</label>${button('tasks-archive-open','批量归档','','small')}${archived?button('tasks-unarchive-open','恢复归档','','small'):''}</div></div>`;
}
const taskArchiveBaseTable=tasksTable;
tasksTable=function(tasks,compact=false){
 if(!state.runtime_features?.task_archive)return taskArchiveBaseTable(tasks,compact);
 const visible=tasks.filter(t=>!t.archived||!compact&&taskArchiveVisible()),template=document.createElement('template');template.innerHTML=taskArchiveBaseTable(visible,compact);
 if(!compact)for(const task of visible){
  const detail=template.content.querySelector(`[data-action=task-detail][data-id="${task.id}"]`),row=detail?.closest('tr');if(!row)continue;
  if(task.archived){row.classList.add('task-is-archived');row.querySelectorAll('[data-action=task-queue],[data-action=task-retry],[data-action=task-pause],[data-action=task-resume],[data-action=task-cancel],[data-action=edit-task]').forEach(el=>el.remove());}
  detail.insertAdjacentHTML('afterend',button('task-archive-one',task.archived?'恢复归档':'归档',`data-id="${task.id}"`,'small task-archive-button'));
  if(task.archived)row.querySelector('td:not(.scheduling-select-cell)')?.insertAdjacentHTML('beforeend','<span class="task-archive-tag">已归档</span>');
 }
 return (!compact?taskArchiveToolbar(tasks):'')+template.innerHTML;
};
const taskArchiveBaseGraph=scheduleGraph;
scheduleGraph=function(tasks){
 if(!state.runtime_features?.task_archive)return taskArchiveBaseGraph(tasks);
 const visible=tasks.filter(t=>!t.archived||taskArchiveVisible()),template=document.createElement('template');template.innerHTML=taskArchiveBaseGraph(visible);
 for(const task of visible.filter(t=>t.archived)){const node=template.content.querySelector(`[data-node-id="${task.id}"]`);if(node){node.classList.add('task-node-archived');node.querySelector('.flow-status-text')?.insertAdjacentHTML('afterbegin','<span class="task-archive-tag">已归档</span>');}}
 return taskArchiveToolbar(tasks)+template.innerHTML;
};
const taskArchiveBaseSchedulingTasks=schedulingTasks;
schedulingTasks=function(){return taskArchiveBaseSchedulingTasks().filter(t=>!t.archived);};
const taskArchiveBaseDetail=taskDetail;
taskDetail=async function(id){
 await taskArchiveBaseDetail(id);if(!state.runtime_features?.task_archive||!$('#dialog').open||dialogKind!=='task-detail'||selectedTask!==id)return;
 const task=state.tasks.find(t=>t.id===id);if(!task)return;
 if(task.archived)$('#dialog-body').querySelectorAll('[data-action=task-queue],[data-action=task-retry],[data-action=task-pause],[data-action=task-resume],[data-action=task-cancel],[data-action=edit-task],[data-action=content-policy]').forEach(el=>el.remove());
 $('#dialog-body').insertAdjacentHTML('afterbegin',`<aside class="task-archive-banner"><div><strong>${task.archived?'此实验已归档':'实验归档'}</strong><p>${task.archived?'已退出调度和批量入队；日志、输出、Notes 与结果关联保留。':'旧版本或不再使用的实验可以归档，后续仍可恢复查看。'}</p></div>${button('task-archive-one',task.archived?'恢复归档':'归档实验',`data-id="${task.id}"`,'small')}</aside>`);
};
function taskArchiveOpen(archived,ids=[]){
 const [page,id]=route(),tasks=(state.tasks||[]).filter(t=>(page!=='project'||t.project_id===id)&&!!t.archived!==archived);
 taskArchiveDialog={tasks,ids:new Set(ids),archived};
 modal(archived?'归档不再使用的实验':'恢复归档实验',`<form id="task-archive-form"><p class="muted">${archived?'归档后默认隐藏，退出调度，不再参与批量入队；日志、输出和实验关联保留。正在运行、回传或状态未知的实验会跳过。未完成前序若仍被保留的后续引用，会提示一起选择。':'恢复后重新显示。原来排队中的实验保持暂停，需手动入队；失败实验需手动重新执行。'}</p><div class="task-archive-selection-controls"><input type="search" data-archive-search placeholder="搜索实验名称或 #编号" aria-label="搜索归档实验"><div class="actions">${archived?button('tasks-archive-failed','选择失败／中断','','small')+button('tasks-archive-pending','选择未完成','','small')+button('tasks-archive-descendants','补选所有后续','','small'):''}${button('tasks-archive-all','全选可见','','small')}${button('tasks-archive-none','清空','','small')}</div></div><div class="task-archive-selection">${tasks.map(t=>`<label data-archive-row="${t.id}"><input type="checkbox" data-task-archive-select="${t.id}" ${taskArchiveDialog.ids.has(t.id)?'checked':''}><span><b>#${t.seq} ${esc(t.name)}</b><small>${esc(projectBy(t.project_id)?.name||'')}</small></span>${badge(t.status)}</label>`).join('')||'<p class="muted">暂无可选择的实验</p>'}</div><div id="task-archive-preview" class="task-archive-review" aria-live="polite"></div><p id="task-archive-error" class="error-text" role="alert"></p><div class="form-footer">${button('close-dialog','取消')}<button type="submit" class="primary" disabled>${archived?'确认归档':'恢复所选实验'}</button></div></form>`,'task-archive');taskArchivePreview();
}
async function taskArchivePreview(){
 const view=taskArchiveDialog,form=$('#task-archive-form'),seq=++taskArchiveSequence;if(!view||!form)return;
 const target=$('#task-archive-preview',form),submit=$('button[type=submit]',form);submit.disabled=true;
 if(!view.ids.size){target.textContent='请选择实验，再确认状态和依赖保护。';return;}
 target.textContent='正在检查状态和依赖…';
 try{
  const plan=await api('/api/tasks/archive/preview',{task_ids:[...view.ids],archived:view.archived});if(!target.isConnected||seq!==taskArchiveSequence)return;
  target.innerHTML=`<strong>可${view.archived?'归档':'恢复'} ${plan.eligible.length} 个 · 跳过 ${plan.excluded.length} 个</strong>${plan.excluded.map(t=>`<p><b>#${t.seq??'?'} ${esc(t.name)}</b><span>${esc(t.reason)}</span></p>`).join('')}`;submit.disabled=!plan.eligible.length;submit.textContent=`确认${view.archived?'归档':'恢复'} ${plan.eligible.length} 个实验`;
 }catch(error){if(target.isConnected&&seq===taskArchiveSequence)target.textContent=error.message;}
}
const taskArchiveBaseAction=handleAction;
handleAction=async function(el){
 const action=el.dataset.action;
 if(action==='task-archive-one'){const t=state.tasks.find(t=>t.id===el.dataset.id);if(t)taskArchiveOpen(!t.archived,[t.id]);return;}
 if(action==='tasks-archive-open'||action==='tasks-unarchive-open'){taskArchiveOpen(action==='tasks-archive-open');return;}
 if(action?.startsWith('tasks-archive-')){
  const view=taskArchiveDialog;if(!view)return;
  const visible=view.tasks.filter(t=>!$(`[data-archive-row="${t.id}"]`)?.hidden);
  if(action==='tasks-archive-none')view.ids.clear();
  else if(action==='tasks-archive-descendants'){
   let changed=true;while(changed){changed=false;for(const t of view.tasks)if(!view.ids.has(t.id)&&t.depends_on.some(id=>view.ids.has(id))){view.ids.add(t.id);changed=true;}}
  }else for(const t of visible)if(action==='tasks-archive-all'||action==='tasks-archive-failed'&&['failed','canceled'].includes(t.status)||action==='tasks-archive-pending'&&t.status!=='success')view.ids.add(t.id);
  document.querySelectorAll('[data-task-archive-select]').forEach(box=>box.checked=view.ids.has(box.dataset.taskArchiveSelect));await taskArchivePreview();return;
 }
 return taskArchiveBaseAction(el);
};
const taskArchiveBaseSubmit=submitForm;
submitForm=async function(form){
 if(form.id!=='task-archive-form')return taskArchiveBaseSubmit(form);
 const view=taskArchiveDialog;if(form.dataset.saving||!view)return;form.dataset.saving='true';
 try{
  const result=await api('/api/tasks/archive',{task_ids:[...view.ids],archived:view.archived});
  if(!result.changed_count){$('#task-archive-error',form).textContent='状态或依赖已变化，未修改实验。请查看最新检查结果。';await taskArchivePreview();return;}
  closeDialogFor(form);schedulingSelection.clear();toast(`已${view.archived?'归档':'恢复'} ${result.changed_count} 个实验${result.excluded.length?'，跳过 '+result.excluded.length+' 个':''}`);await load();
 }catch(error){$('#task-archive-error',form).textContent=error.message;}
 finally{delete form.dataset.saving;}
};
document.addEventListener('change',event=>{
 const el=event.target;
 if(el.matches('[data-task-archives-visible]')){const key=taskArchiveScope();taskArchiveViews.set(key,el.checked);try{sessionStorage.setItem('compute-task-archives:'+key,String(el.checked));}catch{}render();styleControls();}
 if(el.matches('[data-task-archive-select]')){el.checked?taskArchiveDialog.ids.add(el.dataset.taskArchiveSelect):taskArchiveDialog.ids.delete(el.dataset.taskArchiveSelect);taskArchivePreview();}
});
document.addEventListener('input',event=>{if(!event.target.matches('[data-archive-search]'))return;const q=event.target.value.trim().toLocaleLowerCase();document.querySelectorAll('[data-archive-row]').forEach(row=>row.hidden=!row.textContent.toLocaleLowerCase().includes(q));});
if(document.body.dataset.authorized==='true'&&state.settings)render();
