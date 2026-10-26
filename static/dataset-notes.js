'use strict';
const datasetNoteBaseAction=handleAction,datasetNoteBaseSubmit=submitForm;

function datasetNotePanel(dataset,detail=false){
  if(!state.runtime_features?.dataset_notes)return '';
  const note=dataset.note,text=note?.text??note?.preview??'',limit=detail?2000:240;
  return `<section class="dataset-note ${detail?'dataset-note-detail':''}" aria-label="数据集备注"><div class="dataset-note-heading"><span>备注${detail?'<small>仅保存在工具内</small>':''}</span>${button('dataset-note-edit',text?'编辑备注':'添加备注',`data-id="${esc(dataset.id)}"`,'small')}</div>${text?`<p class="dataset-note-text">${esc(text.slice(0,limit))}${(note.characters??text.length)>limit?'…':''}</p>${(note.characters??text.length)>limit?button('dataset-note-edit','查看完整备注',`data-id="${esc(dataset.id)}"`,'small dataset-note-more'):''}`:'<p class="dataset-note-empty">记录用途、数据划分或使用注意事项</p>'}${detail&&note?.updated_at?`<small class="muted">更新于 ${fmt(note.updated_at)}</small>`:''}</section>`;
}

handleAction=async function(el){
  if(el.dataset.action!=='dataset-note-edit')return datasetNoteBaseAction(el);
  const id=el.dataset.id,dataset=state.datasets?.find(d=>d.id===id);
  // Do not open a second dialog over an unrelated form after a slow response.
  const revision=dialogRevision;
  el.disabled=true;
  try{
    const note=await api(`/api/datasets/${encodeURIComponent(id)}/note`);
    if(dialogRevision!==revision)return;
    modal('数据集备注',`<form id="dataset-note-form" data-id="${esc(id)}" data-revision="${note.revision}"><p class="dataset-note-context">${esc(dataset?.name||'数据集')}<small>仅在工具内保存，不上传到服务器，不影响文件与校验。</small></p><label for="dataset-note-text">备注内容<textarea id="dataset-note-text" name="text" rows="12" maxlength="1000000" placeholder="例如：用于验证的固定划分；标签已脱敏；使用前注意归一化…">${esc(note.text)}</textarea></label><div class="dataset-note-meta"><small class="muted">留空并保存即可清除备注</small><small class="muted" id="dataset-note-count">${note.text.length.toLocaleString()} 字符</small></div><p class="error-text" id="dataset-note-error" role="alert" hidden></p>${footer('保存备注')}</form>`,'dataset-note');
    $('#dataset-note-text').focus();
  }finally{if(el.isConnected)el.disabled=false;}
};

submitForm=async function(form){
  if(form.id!=='dataset-note-form')return datasetNoteBaseSubmit(form);
  if(form.dataset.saving)return;
  const input=form.elements.text,error=$('#dataset-note-error',form),submit=$('button[type=submit]',form);
  form.dataset.saving='true';input.disabled=true;submit.disabled=true;error.hidden=true;
  try{
    const note=await api(`/api/datasets/${encodeURIComponent(form.dataset.id)}/note`,{text:input.value,revision:Number(form.dataset.revision)});
    const dataset=state.datasets?.find(d=>d.id===form.dataset.id);
    if(dataset)dataset.note={...note,text:undefined,preview:note.text.slice(0,240),characters:note.text.length};
    if(datasetDetail?.id===form.dataset.id&&datasetDetail.data){datasetDetail.data.dataset.note=note;datasetDetail.fingerprint=datasetFingerprint(form.dataset.id);}
    closeDialogFor(form);render();toast(note.text?'备注已保存':'备注已清除');
  }catch(e){error.textContent=e.message;error.hidden=false;}
  finally{delete form.dataset.saving;input.disabled=false;submit.disabled=false;}
};

document.addEventListener('input',event=>{
  if(event.target.id==='dataset-note-text')$('#dataset-note-count').textContent=event.target.value.length.toLocaleString()+' 字符';
});

// The first state request may finish before the last deferred script loads.
if(document.body.dataset.authorized==='true'&&state.settings)render();
