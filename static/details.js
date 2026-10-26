'use strict';
const detailBaseRender=render,detailBaseAction=handleAction,detailBaseSubmit=submitForm,detailBaseTaskDetail=taskDetail;
const detailBaseTransferCard=transferCard,detailBaseJobCard=uploadJobCard;
const transferStates={queued:'等待处理',uploading:'正在上传',verifying:'文件 hash 校验',verified:'核验完成',verification_pending:'已传输 · 待核验',success:'上传完成',skipped:'已跳过',failed:'失败',interrupted:'已中断'};
const filePresence={unchecked:'待核验',matching:'有 · hash 一致',missing:'无',mismatch:'有 · hash 不一致'};
const fileActions={pending:'尚未判定',verify:'仅核验（不传输）',skip:'跳过上传',upload:'上传新文件',replace:'重传并覆盖'};
colors.verification_pending='amber';labels.verification_pending='待核验';
let transferHistory={offset:0,status:'all',lastFetch:0,loading:false,data:null},transferReader=null,logReader=null,tailRequest=null;

async function detailGet(path,signal){const response=await fetch(path,{signal}),data=await response.json();if(!response.ok)throw Error(data.error||'读取失败');return data;}
function detailsTitle(html,kind,id){return html.replace('<h3>',`<h3><button class="transfer-title-button" data-action="transfer-detail" data-kind="${kind}" data-id="${esc(id)}">`).replace('</h3>','</button></h3>').replace('<div class="actions">',`<div class="actions">${button('transfer-detail','详细记录',`data-kind="${kind}" data-id="${esc(id)}"`,'small')}`);}
transferCard=function(upload){return detailsTitle(detailBaseTransferCard(upload),'upload',upload.id);};
uploadJobCard=function(job){return detailsTitle(detailBaseJobCard(job),'job',job.id);};
uploadsPage=function(){
  const h=transferHistory,data=h.data,entries=data?.entries||[...(state.uploads||[]).map(u=>({kind:'upload',value:u,time:Math.max(u.created_at,state.jobs?.find(j=>j.id===u.job_id)?.created_at||0)})),...uploadPageJobs().map(j=>({kind:'job',value:j,time:j.created_at}))].sort((a,b)=>b.time-a.time);
  const counts=data?.counts||{active:entries.filter(e=>transferActive.includes(e.value.status)||['running','queued'].includes(e.value.status)).length,success:entries.filter(e=>e.value.status==='success').length,stopped:entries.filter(e=>['failed','interrupted','canceled'].includes(e.value.status)).length};
  return pageHead('上传与进度','每次上传与同步都保留详细记录，最新的传输排在最上面。','<a href="#events">完整操作记录 →</a>')+`<div class="upload-summary"><span><b>${counts.active}</b> 正在进行</span><span><b>${counts.success}</b> 已完成</span><span><b>${counts.stopped}</b> 已中断 / 失败</span></div><div class="transfer-list-toolbar"><div class="tabs">${[['all','全部记录'],['active','进行中'],['success','已完成'],['stopped','中断与失败']].map(([v,label])=>button('transfer-filter',label,`data-value="${v}"`,h.status===v?'active':'')).join('')}</div><small>点击名称或「详细记录」查看每个文件 · 按最新发起时间排序</small></div>${h.error?`<div class="notice warning">${esc(h.error)}</div>`:''}${entries.length?`<div class="grid">${entries.map(e=>e.kind==='upload'?transferCard(e.value):uploadJobCard(e.value)).join('')}</div>`:empty(h.loading?'正在读取记录…':'暂无符合条件的上传','从项目上传代码或从数据集页面导入、同步数据。')}<div class="detail-pagination"><span>${data?.total_count?data.offset+1:0}–${data?Math.min(data.offset+data.entries.length,data.total_count):entries.length} / ${data?.total_count??entries.length}</span><div class="actions">${button('transfer-history-prev','上一页',`${!h.offset||h.loading?'disabled':''}`,'small')}${button('transfer-history-next','下一页',`${!data||data.offset+data.limit>=data.total_count||h.loading?'disabled':''}`,'small')}</div></div>`;
};
async function fetchTransferHistory(force=false){
  const h=transferHistory;if(h.loading||!force&&Date.now()-h.lastFetch<2500)return;h.loading=true;
  try{h.data=await detailGet(`/api/transfers?offset=${h.offset}&status=${h.status}`);h.error='';}catch(e){h.error=e.message;}
  finally{h.loading=false;h.lastFetch=Date.now();if(route()[0]==='uploads')render();}
}
function transferView(kind,id){
  if(transferReader?.kind!==kind||transferReader?.id!==id){transferReader?.controller?.abort();transferReader={kind,id,offset:0,receiptOffset:0,query:'',server:'',status:'',job:'',tab:'files',seq:0,lastFetch:0};}
  return transferReader;
}
function transferDetailSkeleton(view){return pageHead('传输详细记录','逐文件保存核验结果、实际操作、进度与时间。','<a href="#uploads">← 返回上传与进度</a>')+`<section class="card"><div id="transfer-reader-heading"></div><div id="transfer-reader-progress"></div><p class="muted">状态是本次同步前的 hash 核验结果；共享目录只上传一份，会标明实际执行服务器。完整写入并校验后才替换目标文件。</p><div id="transfer-attempts"></div></section><div class="tabs detail-tabs">${button('transfer-detail-tab','本机与服务器文件','data-tab="files"','active')}${view.kind==='upload'?button('transfer-detail-tab','文件接收进度','data-tab="receipts"'):''}</div><form id="transfer-filter-form" class="detail-filters"><label>搜索文件<input name="query" value="" placeholder="文件名或目录名"></label><label>目标位置<select name="server"><option value="">全部位置</option><option value="local">本机保存目录</option>${state.servers.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')}</select></label><label>处理结果<select name="status"><option value="">全部状态</option>${Object.entries(transferStates).map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></label><button type="submit" class="small">搜索</button></form><div id="transfer-reader-summary"></div><div id="transfer-reader-files" class="card">正在读取逐文件记录…</div><div id="transfer-reader-pagination" class="detail-pagination"></div>`;}
function filePercent(file){return file.total?Math.min(100,file.bytes/file.total*100):['skipped','success'].includes(file.status)?100:0;}
function fileRow(file){const percent=filePercent(file),local=file.server_id==='local';return `<tr><td><span class="detail-file-path">${esc(file.path)}</span><small>${esc(file.server_name)}${file.shared?' · 共享，由 '+esc(file.source_server_name)+' 执行':''}</small></td><td><span class="file-presence ${file.initial_state}">${filePresence[file.initial_state]||'未知'}</span></td><td>${local?({skip:'跳过保存',upload:'保存新文件',replace:'覆盖并保存历史副本'}[file.action]||fileActions[file.action]):fileActions[file.action]}</td><td><div class="file-progress-label"><span>${transferStates[file.status]||file.status}</span><b>${percent.toFixed(1)}%</b></div><div class="bar"><i style="width:${percent}%"></i></div><small>${size(file.bytes)} / ${size(file.total)}${file.verified_at?' · 已最终核验':''}</small>${file.error?`<small class="error-text">${esc(file.error)}</small>`:''}</td><td>${button('transfer-file-detail','详情',`data-server="${esc(file.server_id)}" data-path="${esc(file.path)}"`,'small')}</td></tr>`;}
function receiptRows(view,data){const client=clientTransfers.get(view.id);return data.receipts.map(file=>{let received=file.received||0;if(data.upload.local_source&&data.job?.progress?.phase==='copying'&&data.job.progress.path===file.path)received=Math.min(file.size,Math.max(received,data.job.progress.file_bytes||0));if(client?.progress?.path===file.path||client?.progress?.path?.endsWith('/'+file.path))received=Math.min(file.size,received+Math.max(0,client.progress.bytes-data.upload.received_bytes));const percent=file.size?received/file.size*100:100;return `<tr><td><span class="detail-file-path">${esc(file.path)}</span></td><td>${size(file.size)}</td><td><div class="file-progress-label"><span>${received===file.size?'已完整接收':data.upload.local_source?'后台复制':'传到本机'}</span><b>${percent.toFixed(1)}%</b></div><div class="bar"><i style="width:${percent}%"></i></div><small>${size(received)} / ${size(file.size)}</small></td></tr>`;}).join('');}
function updateTransferReader(view){
  if(route()[0]!=='transfer'||transferReader!==view)return;const d=view.data;if(!d){$('#transfer-reader-files').innerHTML=view.error?`<div class="notice warning">${esc(view.error)}</div>`:'正在读取逐文件记录…';return;}
  const value=view.job?d.job:d.upload?uploadView(d.upload):d.job;
  $('#transfer-reader-heading').innerHTML=`<div class="card-head"><div><h3>${esc(value?.label||'文件同步')}</h3><small class="muted">发起于 ${fmt(value?.created_at)}${value?.finished_at?' · 结束于 '+fmt(value.finished_at):''}</small></div>${badge(value?.status,({canceled:'已中断',interrupted:'已中断',running:'处理中'}[value?.status])||phases[value?.status])}</div>`;
  $('#transfer-reader-progress').innerHTML=transferProgress(value||{})+`<div class="actions">${value?interruptButton(value,d.upload&&!view.job?'upload':'job'):''}</div>`;
  if(d.jobs?.length){const target=$('#transfer-attempts'),fingerprint=JSON.stringify(d.jobs.map(j=>[j.id,j.status]));if(target.dataset.fingerprint!==fingerprint){target.innerHTML=`<label>后台处理批次<select id="transfer-attempt-select"><option value="">最新处理</option>${d.jobs.map(j=>`<option value="${esc(j.id)}" ${view.job===j.id?'selected':''}>${fmt(j.created_at)} · ${labels[j.status]||j.status}</option>`).join('')}</select></label>`;target.dataset.fingerprint=fingerprint;}}
  const receipts=view.tab==='receipts',total=receipts?d.receipt_matched:d.matched_count,count=receipts?d.receipts?.length||0:d.files.length,offset=receipts?view.receiptOffset:view.offset;
  $('#transfer-reader-summary').innerHTML=`<div class="detail-counts"><span>${receipts?(d.receipt_total||0)+' 个原始上传文件':d.total_count+' 条文件与位置记录'}</span>${!receipts?d.summary.map(s=>`<span>${esc(s.server_id==='local'?'本机':serverBy(s.server_id)?.name||s.server_id)} · ${transferStates[s.status]||s.status} ${s.count}</span>`).join(''):''}${view.loading?'<span>正在更新…</span>':''}</div>`;
  $('#transfer-reader-files').innerHTML=count?`<div class="detail-table"><table><thead><tr>${receipts?'<th>原始文件</th><th>大小</th><th>浏览器接收进度</th>':'<th>文件 / 目标位置</th><th>同步前状态</th><th>实际操作</th><th>单文件进度</th><th>记录</th>'}</tr></thead><tbody>${receipts?receiptRows(view,d):d.files.map(fileRow).join('')}</tbody></table></div>`:empty(receipts?'没有匹配的上传文件':view.query||view.server||view.status?'没有匹配记录':'暂未产生逐文件记录',receipts?'调整搜索条件。':d.job&&['queued','running'].includes(d.job.status)?'正在后台扫描 / 计算 hash，文件核验后会自动出现。':d.upload&&!d.job?'文件正在传到本机，可以切换到「浏览器接收进度」。':'更新前的历史记录不包含逐文件决策，不会推测当时的 hash 或操作。新的导入和同步会记录这些信息。');
  $('#transfer-reader-pagination').innerHTML=`<span>${total?offset+1:0}–${Math.min(offset+count,total||0)} / ${total||0}</span><div class="actions">${button('transfer-detail-prev','上一页',`${!offset||view.loading?'disabled':''}`,'small')}${button('transfer-detail-next','下一页',`${offset+100>=total||view.loading?'disabled':''}`,'small')}</div>`;
  $('#transfer-filter-form [name=server]').closest('label').hidden=receipts;$('#transfer-filter-form [name=status]').closest('label').hidden=receipts;
  if(dialogKind==='transfer-file'&&view.selectedFile){const file=d.files.find(f=>f.server_id===view.selectedFile.server&&f.path===view.selectedFile.path);if(file)$('#dialog-body').innerHTML=transferFileBody(file);}
}
async function fetchTransferReader(view,force=false){
  if(view.loading&&!force||!force&&Date.now()-view.lastFetch<2000)return;view.controller?.abort();view.controller=new AbortController();const seq=++view.seq;view.loading=true;
  const params=new URLSearchParams({offset:view.offset,receipt_offset:view.receiptOffset,limit:100,query:view.query,server:view.server,status:view.status,job_id:view.job});
  try{const data=await detailGet(`/api/${view.kind==='upload'?'uploads/'+view.id+'/details':'jobs/'+view.id+'/transfer-details'}?${params}`,view.controller.signal);if(seq===view.seq){view.data=data;view.error='';}}
  catch(e){if(seq===view.seq&&e.name!=='AbortError')view.error=e.message;}
  finally{if(seq===view.seq){view.loading=false;view.lastFetch=Date.now();updateTransferReader(view);}}
}
function transferFileBody(f){return `<p class="detail-full-path">${esc(f.path)}</p><p>${esc(f.server_name)}${f.shared?' · 共享，由 '+esc(f.source_server_name)+' 执行':''}</p><div class="info-grid"><div><small>同步前状态</small>${filePresence[f.initial_state]}</div><div><small>实际操作</small>${fileActions[f.action]}</div><div><small>单文件进度</small>${transferStates[f.status]} · ${filePercent(f).toFixed(1)}%<br>${size(f.bytes)} / ${size(f.total)}</div><div><small>最终核验</small>${f.verified_at?filePresence[f.current_state]+' · '+fmt(f.verified_at):'等待最终核验'}</div></div>${f.verification_method==='shared_sha256'?`<p class="muted">共享目录与文件视图已确认；最终 SHA-256 来自 ${esc(f.source_server_name)} 的内容核验。</p>`:''}${f.verification_error?`<p class="error-text">${esc(f.verification_error)}</p>`:''}<div class="hash-record"><span>预期 SHA-256</span><code>${esc(f.expected_sha256)}</code><span>同步前 SHA-256</span><code>${esc(f.previous_sha256||'不存在 / 未核验')}</code><span>最终 SHA-256</span><code>${esc(f.current_sha256||'尚未核验')}</code></div>${f.error?`<p class="error-text">${esc(f.error)}</p>`:''}<div class="section-title"><h3>文件操作时间线</h3></div>${(f.events||[]).map(e=>`<div class="file-audit-event"><time>${fmt(e.time)}</time><span>${e.status==='planned'?'判定操作 · '+fileActions[e.message]:transferStates[e.status]||e.status}</span></div>`).join('')}`;}

logLoad=async function(id){
  if(!$('#task-log')||selectedTask!==id)return;if(tailRequest?.id===id)return;tailRequest?.controller.abort();const request={id,controller:new AbortController()};tailRequest=request;
  const timer=setTimeout(()=>request.controller.abort(),12000);
  try{const data=await detailGet(`/api/tasks/${id}/log`,request.controller.signal);if(tailRequest!==request||selectedTask!==id||!$('#task-log'))return;if(data.pending){if($('#task-log-meta'))$('#task-log-meta').textContent='正在读取服务器日志，稍后自动更新…';return;}const pre=$('#task-log'),bottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-35,old=pre.scrollTop;if(pre.textContent!==(data.text||'尚无日志')){pre.textContent=data.text||'尚无日志';pre.scrollTop=bottom?pre.scrollHeight:old;}if($('#task-log-meta'))$('#task-log-meta').textContent=`最近 ${data.lines||0} 行 · ${data.source==='server'?'服务器':'本机'} · 自动更新于 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}${data.truncated_line?' · 超长行已截断，可查看完整日志':''}${data.warning?' · '+data.warning:''}`;}
  catch(e){if(tailRequest===request&&selectedTask===id&&$('#task-log'))$('#task-log').textContent=e.name==='AbortError'?'日志读取超时，将在下一轮自动重试。':e.message;}
  finally{clearTimeout(timer);if(tailRequest===request)tailRequest=null;}
};
taskDetail=async function(id){const result=detailBaseTaskDetail(id);if($('#task-log'))$('#task-log').insertAdjacentHTML('beforebegin',`<div class="log-toolbar"><small id="task-log-meta">最近 200 行 · 每 5 秒自动更新</small>${button('full-task-log','完整日志详情',`data-id="${esc(id)}"`,'small')}</div>`);await result;};
function logView(id){if(logReader?.id!==id){logReader?.controller?.abort();logReader={id,mode:'full',offset:0,previous:[],attempt:'',seq:0,lastFetch:0};}return logReader;}
function logPageSkeleton(view){const t=state.tasks.find(t=>t.id===view.id),attempts=[...(t?.attempt_id?[{attempt_id:t.attempt_id,exit_code:t.exit_code}]:[]),...(t?.previous_attempts||[]).slice().reverse()];return pageHead(t?'#'+t.seq+' '+t.name+' · 日志':'实验日志','stdout / stderr；默认预览最近 200 行，完整日志分页读取。',t?`<a href="#project/${t.project_id}">← 返回项目</a>`:'')+`<section class="card"><div class="log-reader-controls"><label>运行记录<select id="log-attempt-select"><option value="">最新运行（自动跟随）</option>${attempts.map(a=>`<option value="${esc(a.attempt_id)}">${esc(a.attempt_id.slice(0,8))} · 退出码 ${a.exit_code??'—'}</option>`).join('')}</select></label><div class="tabs">${button('log-mode','最新 200 行','data-mode="tail"',view.mode==='tail'?'active':'')}${button('log-mode','完整日志','data-mode="full"',view.mode==='full'?'active':'')}</div><div class="actions">${button('log-reader-refresh','刷新','','small')}<a id="log-download" class="download-button" href="/api/tasks/${view.id}/log/download">下载完整日志</a></div></div><p id="log-reader-meta" class="muted">正在读取日志…</p><div id="log-task-status" class="log-task-status"></div><pre id="full-task-log" class="log log-reader">正在读取日志…</pre><div id="log-reader-pagination" class="detail-pagination"></div><p class="muted">最近 200 行自动轮询。完整日志每次最多读取 128 KB，分页浏览和下载可覆盖整个文件；不会一次把大日志塞进网页。</p></section>`;}
function updateLogReader(view){
  if(route()[0]!=='logs'||logReader!==view)return;const pre=$('#full-task-log'),data=view.data;
  const task=state.tasks.find(t=>t.id===view.id),historical=task?.previous_attempts?.find(a=>a.attempt_id===(view.attempt||view.data?.attempt_id)&&a.attempt_id!==task.attempt_id),run=historical||task;if(run&&$('#log-task-status'))$('#log-task-status').innerHTML=`${historical?'<span class="tag">历史运行</span>':badge(task.status)}<span>退出码 ${run.exit_code??'—'}</span><span>${esc(serverBy(run.server_id)?.name||'尚未分配服务器')}</span>${!historical&&task.reason?`<span class="error-text">${esc(task.reason)}</span>`:''}`;
  if(view.error){pre.textContent=view.error;return;}if(view.pending){$('#log-reader-meta').textContent='服务器日志正在读取，稍后自动更新…';document.querySelectorAll('[data-action=log-prev],[data-action=log-next]').forEach(b=>b.disabled=true);return;}if(!data)return;
  const bottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-35,old=pre.scrollTop,changedPage=pre.dataset.offset!==String(data.offset??'tail')||pre.dataset.attempt!==String(data.attempt_id);
  if(pre.textContent!==(data.text||'尚无日志'))pre.textContent=data.text||'尚无日志';pre.scrollTop=changedPage?view.mode==='tail'?pre.scrollHeight:0:bottom&&view.mode==='tail'?pre.scrollHeight:old;pre.dataset.offset=String(data.offset??'tail');pre.dataset.attempt=String(data.attempt_id);
  $('#log-reader-meta').textContent=`${view.mode==='tail'?'最近 '+(data.lines||0)+' 行':'字节 '+(data.offset||0)+'–'+(data.next_offset||0)} / ${size(data.size||0)} · ${data.source==='server'?'服务器':'本机'} · ${fmt(data.updated_at)}${data.truncated_line?' · 超长行已截断':''}${data.warning?' · '+data.warning:''}`;
  $('#log-reader-pagination').innerHTML=view.mode==='full'?`<span>${data.has_more?'后面还有日志':'已到日志末尾'}</span><div class="actions">${button('log-prev','上一页',`${!view.previous.length||view.loading?'disabled':''}`,'small')}${button('log-next','下一页',`${!data.has_more||view.loading?'disabled':''}`,'small')}</div>`:'<span>每 3 秒自动读取末尾 200 行</span>';
  $('#log-download').href=`/api/tasks/${view.id}/log/download${view.attempt?'?attempt_id='+encodeURIComponent(view.attempt):''}`;
}
async function fetchLogReader(view,force=false){
  if(view.loading&&!force||!force&&view.data&&(view.mode==='full'&&!view.pending||Date.now()-view.lastFetch<2500))return;
  if(!view.attempt&&view.data){const task=state.tasks.find(t=>t.id===view.id),latest=task?.attempt_id||task?.previous_attempts?.slice(-1)[0]?.attempt_id;if(latest&&view.data.attempt_id!==latest){view.offset=0;view.previous=[];}}
  view.controller?.abort();const controller=new AbortController();view.controller=controller;const seq=++view.seq;view.loading=true;const timer=setTimeout(()=>controller.abort(),12000);
  try{const data=await detailGet(`/api/tasks/${view.id}/log?mode=${view.mode}&offset=${view.offset}${view.attempt?'&attempt_id='+encodeURIComponent(view.attempt):''}`,controller.signal);if(seq===view.seq){view.pending=!!data.pending;view.error='';if(!data.pending){view.data=data;if(view.mode==='full'&&data.offset!==undefined){if(data.offset!==view.offset)view.previous=[];view.offset=data.offset;}}}}
  catch(e){if(seq===view.seq)view.error=e.name==='AbortError'?'读取超时，请重试；最近 200 行模式会自动再次读取。':e.message;}
  finally{clearTimeout(timer);if(seq===view.seq){view.loading=false;view.lastFetch=Date.now();updateLogReader(view);}}
}
render=function(){
  const [page,kind,id]=route();
  if(page==='transfer'||page==='logs'){
    projectFileObserver?.disconnect();document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===(page==='transfer'?'uploads':'tasks')));$('#breadcrumb').textContent=page==='transfer'?'传输详细记录':'实验日志';$('#connection-state').textContent='已更新 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});$('#scheduler-button').textContent=state.settings.paused?'恢复调度':'暂停调度';
    const key=route().join('/'),main=$('#main');if(main.dataset.reader!==key){main.dataset.reader=key;if(page==='logs')logView(kind).data=null;main.innerHTML=page==='transfer'?transferDetailSkeleton(transferView(kind,id)):logPageSkeleton(logView(kind));}
    if(page==='transfer'){const view=transferView(kind,id);updateTransferReader(view);fetchTransferReader(view);}else{const view=logView(kind);updateLogReader(view);fetchLogReader(view);}renderTransferDock();
  }else{delete $('#main').dataset.reader;detailBaseRender();if(page==='uploads')fetchTransferHistory();
    if(page==='events')document.querySelectorAll('[data-job-id]').forEach(card=>{const job=state.jobs?.find(j=>j.id===card.dataset.jobId);if(job?.operation?.startsWith('upload_dataset:')||job?.operation?.startsWith('verify_dataset:')||job?.operation?.startsWith('process:'))$('.actions',card)?.insertAdjacentHTML('beforeend',button('transfer-detail','详细记录',`data-kind="job" data-id="${esc(job.id)}"`,'small'));});
  }
  styleControls();
};
handleAction=async function(el){
  const a=el.dataset.action,v=transferReader,l=logReader;
  if(a==='dataset-verify'){await api(`/api/datasets/${el.dataset.id}/verify`,{});toast('仅核验已开始，可在操作记录查看进度或中断');await load();return;}
  if(a==='transfer-detail'){if($('#dialog').open)$('#dialog').close();location.hash=`transfer/${el.dataset.kind}/${el.dataset.id}`;return;}
  if(a==='transfer-filter'||a==='transfer-history-prev'||a==='transfer-history-next'){if(transferHistory.loading)return;if(a==='transfer-filter'){transferHistory.status=el.dataset.value;transferHistory.offset=0;}else transferHistory.offset=Math.max(0,transferHistory.offset+(a.endsWith('next')?30:-30));transferHistory.data=null;await fetchTransferHistory(true);return;}
  if(a==='transfer-detail-tab'){v.tab=el.dataset.tab;document.querySelectorAll('.detail-tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===v.tab));updateTransferReader(v);return;}
  if(a==='transfer-detail-prev'||a==='transfer-detail-next'){const field=v.tab==='receipts'?'receiptOffset':'offset';v[field]=Math.max(0,v[field]+(a.endsWith('next')?100:-100));await fetchTransferReader(v,true);return;}
  if(a==='transfer-file-detail'){const f=v.data.files.find(f=>f.server_id===el.dataset.server&&f.path===el.dataset.path);if(f){v.selectedFile={server:f.server_id,path:f.path};modal('文件核验与操作记录',transferFileBody(f),'transfer-file');}return;}
  if(a==='full-task-log'){tailRequest?.controller.abort();const view=logView(el.dataset.id);Object.assign(view,{mode:'full',offset:0,previous:[],attempt:'',pending:false});if($('#dialog').open)$('#dialog').close();location.hash='logs/'+el.dataset.id;return;}
  if(['log-mode','log-reader-refresh','log-prev','log-next'].includes(a)){
    if(a==='log-mode'){l.mode=el.dataset.mode;l.offset=0;l.previous=[];document.querySelectorAll('.log-reader-controls .tabs button').forEach(b=>b.classList.toggle('active',b.dataset.mode===l.mode));}
    if(a==='log-next'){l.previous.push(l.offset);l.offset=l.data.next_offset;}
    if(a==='log-prev')l.offset=l.previous.pop()||0;
    await fetchLogReader(l,true);return;
  }
  const result=await detailBaseAction(el);if(route()[0]==='transfer'&&['job-interrupt','upload-interrupt','upload-retry','dataset-retry'].includes(a))await fetchTransferReader(transferReader,true);return result;
};
submitForm=async function(form){if(form.id!=='transfer-filter-form')return detailBaseSubmit(form);const fd=new FormData(form);Object.assign(transferReader,{query:fd.get('query'),server:fd.get('server'),status:fd.get('status'),offset:0,receiptOffset:0});await fetchTransferReader(transferReader,true);};
document.addEventListener('change',e=>{
  if(e.target.id==='transfer-attempt-select'){transferReader.job=e.target.value;transferReader.offset=0;fetchTransferReader(transferReader,true);}
  if(e.target.id==='log-attempt-select'){logReader.attempt=e.target.value;logReader.offset=0;logReader.previous=[];fetchLogReader(logReader,true);}
  if(e.target.type==='file'||e.target.name==='mode')styleControls();
  if(e.target.tagName==='SELECT')e.target.title=e.target.value||e.target.selectedOptions[0]?.textContent||'';
});
function styleControls(){
  document.querySelectorAll('select').forEach(s=>{s.title=s.dataset.environmentServer?s.value||s.selectedOptions[0]?.textContent||'':s.selectedOptions[0]?.textContent||'';});
  document.querySelectorAll('input[type=file]').forEach(input=>{
    if(input.classList.contains('note-file-input'))return;
    let wrapper=input.closest('.file-picker');if(!wrapper){wrapper=document.createElement('div');wrapper.className='file-picker';input.before(wrapper);wrapper.innerHTML='<span class="file-picker-icon">↥</span><span class="file-picker-copy"><b></b><small></small></span><span class="file-picker-button">浏览文件</span>';wrapper.append(input);}
    const files=input.files,title=files.length===1?files[0].name:files.length?(files[0].webkitRelativePath.split('/')[0]||'已选择')+' · '+files.length+' 个文件':input.hasAttribute('webkitdirectory')?'选择整个文件夹':input.accept==='.zip'?'选择 ZIP 压缩包':'选择单个或多个文件';
    const desc=files.length?(files.length>500?files.length+' 个文件 · 总大小提交后后台计算':size(Array.from(files).reduce((sum,f)=>sum+f.size,0))+' · 点击可重新选择'):'文件将在后台导入，可查看进度并中断';
    const b=$('.file-picker-copy b',wrapper),small=$('.file-picker-copy small',wrapper);if(b.textContent!==title)b.textContent=title;if(small.textContent!==desc)small.textContent=desc;wrapper.title=title;
  });
  document.querySelectorAll('.environment-row small').forEach(s=>s.title=s.textContent);
}
let styleControlsQueued=false;
new MutationObserver(()=>{if(styleControlsQueued)return;styleControlsQueued=true;requestAnimationFrame(()=>{styleControlsQueued=false;styleControls();});}).observe(document.body,{childList:true,subtree:true});
$('#dialog')?.addEventListener('close',()=>tailRequest?.controller.abort());
if(document.body.dataset.authorized==='true')setInterval(()=>{if(document.hidden)return;if(route()[0]==='transfer')fetchTransferReader(transferReader);if(route()[0]==='logs'&&(logReader?.mode==='tail'||logReader?.pending))fetchLogReader(logReader);},3000);
