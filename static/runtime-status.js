'use strict';
labels.staging='准备中';
function runtimeLabel(task){
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
  const p=task.runtime_progress||{},active=['staging','launching','syncing','sync_failed','canceling'].includes(task.status);
  panel.hidden=!active;const signature=JSON.stringify([task.status,task.reason,p]);if(panel.dataset.signature===signature)return;panel.dataset.signature=signature;
  const returning=['syncing','sync_failed'].includes(task.status),byteProgress=p.phase==='transferring'||p.phase==='verifying'||p.phase==='syncing'&&Number.isFinite(p.total);
  panel.innerHTML=`<b>${esc(runtimeLabel(task))}</b><p>${esc(task.reason||p.label||'后台正在处理')}</p>${byteProgress?`<div class="runtime-byte-progress"><span>${returning?'本次回传字节进度':'文件字节进度'}</span><b>${p.percent||0}%</b></div><progress max="100" value="${p.percent||0}"></progress><small>${returning?'已接收':'已发送'} ${size(p.bytes||0)} / ${size(p.total||0)}。${returning?'已有完整内容直接复用；接收完成后还需核验 hash 并保存日志。':'100% 后仍需完成核验、资源复查和启动确认。'}</small>`:''}${task.status==='syncing'&&task.sync_last_error?`<small>上次回传错误：${esc(task.sync_last_error)}；当前正在恢复。</small>`:''}<small>更新于 ${fmt(p.updated_at)}</small>`;
  const live=$('#experiment-live-status');if(live)live.innerHTML=badge(task.status,runtimeLabel(task))+'<span>'+esc(task.reason||'')+'</span>';
}
const runtimeBaseDetail=taskDetail;
taskDetail=async function(id){await runtimeBaseDetail(id);refreshRuntimePanel();};
setInterval(refreshRuntimePanel,1000);
const runtimeBaseGraph=scheduleGraph;
scheduleGraph=function(tasks){const map=new Map(tasks.map(t=>[t.id,t]));return runtimeBaseGraph(tasks).replace(/<article[\s\S]*?<\/article>/g,card=>{const id=card.match(/data-node-id="([^"]+)"/)?.[1],task=map.get(id);return task?.status==='staging'?card.replace(`${esc(labels[task.status])}</span></div><div class="flow-node-name">`,`${esc(runtimeLabel(task))}</span></div><div class="flow-node-name">`):card;});};
