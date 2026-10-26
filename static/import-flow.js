'use strict';
const flowBaseSubmit=submitForm,flowBaseAction=handleAction,flowBaseRender=render,flowBaseControls=styleControls;
const preparations=new Map();let flowPaintQueued=false;
const flowBaseUploadView=uploadView;
uploadView=function(upload){const value=flowBaseUploadView(upload);if(clientTransfers.get(upload.id)?.canceled&&!['success','canceled','interrupted','failed'].includes(value.status))Object.assign(value,{status:'interrupting',cancel_requested:true});return value;};
for(const phase of ['scanning','copying'])if(!transferActive.includes(phase))transferActive.push(phase);
Object.assign(phases,{preparing:'后台整理文件清单',deleting:'删除本机数据集',scanning:'后台扫描文件',copying:'后台复制并计算 hash'});
function paintUpload(){if(flowPaintQueued)return;flowPaintQueued=true;setTimeout(()=>{flowPaintQueued=false;renderTransferDock();},250);}
function startFileWorker(files,data,resume=null){
 const provisional='preparing-'+crypto.randomUUID(),worker=new Worker('/static/upload-worker.js');let key=provisional;
 const client={worker,canceled:false,progress:{phase:'preparing',bytes:0,total:0,files_done:0,files_total:files.length}};
 const pending={id:provisional,kind:data.kind||'project',label:resume?'继续传输':data.kind==='dataset'?'导入数据集 · '+data.name:'导入项目文件',status:'receiving',created_at:Date.now()/1000,file_count:files.length,progress:client.progress,preparing:true};
 preparations.set(key,pending);clientTransfers.set(key,client);render();
 worker.onmessage=async({data:message})=>{
  if(message.type==='notice'){toast(message.message,true);return;}
  if(message.type==='preparing'){client.progress={phase:'preparing',bytes:0,total:0,files_done:message.count,files_total:message.total};pending.progress=client.progress;pending.total_bytes=message.bytes;paintUpload();return;}
  if(message.type==='bound'){
   preparations.delete(key);clientTransfers.delete(key);state.uploads=state.uploads.filter(u=>u.id!==key);key=message.upload.id;
   clientTransfers.set(key,client);const old=state.uploads.find(u=>u.id===key);if(old)Object.assign(old,message.upload);else state.uploads.push(message.upload);render();return;
  }
  if(message.type==='progress'){client.progress=message.progress;const old=state.uploads.find(u=>u.id===key);if(old&&message.upload.received_bytes>=old.received_bytes)Object.assign(old,message.upload);paintUpload();return;}
  preparations.delete(key);clientTransfers.delete(key);state.uploads=state.uploads.filter(u=>u.id!==provisional);worker.terminate();
  if(message.type==='complete'){toast('文件已接收，后台继续整理、校验和同步');}
  if(message.type==='error'){toast('传输中断：'+message.message+'；已接收文件保留。',true);}
  await load();
 };
 worker.onerror=async event=>{preparations.delete(key);clientTransfers.delete(key);worker.terminate();toast('上传线程出错：'+event.message,true);await load();};
 const paths=files.map(f=>f.webkitRelativePath?f.webkitRelativePath.split('/').slice(1).join('/'):f.name);
 worker.postMessage({type:'start',files,paths,data,resume,csrf});
}
submitForm=async function(form){
 if(!['dataset-form','files-form','resume-form'].includes(form.id))return flowBaseSubmit(form);
 const fd=new FormData(form),data=form.id==='dataset-form'?{kind:'dataset',name:fd.get('name')}:{kind:'project',project_id:form.dataset.project,task_id:form.dataset.task||null};
 if(fd.get('mode')==='local'){
  const upload=await api('/api/uploads/local',{...data,source_path:fd.get('source_path')});closeDialogFor(form);toast('后台导入已开始，可以关闭网页；进度与中断在上传记录查看');await load();return;
 }
 const files=Array.from($('input[type=file]',form).files);if(!files.length)throw Error('请选择文件');data.mode=fd.get('mode')==='zip'?'zip':'files';
 closeDialogFor(form);startFileWorker(files,data,form.id==='resume-form'?form.dataset.id:null);toast('已交给上传线程，可继续使用其他页面');
};
pumpFiles=function(upload,files){startFileWorker(files,{kind:upload.kind,mode:upload.mode},upload.id);};
handleAction=async function(el){
 const a=el.dataset.action,id=el.dataset.id,client=clientTransfers.get(id);
 if(a==='upload-discard'&&id?.startsWith('preparing-')){client?.worker?.postMessage({type:'cancel'});return;}
 if(a==='confirm-discard'&&client?.worker){client.canceled=true;client.worker.postMessage({type:'cancel'});}
 if(a==='transfer-detail'&&id?.startsWith('preparing-')){toast('文件清单正在后台整理，建立上传后即可查看详细记录');return;}
 if(a==='upload-interrupt'&&client?.worker){client.canceled=true;client.worker.postMessage({type:'cancel'});paintUpload();toast('正在中断，已接收文件会保留');return;}
 if(a==='dataset-delete'){
  const d=state.datasets.find(d=>d.id===id);modal('删除数据集',`<p>从数据集列表移除 <b>${esc(d.name)}</b>，并在后台删除工具保存的本机副本及未完成的导入暂存。</p><p class="muted">原始导入文件和服务器上的已有数据保留。存在执行中、等待执行的实验或后台同步／核验时，会拒绝删除。</p><div class="form-footer">${button('confirm-dataset-delete','删除本机副本并移除',`data-id="${esc(id)}"`,'danger')}</div>`,'dataset-delete');return;
 }
 if(a==='confirm-dataset-delete'){
  for(const u of state.uploads.filter(u=>u.dataset_id===id)){const c=clientTransfers.get(u.id);if(c?.worker){c.canceled=true;c.worker.postMessage({type:'cancel'});}}
  await api(`/api/datasets/${id}/delete`,{});$('#dialog').close();if(route()[0]==='dataset')location.hash='datasets';toast('删除已提交到后台，可在操作记录查看');await load();return;
 }
 return flowBaseAction(el);
};
render=function(){for(const [id,value] of preparations)if(!state.uploads.some(u=>u.id===id))state.uploads.push(value);flowBaseRender();flowControls();};
function flowControls(){
 document.querySelectorAll('#dataset-form,#files-form').forEach(form=>{
  const select=form.elements.mode;if(!select)return;
  if(!select.querySelector('[value=local]'))select.add(new Option('本机路径 · 大数据推荐','local'));
  if(!form.elements.source_path){const label=document.createElement('label');label.className='local-import-path';label.innerHTML='<span>本机文件夹或 ZIP 绝对路径</span><input name="source_path" placeholder="例如 D:\\Datasets\\BraTS2018_cl"><small class="muted">控制器所在电脑的路径。由后台直接复制、计算 hash 和同步，无需浏览器逐文件传输；原始文件保留。</small>';select.closest('label').after(label);}
  const local=select.value==='local',input=$('input[type=file]',form);input.closest('label').hidden=local;input.required=!local;form.elements.source_path.closest('label').hidden=!local;form.elements.source_path.required=local;
  const note=form.querySelector(':scope > p.muted');if(note&&form.id==='dataset-form'){const text=local?'后台直接读取本机来源、复制并校验。提交后可以关闭网页，在上传与进度中查看进度或中断。':'由独立上传线程传到本机，随后后台整理和同步。可切换页面；浏览器传输完成前请保持窗口打开。';if(note.textContent!==text)note.textContent=text;}
 });
 if(route()[0]==='datasets')document.querySelectorAll('[data-action=dataset-verify]').forEach(b=>{if(b.parentElement.querySelector('[data-action=dataset-delete]'))return;b.insertAdjacentHTML('afterend',button('dataset-delete','删除',`data-id="${esc(b.dataset.id)}"`,'small danger'));});
 document.querySelectorAll('.card-foot:has([data-action=dataset-delete])').forEach(footer=>{if(footer.querySelector('.dataset-operation-actions'))return;const row=document.createElement('div');row.className='dataset-operation-actions';for(const button of Array.from(footer.children).filter(el=>el.tagName==='BUTTON'))row.append(button);footer.append(row);});
 if(route()[0]==='dataset'&&datasetDetail?.data){const box=$('#dataset-heading .page-actions')||$('#dataset-heading .page-title');if(box&&!box.querySelector('[data-action=dataset-delete]'))box.insertAdjacentHTML('beforeend',button('dataset-delete','删除数据集',`data-id="${esc(datasetDetail.id)}"`,'small danger'));}
}
styleControls=function(){flowBaseControls();flowControls();};
document.addEventListener('change',e=>{if(e.target.name==='mode')flowControls();});
if(document.body.dataset.authorized==='true')setInterval(()=>{if(state.jobs?.some(j=>j.operation?.startsWith('delete_dataset:')&&['queued','running'].includes(j.status)))load();},1000);
