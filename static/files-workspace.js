'use strict';
const filesWorkspaceBaseRender=render,filesWorkspaceBaseAction=handleAction,filesWorkspaceBaseSubmit=submitForm;
const projectFileViews=new Map(),projectFileQueue=[];let projectFileWorkers=0,projectFileObserver=null,projectFilesMounted='';
function transferArgs(job){try{return JSON.parse((job.operation||'').slice((job.operation||'').indexOf(':')+1));}catch(_){return [];}}
function uploadPageJobs(){
  const uploads=state.uploads||[],ids=new Set(uploads.map(u=>u.id)),jobs=new Set(uploads.map(u=>u.job_id).filter(Boolean));
  return (state.jobs||[]).filter(j=>!jobs.has(j.id)&&((j.operation||'').startsWith('upload_dataset:')||(j.operation||'').startsWith('process:')&&!ids.has(transferArgs(j)[0])));
}
function uploadJobCard(job){
  const dataset=(job.operation||'').startsWith('upload_dataset:')?state.datasets?.find(d=>d.id===transferArgs(job)[0]):null;
  return `<article class="card transfer-card" data-job-id="${esc(job.id)}"><div class="card-head"><div><h3>${esc(dataset?'数据集同步 · '+dataset.name:job.label)}</h3><small class="muted">${fmt(job.created_at)} · ${dataset?'同步到服务器':'文件导入'}</small></div>${badge(job.status,job.cancel_requested&&job.status==='running'?'正在中断':job.status==='running'?'处理中':job.status==='queued'?'等待处理':job.status==='canceled'?'已中断':undefined)}</div>${transferProgress(job)}${job.error?`<p class="error-text">${esc(job.error)}</p>`:''}<div class="actions">${interruptButton(job,'job')}${dataset&&['failed','canceled'].includes(job.status)?button('dataset-retry','重试同步',`data-id="${esc(dataset.id)}"`,'small primary'):''}${dataset?`<a href="#dataset/${encodeURIComponent(dataset.id)}">查看数据集 →</a>`:''}<a href="#events">操作记录 →</a></div></article>`;
}
uploadsPage=function(){
  const entries=[...(state.uploads||[]).map(u=>({value:u,kind:'upload',active:transferActive.includes(u.status)})),...uploadPageJobs().map(j=>({value:j,kind:'job',active:['queued','running'].includes(j.status)}))];
  entries.sort((a,b)=>Number(b.active)-Number(a.active)||(b.value.created_at||0)-(a.value.created_at||0));
  const active=entries.filter(e=>e.active),history=entries.filter(e=>!e.active).slice(0,30),cards=list=>`<div class="grid">${list.map(e=>e.kind==='upload'?transferCard(e.value):uploadJobCard(e.value)).join('')}</div>`;
  return pageHead('上传与进度','浏览器上传、解压、hash 校验和服务器同步，都可以在这里查看。','<a class="upload-record-link" href="#events">完整操作记录 →</a>')+`<div class="notice">进度按当前阶段显示，包含速度、剩余时间和正在处理的文件。可以中断并保留已传文件；浏览器传到本机期间请保持窗口打开，后台导入和同步可继续运行。</div>`+(entries.length?`<div class="upload-summary"><span><b>${active.length}</b> 正在进行</span><span><b>${entries.filter(e=>e.value.status==='success').length}</b> 已完成</span><span><b>${entries.filter(e=>['failed','canceled','interrupted'].includes(e.value.status)).length}</b> 已中断 / 待处理</span></div>${active.length?'<div class="section-title"><h2>正在上传与处理</h2></div>'+cards(active):'<div class="upload-idle">当前没有进行中的传输。</div>'}${history.length?'<div class="section-title"><h2>最近传输</h2><span>最近 '+history.length+' 条</span></div>'+cards(history):''}`:empty('暂无上传或同步','从项目上传代码和任务文件，或从数据集页面导入、同步数据集。'));
};
function fileView(project,task=''){
  if(!projectFileViews.has(project))projectFileViews.set(project,new Map());
  const views=projectFileViews.get(project),key=task||'code';
  if(!views.has(key))views.set(key,{project,task,key,query:'',offset:0,seq:0,fetchedAt:0,selected:new Set()});
  return views.get(key);
}
function fileEditable(view){
  if(!view.task&&(state.code_sources||[]).some(s=>s.id===view.project&&s.path))return false;
  const task=state.tasks?.find(t=>t.id===view.task),blocked=task&&['staging','launching','running','suspended','canceling','syncing','sync_failed','lost'].includes(task.status);
  return !blocked&&!state.uploads?.some(u=>u.kind==='project'&&u.project_id===view.project&&(!u.task_id||u.task_id===view.task)&&!['success','canceled'].includes(u.status));
}
function publicFileDeleteBlock(view){
  if((state.code_sources||[]).some(s=>s.id===view.project&&s.path))return '已绑定源码目录，请在原目录修改；需要清除工具副本时请先解绑。';
  if(state.tasks?.some(t=>t.project_id===view.project&&['staging','launching','running','suspended','canceling','syncing','sync_failed','lost'].includes(t.status)))return '项目有执行、暂停运行、回传或状态未知的实验，暂不能清除公共文件。';
  if(!fileEditable(view)||state.jobs?.some(j=>['queued','running'].includes(j.status)&&(j.operation||'').startsWith('clear_public_files:')&&transferArgs(j)[0]===view.project))return '文件导入或清除尚未完成；失败或中断的导入请先重试或放弃。';
  return '';
}
function publicFileDeleteTools(view){
  const block=publicFileDeleteBlock(view),disabled=block||view.loading;
  return `<div class="public-file-delete-tools"><label><input type="checkbox" data-public-file-page ${view.data?.files.length&&view.data.files.every(f=>view.selected.has(f.path))?'checked':''} ${disabled||!view.data?.files.length?'disabled':''}>选择本页</label><span>已选 ${view.selected.size} 个文件</span><div class="actions">${button('public-file-delete-selected','删除选中',`${disabled||!view.selected.size?'disabled':''}`,'small danger')}${view.selected.size?button('public-file-unselect','取消选择','','small'):''}${button('public-file-delete-all','清空全部',`${disabled||!view.data?.total_count?'disabled':''}`,'small danger')}</div></div>${block?`<p class="public-file-delete-block">${esc(block)}</p>`:''}`;
}
function fileRows(view){
  if(view.error)return `<div class="notice warning">${esc(view.error)}</div>${button('project-files-refresh','重新读取',`data-scope="${esc(view.key)}"`,'small')}`;
  if(!view.data)return '<div class="loading file-loading">正在读取文件列表…</div>';
  const data=view.data,editable=fileEditable(view),deletable=!view.task&&!publicFileDeleteBlock(view),textFile=v=>v.size<=2*1024**2&&/\.(py|js|ts|json|txt|md|yaml|yml|toml|ini|cfg|sh|csv|html|css|xml|tex|ipynb)$|(^|\/)(README|LICENSE|Dockerfile|Makefile|\.env)$/i.test(v.path);
  return `<div class="inline-file-meta"><span>${data.total_count} 个文件 · ${size(data.total_bytes)}${view.query?' · 匹配 '+data.matched_count+' 个':''}</span><div class="actions">${view.loading?'<small>正在更新…</small>':''}${button('project-files-refresh','↻ 刷新',`data-scope="${esc(view.key)}"`,'small')}</div></div>${!view.task?publicFileDeleteTools(view):''}${data.files.length?`<div class="inline-file-table ${!view.task?'public-file-table':''}"><table><thead><tr><th>文件路径</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>${data.files.map(v=>{const type=(v.path.split('.').pop()||'FILE').slice(0,5).toUpperCase(),url=`/api/projects/${encodeURIComponent(view.project)}/file?path=${encodeURIComponent(v.path)}&download=1${view.task?'&task_id='+encodeURIComponent(view.task):''}`;return `<tr><td>${!view.task?`<input type="checkbox" class="public-file-checkbox" data-public-file-path="${esc(v.path)}" aria-label="选择 ${esc(v.path)}" ${view.selected.has(v.path)?'checked':''} ${!deletable||view.loading?'disabled':''}>`:''}<span class="inline-file-type">${esc(type)}</span><span class="inline-file-path">${esc(v.path)}</span></td><td>${size(v.size)}</td><td>${fmt(v.modified_at)}</td><td><div class="actions">${textFile(v)?button('edit-file','编辑',`data-project="${esc(view.project)}" data-task="${esc(view.task)}" data-path="${esc(v.path)}" ${editable?'':'disabled title="任务执行或文件导入期间暂不能编辑"'}`,'small'):''}<a href="${url}" download>下载</a>${!view.task?button('public-file-delete-one','删除',`data-path="${esc(v.path)}" ${!deletable||view.loading?'disabled':''}`,'small danger'):''}</div></td></tr>`;}).join('')}</tbody></table></div>`:empty(view.query?'没有匹配文件':view.task?'暂无任务专属文件':'还没有项目公共代码',view.query?'换一个文件名或目录名试试。':view.task?'该实验可直接使用项目公共代码，也可以上传专属输入文件。':'上传代码文件、文件夹或 ZIP 后，会直接显示在这里。')}
  <div class="inline-file-pagination"><small>${data.matched_count?data.offset+1:0}–${Math.min(data.offset+data.files.length,data.matched_count)} / ${data.matched_count}</small><div class="actions">${button('project-files-prev','上一页',`data-scope="${esc(view.key)}" ${!view.offset||view.loading?'disabled':''}`,'small')}${button('project-files-next','下一页',`data-scope="${esc(view.key)}" ${data.offset+data.limit>=data.matched_count||view.loading?'disabled':''}`,'small')}</div></div>`;
}
function fileCard(project,task){
  const view=fileView(project.id,task?.id),title=task?'#'+task.seq+' '+task.name:'项目公共代码';
  const bound=!task&&(state.code_sources||[]).some(s=>s.id===project.id&&s.path);
  return `<section class="card inline-file-card" data-file-scope="${esc(view.key)}"><div class="card-head"><div><h3>${esc(title)}</h3><small class="muted">${task?'此实验的专属输入文件；同名文件覆盖公共代码':'供本项目全部实验使用'}</small></div>${button('upload-files',task?'上传任务文件':'上传代码 / ZIP',`data-project="${esc(project.id)}" data-task="${esc(view.task)}" ${bound?'disabled title="请在绑定的源码目录中修改"':''}`,'primary small')}</div><form class="inline-file-search" data-file-query-scope="${esc(view.key)}"><input name="query" value="${esc(view.query)}" placeholder="搜索文件名或目录名" aria-label="${esc(title)}：搜索文件" autocomplete="off"><button type="submit" class="small">搜索</button></form><div data-file-results="${esc(view.key)}">${fileRows(view)}</div></section>`;
}
function projectFilesPage(project,tasks){
  return `<div class="notice">公共代码和各实验的专属输入文件直接列在下方。文件列表显示名称、大小和修改时间；实验生成的输出仍在实验详情中查看。${state.uploads?.some(u=>u.kind==='project'&&u.project_id===project.id&&!['success','canceled'].includes(u.status))?' <a href="#uploads">文件导入记录与进度 →</a>':''}</div>${fileCard(project,null)}${tasks.length?'<div class="section-title"><h2>任务输入文件</h2><span>'+tasks.length+' 个实验</span></div>'+tasks.map(t=>fileCard(project,t)).join(''):''}`;
}
function fileRevision(view){return JSON.stringify([state.local_storage?.root,state.projects?.find(p=>p.id===view.project)?.public_files_revision,state.uploads?.filter(u=>u.kind==='project'&&u.project_id===view.project&&(!u.task_id||u.task_id===view.task)).map(u=>[u.id,u.local_ready||u.status==='success',u.finished_at])]);}
function updateFileRows(view){if(route()[0]!=='project'||route()[1]!==view.project||projectTab!=='files')return;const target=$(`[data-file-results="${view.key}"]`);if(target)target.innerHTML=fileRows(view);}
function requestFileView(view,force=false){
  if(view.loading&&!force)return;
  if(!force&&view.fetchedAt&&Date.now()-view.fetchedAt<30000&&view.revision===fileRevision(view))return;
  view.seq++;view.loading=true;view.error='';const seq=view.seq,revision=fileRevision(view);updateFileRows(view);
  projectFileQueue.push({view,seq,revision});drainFileQueue();
}
async function drainFileQueue(){
  while(projectFileWorkers<3&&projectFileQueue.length){const entry=projectFileQueue.shift(),{view,seq,revision}=entry;if(view.seq!==seq)continue;projectFileWorkers++;
    (async()=>{try{const data=await api(`/api/projects/${encodeURIComponent(view.project)}/file-index?offset=${view.offset}&limit=100&query=${encodeURIComponent(view.query)}${view.task?'&task_id='+encodeURIComponent(view.task):''}`);if(view.seq===seq){view.data=data;view.revision=revision;view.fetchedAt=Date.now();}}
      catch(e){if(view.seq===seq){view.error=e.message;view.fetchedAt=Date.now();view.revision=revision;}}
      finally{if(view.seq===seq){view.loading=false;updateFileRows(view);}projectFileWorkers--;drainFileQueue();}})();
  }
}
render=function(){
  filesWorkspaceBaseRender();projectFileObserver?.disconnect();
  const [page,id]=route();if(page!=='project'||projectTab!=='files'){if(projectFilesMounted)for(const view of projectFileViews.get(projectFilesMounted)?.values()||[])view.fetchedAt=0;projectFilesMounted='';return;}
  projectFilesMounted=id;
  projectFileObserver=new IntersectionObserver(entries=>{for(const e of entries)if(e.isIntersecting)requestFileView(fileView(id,e.target.dataset.fileScope==='code'?'':e.target.dataset.fileScope));},{rootMargin:'400px'});
  document.querySelectorAll('[data-file-scope]').forEach(el=>projectFileObserver.observe(el));
};
let publicFileDeleteRequest=null;
handleAction=async function(el){
  const action=el.dataset.action;
  if(action==='public-file-unselect'){const view=fileView(route()[1]);view.selected.clear();updateFileRows(view);return;}
  if(['public-file-delete-one','public-file-delete-selected','public-file-delete-all'].includes(action)){
    const view=fileView(route()[1]),block=publicFileDeleteBlock(view);if(block)throw Error(block);
    const all=action==='public-file-delete-all',paths=all?[]:action==='public-file-delete-one'?[el.dataset.path]:[...view.selected];
    if(!all&&!paths.length)throw Error('请先选择文件');
    publicFileDeleteRequest={project:view.project,paths,all};
    modal(all?'清空项目公共文件':'删除项目公共文件',`<p>${all?'将清空整个公共代码目录，包括其他分页和搜索未显示的文件。':'将删除以下 '+paths.length+' 个公共文件：'}</p>${!all?`<ul class="public-file-delete-list">${paths.slice(0,10).map(p=>'<li>'+esc(p)+'</li>').join('')}${paths.length>10?'<li>以及另外 '+(paths.length-10)+' 个文件</li>':''}</ul>`:''}<div class="notice warning">删除后无法恢复。任务专属文件、已完成实验的输出和服务器运行副本保留。后续实验使用清除后的公共代码。</div><p class="muted">磁盘清理在后台独立进程执行，结果在操作记录中查看。</p><div class="actions">${button('close-dialog','取消','','small')}${button('public-file-delete-confirm',all?'确认清空全部':'确认删除','','danger')}</div>`,'confirm');return;
  }
  if(action==='public-file-delete-confirm'){
    if(!publicFileDeleteRequest)throw Error('请重新选择要清除的文件');
    const req=publicFileDeleteRequest,result=await api(`/api/projects/${encodeURIComponent(req.project)}/files/delete`,{paths:req.paths,all:req.all});
    publicFileDeleteRequest=null;closeDialogFor(el);const view=fileView(req.project);view.selected.clear();view.offset=0;
    state.jobs=state.jobs||[];state.jobs.push(result.job);toast('清除已提交，在操作记录中查看结果');await load();requestFileView(view,true);return;
  }
  const a=el.dataset.action;if(!['project-files-refresh','project-files-prev','project-files-next'].includes(a))return filesWorkspaceBaseAction(el);
  const view=fileView(route()[1],el.dataset.scope==='code'?'':el.dataset.scope);if(a==='project-files-prev')view.offset=Math.max(0,view.offset-100);if(a==='project-files-next')view.offset+=100;requestFileView(view,true);
};
submitForm=async function(form){
  if(form.dataset.fileQueryScope){const view=fileView(route()[1],form.dataset.fileQueryScope==='code'?'':form.dataset.fileQueryScope);clearTimeout(view.searchTimer);view.query=new FormData(form).get('query');view.offset=0;requestFileView(view,true);return;}
  const view=form.id==='editor-form'?fileView(form.dataset.project,form.dataset.task):null;await filesWorkspaceBaseSubmit(form);if(view){view.fetchedAt=0;requestFileView(view,true);}
};
document.addEventListener('input',e=>{if(!e.target.form?.dataset.fileQueryScope)return;const key=e.target.form.dataset.fileQueryScope,view=fileView(route()[1],key==='code'?'':key);view.query=e.target.value;view.offset=0;clearTimeout(view.searchTimer);view.searchTimer=setTimeout(()=>requestFileView(view,true),300);});
document.addEventListener('change',e=>{
  const el=e.target;if(!el.matches('[data-public-file-path],[data-public-file-page]'))return;
  const view=fileView(route()[1]);if(publicFileDeleteBlock(view))return;
  const paths=el.hasAttribute('data-public-file-page')?view.data.files.map(f=>f.path):[el.dataset.publicFilePath];
  const next=new Set(view.selected);for(const path of paths){if(el.checked)next.add(path);else next.delete(path);}
  if(next.size>1000){toast('一次最多选择 1000 个文件；可分批删除或清空全部',true);updateFileRows(view);return;}
  view.selected=next;updateFileRows(view);
});
