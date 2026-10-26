'use strict';
const taskFilterStates=['draft','queued','waiting','blocked','paused','staging','launching','running','suspended','canceling','syncing','sync_failed','success','failed','canceled','lost'];
const taskFilterGroups={pending:['draft','queued','waiting','blocked','paused'],active:['staging','launching','running','suspended','canceling','syncing'],attention:['failed','sync_failed','lost'],ended:['success','failed','canceled']};
let taskFilterDrafts=new Map(),taskFilterTimer=null;
function taskFilterScope(){const [page,id]=route();return page==='tasks'?'tasks':page==='project'&&projectTab==='tasks'&&!graphMode?'project/'+id:'';}
function taskFilterDraft(){const key=taskFilterScope();if(!taskFilterDrafts.has(key)){let value={status:'',query:'',project:''};try{value={...value,...JSON.parse(sessionStorage.getItem('compute-task-filter:'+key)||'{}')};}catch{}taskFilterDrafts.set(key,value);}return taskFilterDrafts.get(key);}
function taskFilterApply(tasks){if(!taskFilterScope())return tasks;const f=taskFilterDraft(),query=f.query.trim().toLocaleLowerCase();return tasks.filter(t=>(!f.status||(taskFilterGroups[f.status]||[f.status]).includes(t.status))&&(!f.project||t.project_id===f.project)&&(!query||('#'+t.seq+' '+t.name+' '+(projectBy(t.project_id)?.name||'')).toLocaleLowerCase().includes(query)));}
const taskFilterBaseTable=tasksTable;
tasksTable=function(tasks,compact=false){
 if(compact||!taskFilterScope())return taskFilterBaseTable(tasks,compact);
 const f=taskFilterDraft(),visible=taskFilterApply(tasks),option=(v,l)=>`<option value="${v}" ${f.status===v?'selected':''}>${l} · ${tasks.filter(t=>(taskFilterGroups[v]||[v]).includes(t.status)).length}</option>`;
 const filters=`<section class="task-list-filters" aria-label="实验列表筛选"><label>实验状态<select data-task-filter="status"><option value="">全部状态 · ${tasks.length}</option>${[['pending','未开始'],['active','执行与回传中'],['attention','需处理'],['ended','已结束']].map(([v,l])=>option(v,l)).join('')}<optgroup label="具体状态">${taskFilterStates.map(v=>option(v,labels[v]||({'suspended':'运行暂停'})[v]||v)).join('')}</optgroup></select></label>${route()[0]==='tasks'?`<label>项目<select data-task-filter="project"><option value="">全部项目</option>${state.projects.map(p=>`<option value="${p.id}" ${f.project===p.id?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label>`:''}<label class="task-filter-search">搜索实验<input data-task-filter="query" type="search" value="${esc(f.query)}" placeholder="实验名称或 #编号"></label><div class="task-filter-result"><span>显示 <b>${visible.length}</b> / ${tasks.length}</span>${button('task-filter-clear','重置','','small')}</div></section>`;
 return filters+(visible.length?taskFilterBaseTable(visible,compact):empty('没有匹配的实验','调整状态、项目或搜索条件可查看其他实验。'));
};
const taskFilterBaseSchedulingTasks=schedulingTasks;
schedulingTasks=function(){return taskFilterApply(taskFilterBaseSchedulingTasks());};
function taskFilterUpdate(el){
 const scope=taskFilterScope();if(!scope)return;const f=taskFilterDraft();f[el.dataset.taskFilter]=el.value;try{sessionStorage.setItem('compute-task-filter:'+scope,JSON.stringify(f));}catch{}
 const start=el.selectionStart,end=el.selectionEnd,key=el.dataset.taskFilter;render();styleControls();const replacement=$(`[data-task-filter="${key}"]`);if(replacement){replacement.focus();if(key==='query'&&start!=null)replacement.setSelectionRange(start,end);}
}
const taskFilterBaseAction=handleAction;
handleAction=async function(el){if(el.dataset.action==='task-filter-clear'){const scope=taskFilterScope();taskFilterDrafts.delete(scope);try{sessionStorage.removeItem('compute-task-filter:'+scope);}catch{}render();styleControls();return;}return taskFilterBaseAction(el);};
document.addEventListener('change',event=>{if(event.target.matches('[data-task-filter]:not([data-task-filter=query])'))taskFilterUpdate(event.target);});
document.addEventListener('input',event=>{if(event.target.matches('[data-task-filter=query]')){clearTimeout(taskFilterTimer);const el=event.target;taskFilterTimer=setTimeout(()=>{if(el.isConnected)taskFilterUpdate(el);},160);}});
