'use strict';
const datasetBaseRender=render,datasetBaseAction=handleAction,datasetBaseSubmit=submitForm;
let datasetDetail=null,datasetSearchTimer;
const datasetServerLabels={success:'已校验',uploading:'上传中',failed:'待重试',verification_pending:'待核验',interrupted:'已中断'};
function datasetFingerprint(id){return JSON.stringify([state.datasets?.find(d=>d.id===id),state.storage?.find(s=>s.scope==='data'),state.local_storage?.root,state.servers?.map(s=>[s.id,s.name,s.data_root]),state.tasks?.map(t=>[t.id,t.name,t.status,t.datasets])]);}
function datasetSummary(view){
  const {dataset:d,servers,sharing,tasks}=view,ready=!d.import_status||d.import_status==='ready';
  const u=state.uploads?.find(v=>v.id===d.upload_id),busy=transferActive.includes(u?.status)||(state.jobs||[]).some(j=>['upload_dataset:','verify_dataset:','delete_dataset:'].some(op=>j.operation===op+JSON.stringify([d.id]))&&['queued','running'].includes(j.status));
  return `${typeof datasetNotePanel==='function'?datasetNotePanel(d,true):''}<div class="dataset-metrics"><div><small>清单文件</small><strong>${d.file_count.toLocaleString()}</strong></div><div><small>文件总大小</small><strong>${size(d.total_bytes)}</strong></div><div><small>本机导入</small><strong>${ready?'已保存':esc(phases[d.import_status]||'未完成')}</strong></div><div><small>服务器已校验</small><strong>${servers.filter(s=>s.replica.status==='success').length} / ${servers.length}</strong></div></div>
  <div class="card dataset-location"><div class="card-head"><h3>本机保存位置</h3><small class="muted">创建于 ${fmt(d.created_at)}</small></div><code class="break">${esc(d.local_path)}</code><p class="muted">文件列表使用导入时保存的 SHA-256 清单。手动修改或移除本机文件后，清单不会自动更新。</p>${!ready?'<a href="#uploads">导入未完成，查看上传记录 →</a>':''}</div>
  <div class="section-title"><h2>服务器存储</h2><div class="actions"><a href="#uploads">同步进度与中断 →</a>${button('dataset-verify','仅核验',`data-id="${esc(d.id)}" ${busy?'disabled':''}`,'small')}${button('dataset-retry',busy?'同步进行中':'同步到所有服务器',`data-id="${esc(d.id)}" ${!ready||!d.file_count||busy?'disabled':''}`,'small')}</div></div>
  <p class="muted">共享目录探测：${sharing.checked_at?fmt(sharing.checked_at)+' · 使用已有探测结果':'尚无有效结果，可在服务器页面探测'}。打开详情不会触发探测或同步。</p>
  ${servers.length?`<div class="grid dataset-servers">${servers.map(s=>{const r=s.replica,shared=s.shared_with.map(id=>servers.find(v=>v.id===id)?.name||id);return `<article class="card"><div class="card-head"><h3>${esc(s.name)}</h3>${badge(r.status||'queued',datasetServerLabels[r.status]||'待上传')}</div><code class="break">${esc(s.dataset_path)}</code><p class="dataset-sharing">${shared.length?'共享存储 · '+esc(shared.join('、')):sharing.states?.[s.id]?.status==='verified'?'独立存储 · 已探测':'共享关系未核验'}</p><small class="muted">最近文件核验：${fmt(r.verified_at)}</small>${r.verification_method==='shared_sha256'?`<p class="muted">共享核验：内容 SHA-256 由 ${esc(servers.find(v=>v.id===r.shared_with)?.name||r.shared_with)} 计算，本机已确认同一数据目录和文件视图。</p>`:''}${r.error?`<p class="error-text">${esc(r.error)}</p>`:''}</article>`;}).join('')}</div>`:empty('尚未配置服务器','添加服务器后可将数据集同步到配置的数据目录。')}
  ${tasks.length?`<div class="section-title"><h2>使用此数据集的实验</h2><span>${tasks.length} 个实验</span></div><div class="card dataset-usage">${tasks.slice(0,20).map(t=>`<div class="file-row"><div><a href="#project/${esc(t.project_id)}">${esc(projectBy(t.project_id)?.name||'项目')}</a><span> / ${esc(t.name)}</span></div>${badge(t.status)}${button('task-detail','查看实验',`data-id="${esc(t.id)}"`,'small')}</div>`).join('')}${tasks.length>20?'<a href="#tasks">查看全部实验 →</a>':''}</div>`:''}`;
}
function datasetFileRows(){
  const view=datasetDetail;if(!view)return;const target=$('#dataset-files');if(!target)return;
  if(view.fileError){target.innerHTML=`<p class="error-text">${esc(view.fileError)}</p>${button('dataset-files-retry','重新加载','','small')}`;return;}
  if(!view.files){target.innerHTML='<div class="loading">正在读取文件清单…</div>';return;}
  const f=view.files;
  target.innerHTML=`<div class="dataset-file-count"><span>${view.query?'搜索结果':'文件清单'} ${f.matched_count.toLocaleString()} 个 · 共 ${f.total_count.toLocaleString()} 个文件</span>${view.filesLoading?'<span>正在加载…</span>':''}</div>${f.files.length?`<div class="dataset-file-table"><table><thead><tr><th>文件路径</th><th>大小</th><th>SHA-256（导入时）</th><th></th></tr></thead><tbody>${f.files.map((v,i)=>`<tr><td><button class="dataset-file-name" data-action="dataset-file-info" data-index="${i}">${esc(v.path)}</button></td><td>${size(v.size)}</td><td><code title="${esc(v.sha256)}">${esc(v.sha256.slice(0,16))}…</code></td><td><a href="/api/datasets/${encodeURIComponent(view.id)}/file?path=${encodeURIComponent(v.path)}" download>下载</a></td></tr>`).join('')}</tbody></table></div>`:empty(view.query?'没有匹配的文件':'暂无文件清单',view.query?'尝试其他文件名或目录名。':'导入完成后，文件清单将在这里显示。')}
  <div class="dataset-pagination"><small>${f.matched_count?f.offset+1:0}–${Math.min(f.offset+f.files.length,f.matched_count)} / ${f.matched_count}</small><div class="actions">${button('dataset-prev','上一页',`${view.offset===0||view.filesLoading?'disabled':''}`,'small')}${button('dataset-next','下一页',`${f.offset+f.limit>=f.matched_count||view.filesLoading?'disabled':''}`,'small')}</div></div>`;
}
async function datasetLoadFiles(){
  const view=datasetDetail,seq=(view.fileSeq||0)+1;view.fileSeq=seq;view.filesLoading=true;view.fileError='';datasetFileRows();
  try{const files=await api(`/api/datasets/${encodeURIComponent(view.id)}/files?offset=${view.offset}&limit=100&query=${encodeURIComponent(view.query)}`);if(datasetDetail!==view||view.fileSeq!==seq)return;view.files=files;}
  catch(e){if(datasetDetail!==view||view.fileSeq!==seq)return;view.fileError=e.message;}
  if(datasetDetail!==view||view.fileSeq!==seq)return;view.filesLoading=false;if(route()[0]==='dataset'&&route()[1]===view.id)datasetFileRows();
}
async function datasetLoadSummary(){
  const view=datasetDetail;if(view.loading)return;view.loading=true;view.fingerprint=datasetFingerprint(view.id);
  try{const value=await api(`/api/datasets/${encodeURIComponent(view.id)}`);if(datasetDetail!==view)return;const changed=view.data?.dataset.file_count!==value.dataset.file_count||view.data?.dataset.import_status!==value.dataset.import_status;view.data=value;view.error='';if(changed)datasetLoadFiles();}
  catch(e){if(datasetDetail!==view)return;view.error=e.message;}
  if(datasetDetail!==view)return;view.loading=false;if(route()[0]==='dataset'&&route()[1]===view.id)render();
}
render=function(){
  const [page,id]=route();if(page!=='dataset'){datasetBaseRender();return;}
  if(!datasetDetail||datasetDetail.id!==id){clearTimeout(datasetSearchTimer);datasetDetail={id,query:'',offset:0};}
  const v=datasetDetail;
  document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav==='datasets'));
  $('#breadcrumb').textContent='数据集 / '+(v.data?.dataset.name||'详情');$('#scheduler-button').textContent=state.settings.paused?'恢复调度':'暂停调度';$('#connection-state').textContent='已更新 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  if($('#main').dataset.datasetId!==id||!$('#dataset-summary')){
    $('#main').innerHTML=`<a class="dataset-back" href="#datasets">← 返回数据集</a><div id="dataset-heading"></div><div id="dataset-summary"></div><section class="card dataset-inventory"><div class="card-head"><h2>本机文件清单</h2><small class="muted">每页 100 个文件</small></div><form id="dataset-search"><label>搜索文件名或目录<input name="query" value="${esc(v.query)}" placeholder="例如 train/ 或 labels.csv" autocomplete="off"></label><button type="submit">搜索</button></form><div id="dataset-files"></div></section>`;
    $('#main').dataset.datasetId=id;datasetFileRows();
  }
  $('#dataset-heading').innerHTML=pageHead(v.data?.dataset.name||'数据集详情','保存位置、服务器副本与文件清单',button('dataset-detail-refresh','刷新详情','','small'));
  $('#dataset-summary').innerHTML=v.error?`<div class="notice warning">${esc(v.error)}</div>`:v.data?datasetSummary(v.data):'<div class="loading">正在读取数据集详情…</div>';
  renderTransferDock();if(!v.loading&&v.fingerprint!==datasetFingerprint(id))datasetLoadSummary();
};
handleAction=async function(el){
  const a=el.dataset.action,v=datasetDetail;
  if(a==='dataset-detail-refresh'){await datasetLoadSummary();await datasetLoadFiles();return;}
  if(['dataset-prev','dataset-next','dataset-files-retry'].includes(a)){clearTimeout(datasetSearchTimer);if(a==='dataset-prev')v.offset=Math.max(0,v.offset-100);if(a==='dataset-next')v.offset+=100;await datasetLoadFiles();return;}
  if(a==='dataset-file-info'){const f=v.files.files[Number(el.dataset.index)];modal('数据集文件',`<h3 class="break">${esc(f.path)}</h3><p>${size(f.size)} · ${esc(v.data.dataset.name)}</p><p class="muted">导入时 SHA-256</p><code class="break">${esc(f.sha256)}</code><div class="form-footer"><a href="/api/datasets/${encodeURIComponent(v.id)}/file?path=${encodeURIComponent(f.path)}" download>下载本机文件 →</a></div>`,'dataset-file');return;}
  return datasetBaseAction(el);
};
submitForm=async function(form){if(form.id!=='dataset-search')return datasetBaseSubmit(form);clearTimeout(datasetSearchTimer);datasetDetail.query=new FormData(form).get('query');datasetDetail.offset=0;await datasetLoadFiles();};
document.addEventListener('input',e=>{if(e.target.form?.id!=='dataset-search')return;datasetDetail.query=e.target.value;datasetDetail.offset=0;clearTimeout(datasetSearchTimer);datasetSearchTimer=setTimeout(()=>datasetLoadFiles(),300);});
