'use strict';
const importPolicyLabels={level:'执行级别',priority:'优先级（1 最优先）',memory_mb:'峰值显存（MB）',max_utilization:'低干扰利用率上限（%）',gpu_name:'GPU 型号包含',max_retries:'OOM 自动重试次数',server_ids:'允许的服务器',environment:'执行环境',datasets:'数据集文件夹名'};
const importPolicySignature=value=>JSON.stringify((function stable(v){return Array.isArray(v)?v.map(stable):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;})(value));
function importPolicyMarkup(changes){
 const item=(key,control)=>`<section class="scheduling-field"><label class="scheduling-field-toggle"><input type="checkbox" class="scheduling-check" data-import-field="${key}" ${Object.hasOwn(changes,key)||key==='environment'&&(Object.hasOwn(changes,'conda_env')||Object.hasOwn(changes,'server_conda_envs'))?'checked':''}>${importPolicyLabels[key]}</label><div class="import-policy-controls" data-import-controls="${key}">${control}</div></section>`;
 const number=(key,value,min,max)=>`<input name="batch_${key}" type="number" value="${esc(changes[key]??value)}" min="${min}" max="${max}" step="${key==='max_utilization'?'any':1}" aria-label="${importPolicyLabels[key]}">`;
 const env=Object.keys(changes.server_conda_envs||{}).length?'servers':changes.conda_env?'common':'inherit';
 return `<details class="import-policy" ${Object.keys(changes).length?'open':''}><summary>导入时统一配置 <span>可选 · 只覆盖勾选字段</span></summary><p class="muted">在创建前统一调整本批全部实验。未勾选的字段保留文件中的原值，不修改源文件、命令或依赖。勾选执行环境并选“继承默认”，可清除导入文件中其他机器的环境配置。</p><div class="scheduling-grid">`+
 item('level',`<select name="batch_level">${[['idle_only','空闲卡'],['low_interference','低干扰：按利用率峰值共享'],['emergency','紧急：允许共享']].map(([v,l])=>`<option value="${v}" ${changes.level===v?'selected':''}>${l}</option>`).join('')}</select>`)+
 item('priority',number('priority',40,1,999))+item('memory_mb',number('memory_mb',0,0,1000000))+item('max_utilization',number('max_utilization',30,0,100))+
 item('gpu_name',`<input name="batch_gpu_name" value="${esc(changes.gpu_name||'')}" placeholder="留空取消型号限制">`)+item('max_retries',number('max_retries',2,0,20))+
 item('server_ids',`<div class="scheduling-servers">${state.servers.map(s=>`<label class="scheduling-field-toggle"><input type="checkbox" class="scheduling-check" name="batch_server_ids" value="${s.id}" ${(changes.server_ids||[]).includes(s.id)?'checked':''}>${esc(s.name)}</label>`).join('')}</div><small>勾选本字段但不选服务器：允许全部服务器。</small>`)+
 item('environment',`<select name="batch_environment_mode"><option value="inherit" ${env==='inherit'?'selected':''}>继承项目／服务器默认环境</option><option value="servers" ${env==='servers'?'selected':''}>分别选择每台服务器的环境</option><option value="common" ${env==='common'?'selected':''}>所有服务器使用同一环境名称</option></select><div data-import-env-common><input name="batch_conda_env" value="${esc(changes.conda_env||'')}" placeholder="如 pytorch；避免另一台机器的绝对路径"></div><div data-import-env-servers>${state.servers.map(s=>`<div class="scheduling-env-server"><label>${esc(s.name)}<select name="batch_env_${s.id}" data-environment-server="${s.id}" data-fallback="继承项目／服务器默认">${environmentOptions(s,changes.server_conda_envs?.[s.id]||'','继承项目／服务器默认')}</select></label>${environmentRefresh(s)}</div>`).join('')}</div>`)+
 item('datasets',`<input name="batch_datasets" value="${esc((changes.datasets||[]).join(', '))}" placeholder="逗号分隔；留空清除本批数据集声明">`)+`</div></details><p class="import-config-note" role="status"></p>`;
}
function importPolicyRead(){
 const form=$('#batch-import-form'),changes={};if(!form?.querySelector('.import-policy'))return batchDialog?.changes||{};
 for(const el of form.querySelectorAll('[data-import-field]:checked')){
  const key=el.dataset.importField;
  if(key==='environment'){
   const mode=form.elements.batch_environment_mode.value;changes.conda_env=mode==='common'?form.elements.batch_conda_env.value.trim():'';changes.server_conda_envs={};
   if(mode==='servers')for(const s of state.servers)changes.server_conda_envs[s.id]=form.elements['batch_env_'+s.id].value||'';
  }else if(key==='server_ids')changes[key]=[...form.querySelectorAll('[name=batch_server_ids]:checked')].map(v=>v.value);
  else if(key==='datasets')changes[key]=form.elements.batch_datasets.value.split(/[,，]/).map(v=>v.trim()).filter(Boolean);
  else {const value=form.elements['batch_'+key].value.trim();changes[key]=['priority','memory_mb','max_utilization','max_retries'].includes(key)&&value!==''?Number(value):value;}
 }
 return changes;
}
function importPolicyRefresh(){
 const form=$('#batch-import-form');if(!form?.querySelector('.import-policy')||!batchDialog)return;
 const busy=!!batchDialog.job||!!batchDialog.configuring;
 for(const toggle of form.querySelectorAll('[data-import-field]')){
  toggle.disabled=busy;const group=$(`[data-import-controls="${toggle.dataset.importField}"]`,form);group.querySelectorAll('input,select,button').forEach(el=>el.disabled=busy||!toggle.checked);group.closest('section').classList.toggle('enabled',toggle.checked);
 }
 const mode=form.elements.batch_environment_mode.value;$('[data-import-env-common]',form).hidden=mode!=='common';$('[data-import-env-servers]',form).hidden=mode!=='servers';
 batchDialog.changes=importPolicyRead();const dirty=batchDialog.plan&&importPolicySignature(batchDialog.changes)!==importPolicySignature(batchDialog.plan.changes||{});
 const note=$('.import-config-note',form);note.textContent=dirty?'配置已修改，先点击“应用配置到预览”，核对后再创建。':batchDialog.plan?'预览包含当前覆盖配置，确认后创建草稿。':'勾选字段后读取，覆盖配置会直接用于首次预览。';
 const confirm=$('[data-action=batch-confirm]');if(confirm)confirm.disabled=busy||!!dirty;
 const apply=$('[data-action=import-config-apply]');if(apply)apply.disabled=busy||!dirty;
}
const importConfigBaseForm=batchForm;
batchForm=function(project){importConfigBaseForm(project);if(!state.runtime_features?.import_configuration)return;const form=$('#batch-import-form');if(!form)return;$('.form-footer',form).insertAdjacentHTML('beforebegin',importPolicyMarkup(batchDialog.changes||batchDialog.plan?.changes||{}));batchBusy(!!batchDialog.job||!!batchDialog.configuring);importPolicyRefresh();styleControls();};
const importConfigBasePreview=batchPreview;
batchPreview=function(plan){importConfigBasePreview(plan);if(!state.runtime_features?.import_configuration)return;const actions=$('.batch-preview-actions');actions?.insertAdjacentHTML('afterbegin',button('import-config-apply','应用配置到预览','','small'));importPolicyRefresh();};
const importConfigBaseBusy=batchBusy;
batchBusy=function(busy){importConfigBaseBusy(busy);importPolicyRefresh();};
const importConfigBaseSubmit=submitForm;
submitForm=async function(form){
 if(form.id!=='batch-import-form'||!state.runtime_features?.import_configuration)return importConfigBaseSubmit(form);
 const b=batchDialog;if(b.job)return;const changes=importPolicyRead();b.project=form.elements.project_id.value;b.source=form.elements.source_path.value;b.changes=changes;b.plan=null;b.committing=false;b.configuring=true;batchBusy(true);
 try{const result=await api(`/api/projects/${b.project}/batch-import/preview`,{source_path:b.source,changes});b.job=result.id;if(batchDialog===b&&dialogKind==='batch-import')await pollBatch();}
 finally{b.configuring=false;if(batchDialog===b&&dialogKind==='batch-import'){batchBusy(!!b.job);importPolicyRefresh();}}
};
const importConfigBaseAction=handleAction;
handleAction=async function(el){
 if(!state.runtime_features?.import_configuration)return importConfigBaseAction(el);
 if(el.dataset.action==='import-config-apply'){
  const b=batchDialog;if(!b?.plan||b.job||b.configuring)return;const changes=importPolicyRead();b.configuring=true;batchBusy(true);
  try{const plan=await api(`/api/batch-imports/${b.plan.id}/configure`,{changes,configuration_revision:b.plan.configuration_revision});b.plan=plan;b.changes=plan.changes;if(batchDialog===b&&dialogKind==='batch-import'){batchPreview(plan);toast('本批配置预览已更新，无需重新扫描文件');}}
  finally{b.configuring=false;if(batchDialog===b&&dialogKind==='batch-import')batchBusy(!!b.job);}return;
 }
 if(el.dataset.action==='batch-confirm'){
  const b=batchDialog;if(!b?.plan||b.job||b.configuring)return;
  if(importPolicySignature(importPolicyRead())!==importPolicySignature(b.plan.changes||{}))throw Error('请先应用配置到预览，再确认创建');
  b.configuring=true;batchBusy(true);
  try{const result=await api(`/api/batch-imports/${b.plan.id}/commit`,{configuration_revision:b.plan.configuration_revision});if(result.status==='success'&&result.count!==undefined){toast('此批次已创建，无重复实验');if(dialogKind==='batch-import')closeDialogFor(el);await load();return;}b.job=result.id;b.committing=true;if(batchDialog===b&&dialogKind==='batch-import'){batchBusy(true);await pollBatch();}}
  finally{b.configuring=false;if(batchDialog===b&&dialogKind==='batch-import')batchBusy(!!b.job);}return;
 }
 return importConfigBaseAction(el);
};
document.addEventListener('change',event=>{if(event.target.closest('.import-policy'))importPolicyRefresh();});
document.addEventListener('input',event=>{if(event.target.closest('.import-policy'))importPolicyRefresh();});
