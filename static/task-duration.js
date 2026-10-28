'use strict';
function formatTaskDuration(seconds){
  seconds=Math.max(0,Math.floor(seconds));
  const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60),secs=seconds%60;
  return [days?days+'天':'',hours||days?hours+'小时':'',minutes||hours||days?minutes+'分':'',secs+'秒'].filter(Boolean).join(' ');
}
function taskDuration(task,now=Date.now()/1000){
  const start=task.started_at,pending=['draft','queued','waiting','blocked','paused','staging'].includes(task.status);
  if(!Number.isFinite(start)||start<=0||pending&&!task.attempt_id)return {text:'未开始',title:'本轮实验尚未开始执行'};
  const end=[task.remote_finished_at,task.finished_at,task.synced_at].find(v=>Number.isFinite(v)&&v>0);
  if(!end&&!['running','launching','canceling','suspended'].includes(task.status))return {text:'时长待核实',title:'尚无可信的远程结束时间，不继续累计未知状态的运行时长'};
  const seconds=Math.max(0,(end||now)-start),live=!end;
  const prefix=live?(task.status==='suspended'?'已历时':'已运行'):'耗时';
  return {seconds,live,text:prefix+' '+formatTaskDuration(seconds),title:'本轮从启动到远程结束的经过时间（含暂停，不含回传等待）；重新执行单独计时'};
}
function taskDurationMarkup(task){
  const value=taskDuration(task);
  return `<span class="task-duration" data-task-duration="${esc(task.id)}" title="${esc(value.title)}">${esc(value.text)}</span>`;
}
if(typeof document!=='undefined')setInterval(()=>{
  if(document.hidden||typeof state==='undefined')return;
  const tasks=new Map((state.tasks||[]).map(t=>[t.id,t]));
  document.querySelectorAll('[data-task-duration]').forEach(el=>{const task=tasks.get(el.dataset.taskDuration);if(!task)return;const value=taskDuration(task);if(el.textContent!==value.text)el.textContent=value.text;if(el.title!==value.title)el.title=value.title;});
},1000);
if(typeof module!=='undefined')module.exports={taskDuration,formatTaskDuration};
