'use strict';
// Parse math as Markdown tokens, so backslashes survive and code stays literal.
const noteMarkdownCache=new Map();
let noteMarkdownCacheSize=0;
function noteSanitize(html){
  const fragment=DOMPurify.sanitize(html,{RETURN_DOM_FRAGMENT:true,FORBID_TAGS:['script','style','iframe','object','embed','form','button'],FORBID_ATTR:['id','name']});
  fragment.querySelectorAll('a[href]').forEach(a=>{a.target='_blank';a.rel='noopener noreferrer';});
  const wrapper=document.createElement('div');wrapper.append(fragment);return wrapper.innerHTML;
}
function noteMarkdown(body){
  body=String(body??'');if(noteMarkdownCache.has(body))return noteMarkdownCache.get(body);
  let html;
  try{
    html=noteSanitize(noteMarkdownParser.parse(body));
  }catch(error){html=`<p class="note-render-fallback">${noteEscape(body)}</p>`;}
  if(body.length+html.length<2000000){noteMarkdownCache.set(body,html);noteMarkdownCacheSize+=body.length+html.length;}
  while(noteMarkdownCache.size>64||noteMarkdownCacheSize>2000000){const key=noteMarkdownCache.keys().next().value;noteMarkdownCacheSize-=key.length+noteMarkdownCache.get(key).length;noteMarkdownCache.delete(key);}
  return html;
}
function noteEditorMarkup(id){return `<form id="note-form" data-task="${esc(id)}" data-view="split"><div class="note-editor-toolbar"><strong>添加记录 <small>Markdown · LaTeX</small></strong><div class="note-view-tabs">${['edit','split','preview'].map((v,i)=>button('note-view',['编辑','分栏预览','预览'][i],`data-view="${v}" aria-pressed="${v==='split'}"`,v==='split'?'active':'')).join('')}</div></div><div class="note-editor-panes"><label class="note-source-label">Markdown 源码<textarea name="body" required placeholder="## 实验观察&#10;&#10;行内公式：$E=mc^2$&#10;&#10;独立公式：&#10;$$&#10;L = \\sum_i (y_i - \\hat y_i)^2&#10;$$"></textarea></label><section class="note-editor-preview" aria-label="Markdown 预览"><small>预览</small><div class="note-markdown" id="note-preview-body"><p class="note-empty-preview">输入内容后实时预览</p></div></section></div><details class="note-format-help"><summary>Markdown 与公式写法</summary><p>支持标题、列表、表格、引用、代码块和链接。行内公式使用 <code>$...$</code> 或 <code>\\(...\\)</code>；独立公式使用 <code>$$...$$</code> 或 <code>\\[...\\]</code>。代码块中的公式符号保持原样。</p></details><div class="form-footer note-editor-footer"><input type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" class="note-file-input" hidden>${button('note-import','导入 .md','title="将文件内容追加到编辑器"','small')}<small class="note-char-count">0 字符</small>${button('note-reset','清空编辑器','','small')}<button type="submit" class="primary">保存 Note</button></div></form>`;}
function noteEditorUpdate(form){
  if(!form?.isConnected)return;
  const text=form.querySelector('[name=body]').value,preview=form.querySelector('#note-preview-body');
  const file=form.noteUploadFile;
  if(file||text.length>12000){
    const key=file||text;if(preview.noteSource!==key){preview.noteSource=key;noteRenderDocument(preview,file?{file}:{body:text});}
  }else{noteDisposeDocument(preview);preview.noteSource=null;preview.innerHTML=text.trim()?noteMarkdown(text):'<p class="note-empty-preview">输入内容后实时预览</p>';}
  form.querySelector('.note-char-count').textContent=file?file.name+' · '+size(file.size):text.length.toLocaleString()+' 字符';
}
function noteEditorSetView(form,view){if(!form)return;form.dataset.view=view;form.querySelectorAll('[data-action=note-view]').forEach(b=>{const active=b.dataset.view===view;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});noteEditorUpdate(form);}
function noteEditorMount(form){noteEditorUpdate(form);}
async function noteEditorAction(el){
  const action=el.dataset.action,form=document.querySelector('#note-form');
  if(action==='note-view'){noteEditorSetView(form,el.dataset.view);return true;}
  if(action==='note-import'){form?.querySelector('.note-file-input').click();return true;}
  if(action==='note-reset'){form.reset();delete form.dataset.note;noteClearFile(form);form.querySelector('button[type=submit]').textContent='保存 Note';noteEditorSetView(form,'split');return true;}
  if(action==='note-source-load'){const file=form.noteUploadFile;if(file){const text=await file.text();noteClearFile(form);form.querySelector('[name=body]').value=text;noteEditorSetView(form,'split');}return true;}
  if(action==='note-open'){const slot=el.closest('.note').querySelector('.note-document-slot');noteRenderDocument(slot,{url:noteTextURL(el.dataset.task,el.dataset.note)});return true;}
  if(action.startsWith('note-page-')){const root=el.closest('.note-document-reader'),view=noteDocuments.get(root);if(view)view.worker.postMessage({type:'page',index:action==='note-page-first'?0:action==='note-page-last'?view.total-1:view.index+(action==='note-page-next'?1:-1)});return true;}
  if(action==='note-download'){
    const note=(state.notes||[]).find(n=>n.id===el.dataset.note);if(!note)return true;
    const link=document.createElement('a');link.href=noteTextURL(note.task_id,note.id)+'?download=1';link.download='note-'+note.id+'.md';link.click();return true;
  }
  return false;
}
let notePreviewTimer;
document.addEventListener('input',event=>{if(event.target.matches('#note-form [name=body]')){clearTimeout(notePreviewTimer);const form=event.target.form;notePreviewTimer=setTimeout(()=>noteEditorUpdate(form),120);}});
document.addEventListener('change',async event=>{
  if(!event.target.matches('.note-file-input'))return;
  const input=event.target,file=input.files[0],form=input.form;input.value='';if(!file||!form)return;
  try{
    const source=form.querySelector('[name=body]'),previous=form.noteUploadFile||source.value;
    const combined=previous?new File([previous,'\n\n',file],file.name,{type:'text/markdown'}):file;
    if(combined.size>48000)noteAttachFile(form,combined);
    else{noteClearFile(form);source.value=(await combined.text()).replace(/^\uFEFF/,'');}
    noteEditorSetView(form,'split');source.focus();
  }catch(error){toast(error.message,true);}
});

function noteTextURL(task,id){return '/api/tasks/'+encodeURIComponent(task)+'/notes/'+encodeURIComponent(id)+'/text';}
function noteDocumentMarkup(note){
  if(!note.large)return `<div class="note-markdown">${noteMarkdown(note.body)}</div>`;
  return `<div class="note-document-slot"><p class="note-document-summary">${Number(note.body_chars||0).toLocaleString()} 字符 · ${size(note.body_bytes||0)}</p>${button('note-open','阅读完整文档',`data-task="${esc(note.task_id)}" data-note="${esc(note.id)}"`,'small')}<pre class="note-document-excerpt">${esc(note.body_preview||'')}</pre></div>`;
}
const noteDocuments=new Map();
function noteDisposeDocument(root){const view=noteDocuments.get(root);if(view){view.worker.terminate();noteDocuments.delete(root);}root?.classList.remove('note-document-reader');}
function noteRenderDocument(root,source){
  noteDisposeDocument(root);root.classList.add('note-document-reader');
  root.innerHTML=`<div class="note-doc-controls"><span class="note-doc-status" role="status">正在读取文档…</span>${button('note-page-first','首页','disabled','small')}${button('note-page-prev','上一页','disabled','small')}${button('note-page-next','下一页','disabled','small')}${button('note-page-last','末页','disabled','small')}</div><div class="note-markdown note-page-content"></div>`;
  const worker=new Worker('/static/notes-render-worker.js'),view={worker,index:0};noteDocuments.set(root,view);
  worker.onmessage=event=>{
    if(!root.isConnected||noteDocuments.get(root)!==view){worker.terminate();return;}
    const data=event.data,status=root.querySelector('.note-doc-status');
    if(data.type==='progress'){status.textContent='已读取 '+size(data.received)+(data.total?' / '+size(data.total):'');return;}
    if(data.type==='parsing'){status.textContent=Number(data.chars).toLocaleString()+' 字符 · 正在准备分页…';return;}
    if(data.type==='error'){status.textContent=data.message;return;}
    if(data.type==='page'){view.index=data.index;view.total=data.total;status.textContent='第 '+(data.index+1)+' / '+data.total+' 页';root.querySelector('.note-page-content').innerHTML=noteSanitize(data.html);for(const action of ['first','prev'])root.querySelector('[data-action=note-page-'+action+']').disabled=data.index<=0;for(const action of ['last','next'])root.querySelector('[data-action=note-page-'+action+']').disabled=data.index>=data.total-1;}
  };
  worker.onerror=()=>{if(root.isConnected)root.querySelector('.note-doc-status').textContent='文档解析失败，请重试或下载 Markdown 源文';};
  worker.postMessage({type:'init',source});
}
new MutationObserver(()=>{for(const root of noteDocuments.keys())if(!root.isConnected)noteDisposeDocument(root);}).observe(document.documentElement,{childList:true,subtree:true});
function noteClearFile(form){delete form.noteUploadFile;const source=form.querySelector('[name=body]');source.readOnly=false;source.required=true;source.placeholder=source.dataset.originalPlaceholder||source.placeholder;form.querySelector('[data-action=note-source-load]')?.remove();}
function noteAttachFile(form,file){
  form.noteUploadFile=file;const source=form.querySelector('[name=body]');if(!source.dataset.originalPlaceholder)source.dataset.originalPlaceholder=source.placeholder;source.value='';source.readOnly=true;source.required=false;source.placeholder='已选择 '+file.name+'\n\n可直接预览全文并保存。需要修改内容时，点击「载入源码编辑」。';
  if(!form.querySelector('[data-action=note-source-load]'))form.querySelector('.note-editor-footer').insertAdjacentHTML('afterbegin',button('note-source-load','载入源码编辑','','small'));
}
async function noteLoadForEdit(note,form){
  if(!note.large){noteClearFile(form);form.querySelector('[name=body]').value=note.body;}
  else{const response=await fetch(noteTextURL(note.task_id,note.id));if(!response.ok)throw Error('读取 Notes 失败');if(!form.isConnected)return;
    if(note.body_chars>200000)noteAttachFile(form,new File([await response.blob()],'note-'+note.id+'.md',{type:'text/markdown'}));
    else{noteClearFile(form);form.querySelector('[name=body]').value=await response.text();}
  }
  form.dataset.note=note.id;noteEditorSetView(form,'split');form.querySelector('button[type=submit]').textContent='保存修改';form.querySelector('[name=body]').focus();
}
async function noteSaveForm(form){
  const body=form.querySelector('[name=body]').value,file=form.noteUploadFile||(body.length>12000?new File([body],'note.md',{type:'text/markdown'}):null);
  if(!file)return api(`/api/tasks/${form.dataset.task}/notes`,{id:form.dataset.note||null,body});
  const data=new FormData();data.append('file',file);if(form.dataset.note)data.append('id',form.dataset.note);
  const progress=form.querySelector('.note-char-count');progress.textContent='正在上传 Markdown…';
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('POST',`/api/tasks/${form.dataset.task}/notes/file`);xhr.setRequestHeader('X-CSRF-Token',csrf);
    xhr.upload.onprogress=e=>{progress.textContent=e.lengthComputable?'上传 '+Math.round(e.loaded/e.total*100)+'% · '+size(e.loaded)+' / '+size(e.total):'上传 '+size(e.loaded);};
    xhr.upload.onload=()=>{progress.textContent='上传完成，正在保存文档…';};
    xhr.onerror=()=>reject(Error('Markdown 上传失败，请重试'));
    xhr.onload=()=>{try{const value=JSON.parse(xhr.responseText);if(xhr.status>=200&&xhr.status<300)resolve(value);else reject(Error(value.error||'保存失败'));}catch(error){reject(error);}};
    xhr.send(data);
  });
}
