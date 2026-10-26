'use strict';
function benchmarkAvailability(server,gpu,jobs=[],tasks=[],servers=[]){
  if(!server.connected)return {disabled:true,label:'等待服务器连接',reason:'服务器未连接，请先在服务器页面恢复连接。',status:'lost'};
  const key=gpu.uuid||String(gpu.index);
  const pending=jobs.find(job=>{
    if(!['queued','running'].includes(job.status)||!job.operation?.startsWith('benchmark:'))return false;
    try{const [id,index]=JSON.parse(job.operation.slice('benchmark:'.length));const target=servers.find(s=>s.id===id)?.snapshot?.gpus?.find(g=>g.index===index);return id===server.id&&index===gpu.index||!!gpu.uuid&&target?.uuid===gpu.uuid;}catch{return false;}
  });
  if(pending)return {disabled:true,label:pending.status==='queued'?'测试排队中':'正在测试',reason:pending.status==='queued'?'测试已提交，等待后台执行。':'正在执行性能测试，完成后自动显示结果。',status:pending.status};
  const active=['staging','launching','running','suspended','canceling','syncing','sync_failed','lost'];
  if(tasks.some(t=>active.includes(t.status)&&(t.server_id===server.id&&t.gpu_index===gpu.index||!!gpu.uuid&&t.gpu_uuid===gpu.uuid)))return {disabled:true,label:'实验保留中',reason:'此 GPU 被实验保留，需等待实验及结果回传结束。',status:'waiting'};
  if(!gpu.is_idle){
    const count=gpu.process_count??gpu.processes?.length??0;
    const details=[count?`${count} 个计算进程`:null,`GPU 利用率 ${gpu.utilization_gpu??'—'}%`,`显存占用 ${((gpu.memory_used_mb||0)/1024).toFixed(1)} GB`].filter(Boolean).join(' · ');
    return {disabled:true,label:'GPU 被占用',reason:details+'。空闲后可测试；利用率为 0% 时，已有计算进程也会阻止测试。',status:'waiting'};
  }
  return {disabled:false,label:'开始性能测试',reason:'GPU 当前空闲，可以开始测试。',status:'success'};
}
if(typeof module!=='undefined')module.exports={benchmarkAvailability};
