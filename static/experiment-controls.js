'use strict';
labels.suspended='运行已暂停';labels.canceled='已中断';colors.suspended='amber';
const controlBaseTable=tasksTable,controlBaseDetail=taskDetail,controlBaseAction=handleAction;
function experimentButtons(t,detail=false){
  if(t.archived)return '';
  const id=`data-id="${esc(t.id)}"`;
  let html='';
  if(detail&&['draft','paused'].includes(t.status))html+=button('task-queue',t.status==='paused'?'继续排队':'入队',id,'small');
  if(detail&&['draft','queued','waiting','blocked'].includes(t.status))html+=button('task-pause','暂停排队',id,'small');
  if(t.status==='running')html+=button('task-pause','暂停运行',id,'small');
  if(t.status==='suspended')html+=button('task-resume','继续运行',id,'small primary');
  if(['draft','paused','queued','waiting','blocked','staging','launching','running','suspended','canceling'].includes(t.status))html+=button('task-cancel',t.status==='canceling'?'正在中断…':'中断整个实验',id+(t.status==='canceling'?' disabled':''),'small danger');
  return html;
}
tasksTable=function(tasks,compact){
  let html=controlBaseTable(tasks,compact);
  for(const t of tasks){const mark=button('task-detail','详情',`data-id="${t.id}"`,'small');html=html.replace(mark,mark+experimentButtons(t));if(['queued','waiting','blocked'].includes(t.status))html=html.replace(button('task-pause','暂停',`data-id="${t.id}"`,'small'),button('task-pause','暂停排队',`data-id="${t.id}"`,'small'));}
  return html;
};
function refreshExperimentControls(){
  if(dialogKind!=='task-detail'||!$('#dialog').open)return;
  const task=state.tasks.find(t=>t.id===selectedTask),group=$('[data-experiment-controls]');if(!task||!group)return;
  const signature=JSON.stringify([task.status,task.reason,task.exit_code,task.synced_at,task.archived]);if(group.dataset.signature===signature)return;group.dataset.signature=signature;
  group.querySelectorAll('[data-action=task-cancel],[data-action=task-pause],[data-action=task-resume],[data-action=task-queue]').forEach(el=>el.remove());group.insertAdjacentHTML('afterbegin',experimentButtons(task,true));
  const status=$('#experiment-live-status');if(status)status.innerHTML=`${badge(task.status)}<span>${esc(task.reason||'')}${task.exit_code!=null?' · 退出码 '+task.exit_code:''}</span>`;
}
taskDetail=async function(id){
  await controlBaseDetail(id);
  const group=$('#dialog-body [data-action=artifacts]')?.closest('.actions');if(!group)return;group.setAttribute('data-experiment-controls','');
  const live=document.createElement('div');live.id='experiment-live-status';live.className='experiment-live-status';
  const first=$('#dialog-body').firstElementChild;if(first?.classList.contains('tag'))first.replaceWith(live);else group.before(live);refreshExperimentControls();
};
handleAction=async function(el){
  if(el.dataset.action==='task-resume'){
    await api(`/api/tasks/${el.dataset.id}/action`,{action:'resume'});toast('继续原运行已提交');await load();refreshExperimentControls();return;
  }
  if(el.dataset.action==='task-cancel'){
    modal('中断整个实验',`<p>停止此实验的脚本、后续命令及已启动的子进程。先发送终止信号，仍未退出的进程约 10 秒后强制终止，随后回传已生成的文件和日志。</p><p class="muted">准备阶段的中断会阻止脚本启动；重新执行会创建新运行，原记录保留。</p>${button('confirm-task','中断整个实验',`data-id="${el.dataset.id}" data-operation="cancel"`,'danger')}`,'confirm');return;
  }
  const result=await controlBaseAction(el);refreshExperimentControls();return result;
};
if(document.body.dataset.authorized==='true'){
  $('.top-actions')?.insertAdjacentHTML('afterbegin','<a class="small guide-link" href="/api/experiment-guide" download>实验指南 ↓</a>');
  setInterval(refreshExperimentControls,1000);
}
