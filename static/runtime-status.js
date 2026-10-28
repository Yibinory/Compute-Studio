'use strict';
labels.staging='准备中';
function runtimeLabel(task){
  if(task.status==='lost'&&task.runtime_progress?.phase==='recovering')return '异常恢复中';
  if(task.cancel_requested&&['staging','launching','canceling'].includes(task.status))return '中断中';
  if(task.status==='staging')return ({validating:'检查配置',assembling:'整理输入',hashing:'计算校验值',transferring:'传输输入',verifying:'核验输入',resource_check:'复查资源'})[task.runtime_progress?.phase]||'准备中';
  return labels[task.status]||task.status;
}
const runtimeBaseApi=api;
api=async function(path,data,method){
  const result=await runtimeBaseApi(path,data,method);
  if(/^\/api\/tasks\/[^/]+\/action$/.test(path)&&state.tasks){const task=result.task||(result.project_id?result:null);if(task){const index=state.tasks.findIndex(t=>t.id===task.id);if(index>=0){state.tasks[index]={...state.tasks[index],...task};render();}}}
  return result;
};
const runtimeBaseTable=tasksTable;
tasksTable=function(tasks,compact){let i=0;return runtimeBaseTable(tasks,compact).replace(/<tbody>([\s\S]*?)<\/tbody>/,(_,body)=>'<tbody>'+body.replace(/<tr>[\s\S]*?<\/tr>/g,row=>{const task=tasks[i++];return row.replace(badge(task.status),badge(task.status,runtimeLabel(task)));})+'</tbody>');};
function refreshRuntimePanel(){
  if(!$('#dialog').open||dialogKind!=='task-detail')return;
  const task=state.tasks.find(t=>t.id===selectedTask);if(!task)return;
  let panel=$('#experiment-runtime-progress');if(!panel){panel=document.createElement('div');panel.id='experiment-runtime-progress';panel.className='runtime-progress-panel';$('#dialog-body').prepend(panel);}
  const p=task.runtime_progress||{},active=['staging','launching','syncing','sync_failed','canceling'].includes(task.status)||task.status==='lost'&&p.phase==='recovering';
  panel.hidden=!active;const signature=JSON.stringify([task.status,task.reason,p]);if(panel.dataset.signature===signature)return;panel.dataset.signature=signature;
  const returning=['syncing','sync_failed'].includes(task.status),byteProgress=p.phase==='transferring'||p.phase==='verifying'||p.phase==='syncing'&&Number.isFinite(p.total);
  panel.innerHTML=`<b>${esc(runtimeLabel(task))}</b><p>${esc(task.reason||p.label||'后台正在处理')}</p>${byteProgress?`<div class="runtime-byte-progress"><span>${returning?'本次回传字节进度':'文件字节进度'}</span><b>${p.percent||0}%</b></div><progress max="100" value="${p.percent||0}"></progress><small>${returning?'已接收':'已发送'} ${size(p.bytes||0)} / ${size(p.total||0)}。${returning?'已有完整内容直接复用；接收完成后还需核验 hash 并保存日志。':'100% 后仍需完成核验、资源复查和启动确认。'}</small>`:''}${task.status==='syncing'&&task.sync_last_error?`<small>上次回传错误：${esc(task.sync_last_error)}；当前正在恢复。</small>`:''}<small>更新于 ${fmt(p.updated_at)}</small>`;
  const live=$('#experiment-live-status');if(live)live.innerHTML=badge(task.status,runtimeLabel(task))+'<span>'+esc(task.reason||'')+'</span>';
}
const runtimeBaseDetail=taskDetail;
taskDetail=async function(id){await runtimeBaseDetail(id);refreshRuntimePanel();};
const recoveryBaseTaskDetail=taskDetail;
taskDetail=async function(id){await recoveryBaseTaskDetail(id);if(dialogKind!=='task-detail'||selectedTask!==id)return;const t=state.tasks.find(v=>v.id===id);if(!t?.interruption)return;const previous=[...(t.previous_attempts||[])].reverse().find(v=>v.interrupted),attempt=t.status==='failed'?t.attempt_id:previous?.attempt_id;const panel=document.createElement('div');panel.className='notice';panel.id='interruption-history';panel.innerHTML=`<b>服务器重启恢复记录</b><p>已自动重新入队 ${t.interruption_retries||0} 次。中断工作副本已清理，日志和输入快照保留；重新执行会从头运行脚本。</p>${attempt?`<a href="/api/tasks/${encodeURIComponent(id)}/log/download?attempt_id=${encodeURIComponent(attempt)}">下载上次中断的完整日志 ↓</a>`:''}`;$('#dialog-body').prepend(panel);};
const recoveryBaseSettingsPage=settingsPage;
settingsPage=function(){const s=state.settings;return recoveryBaseSettingsPage().replace('<div class="full notice">',`<label>服务器重启后自动重新入队<select name="auto_requeue_interrupted"><option value="true" ${s.auto_requeue_interrupted!==false?'selected':''}>开启</option><option value="false" ${s.auto_requeue_interrupted===false?'selected':''}>关闭</option></select></label>${field('异常中断最多自动重试（次）','interruption_retry_limit',s.interruption_retry_limit??2,'number')}${field('异常重试等待时间（秒）','interruption_retry_delay',s.interruption_retry_delay??30,'number')}<div class="full notice">已证明原运行被服务器重启终止时，先保留日志、清理失败工作副本，再按原输入快照重新入队；暂时断联和无法确认停止的进程继续核验。手动暂停或中断不会被自动重跑覆盖。此次数与 OOM 重试分别计数。</div><div class="full notice">`);};
const recoveryBaseSubmitForm=submitForm;
submitForm=async function(form){if(form.id==='settings-form'){const data=Object.fromEntries(new FormData(form).entries());data.auto_requeue_interrupted=data.auto_requeue_interrupted==='true';await api('/api/settings',data);toast('设置已保存');await load();return;}return recoveryBaseSubmitForm(form);};
const recoverySettingsWithFields=settingsPage;
settingsPage=function(){return recoverySettingsWithFields()+`<section class="card" style="max-width:800px;margin-top:24px"><h2>失败运行副本</h2><p class="muted">清理已确认结束的失败工作副本，保留本机归档输出、控制日志和共享 objects 缓存。执行中和状态未知的运行不会删除。</p>${button('cleanup-failed-workspaces','清理失败运行副本')}</section>`;};
const recoveryBaseHandleAction=handleAction;
handleAction=async function(el){if(el.dataset.action==='cleanup-failed-workspaces'){await api('/api/tasks/failed-workspaces/cleanup',{});toast('失败副本清理已加入后台操作');await load();return;}return recoveryBaseHandleAction(el);};
setInterval(refreshRuntimePanel,1000);
const runtimeBaseGraph=scheduleGraph;
scheduleGraph=function(tasks){const map=new Map(tasks.map(t=>[t.id,t]));return runtimeBaseGraph(tasks).replace(/<article[\s\S]*?<\/article>/g,card=>{const id=card.match(/data-node-id="([^"]+)"/)?.[1],task=map.get(id);return task?.status==='staging'?card.replace(`${esc(labels[task.status])}</span></div><div class="flow-node-name">`,`${esc(runtimeLabel(task))}</span></div><div class="flow-node-name">`):card;});};
