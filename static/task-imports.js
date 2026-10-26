'use strict';
const taskImportsBaseForm=taskForm,taskImportsBaseSubmit=submitForm,taskImportsBaseAction=handleAction,taskImportsBaseRender=render;
let batchDialog=null;
function bashPath(path){return "'"+path.replace(/'/g,"'\"'\"'")+"'";}
async function loadTaskScripts(form){
  const select=form.querySelector('[name=project_script]'),project=form.elements.project_id.value,task=form.dataset.task;
  const seq=Number(select.dataset.seq||0)+1;select.dataset.seq=seq;select.innerHTML='<option value="">正在读取脚本…</option>';select.disabled=true;
  try{
    const result=await api(`/api/projects/${project}/scripts${task?'?task_id='+task:''}`);
    if(!form.isConnected||Number(select.dataset.seq)!==seq)return;
    select.innerHTML='<option value="">选择已有 .sh（可选）</option>'+result.scripts.map(s=>`<option value="${esc(JSON.stringify([s.scope,s.path]))}">${esc(s.scope==='task'?'任务文件':'项目代码')} · ${esc(s.path)}</option>`).join('');
    const existing=state.tasks.find(t=>t.id===task);
    if(existing?.script_path&&!form.elements.script_upload.files.length){const value=JSON.stringify([existing.script_scope||'task',existing.script_path]);if(Array.from(select.options).some(o=>o.value===value))select.value=value;}
    $('#task-script-hint',form).textContent=result.scripts.length?'选择后自动填写 Bash 命令；也可直接编辑命令添加参数。':'项目中暂时没有 .sh，可选择本机脚本，或直接填写执行命令。';
  }catch(e){if(form.isConnected&&Number(select.dataset.seq)===seq){select.innerHTML='<option value="">脚本列表读取失败，可使用本机脚本</option>';$('#task-script-hint',form).textContent=e.message;}}
  finally{if(form.isConnected&&Number(select.dataset.seq)===seq){select.disabled=false;styleControls();}}
}
taskForm=function(project,task){
  taskImportsBaseForm(project,task);const form=$('#task-form');if(!form)return;
  const command=form.elements.command.closest('label');
  command.insertAdjacentHTML('beforebegin',`<section class="task-script-controls full"><div class="task-script-heading"><b>Shell 脚本</b><span>选择脚本后自动上传并使用 Bash 执行</span></div><div class="form-grid"><label>选择本机 .sh 文件<input type="file" name="script_upload" accept=".sh"></label><label>或选择项目已有脚本<select name="project_script"><option value="">正在读取脚本…</option></select></label></div><small id="task-script-hint">支持 UTF-8；上传时自动转换 Windows 换行。编辑实验时不重新选择会保留原脚本。</small></section>`);
  if(task)form.elements.project_id.disabled=true;
  loadTaskScripts(form);styleControls();
};
function batchForm(project){
  if(!state.projects.length){toast('请先创建项目');return;}
  project=project||($('#task-form')?.elements.project_id.value)||route()[1]||state.projects[0].id;
  const previous=batchDialog?.project===project?batchDialog:null;
  batchDialog=previous||{project,job:null,plan:null,polling:false};
  modal('批量导入实验',`<form id="batch-import-form"><div class="form-grid"><label>所属项目<select name="project_id">${state.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===project?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label><label>创建方式<input value="全部创建为草稿，检查后再入队" disabled></label><label class="full">本机实验文件夹地址<input name="source_path" required placeholder="D:\\Experiments\\AFA"><small>自动读取 experiments.json，或各子目录中的 experiment.json。配置读取和文件整理在后台进行。</small></label></div><details class="batch-format"><summary>配置示例与支持的字段</summary><p>脚本和 files 路径相对于配置文件。depends_on 使用本批次的 key，或本项目已有实验 ID。服务器可使用 server_names 或 server_ids。</p><pre>${esc(JSON.stringify({version:1,defaults:{level:'idle_only',priority:50,datasets:['Fundus']},experiments:[{key:'train',name:'train_01',script:'train.sh',args:['--epochs','50'],notes:'基线实验'},{key:'eval',name:'eval_01',script:'eval.sh',depends_on:['train']}]},null,2))}</pre><p>支持 Conda、显存、GPU 型号、优先级、利用率、重试次数、依赖、Notes 和任务专属 files。脚本与 command 选择一种；一次最多 500 个实验。</p></details>${footer('读取配置并预览')}</form><div id="batch-import-preview"></div>`,'batch-import');
  $('#batch-import-form [name=source_path]').value=batchDialog.source||'';
  if(batchDialog.plan)batchPreview(batchDialog.plan);
  if(batchDialog.job){batchBusy(true);pollBatch();}
}
function batchBusy(busy){const form=$('#batch-import-form');if(!form)return;for(const name of ['project_id','source_path'])form.elements[name].disabled=busy;const submit=form.querySelector('button[type=submit]');submit.disabled=busy;submit.dataset.operationBusy=String(busy);}
function batchPreview(plan){
  const names=new Map(plan.tasks.map(t=>[t.id,t.name]));
  $('#batch-import-preview').innerHTML=`<section class="batch-preview"><div class="card-head"><div><h3>${plan.count} 个实验 · 配置校验通过</h3><small>${esc(plan.configs.join('、'))} · 专属输入 ${size(plan.input_bytes)}</small></div></div><div class="batch-preview-scroll"><table><thead><tr><th>实验 / key</th><th>脚本 / 命令</th><th>前序</th><th>调度</th></tr></thead><tbody>${plan.tasks.map(t=>`<tr><td><b>${esc(t.name)}</b><small>${esc(t.key)}</small></td><td><code title="${esc(t.command)}">${esc(t.command)}</code><small>${t.input_count} 个输入文件${t.notes?' · Notes':''}</small></td><td>${t.depends_on.map(id=>esc(names.get(id)||state.tasks.find(t=>t.id===id)?.name||id)).join('、')||'—'}</td><td>${esc({idle_only:'空闲卡',low_interference:'低干扰',emergency:'紧急'}[t.level])} · 优先级 ${t.priority}<small>${t.memory_mb} MB · ${esc(Object.values(t.server_conda_envs||{}).some(Boolean)?'按服务器配置环境':t.conda_env||'项目 / 服务器默认环境')}</small></td></tr>`).join('')}</tbody></table></div><p class="muted">确认后一次性创建全部草稿。再次确认相同批次不会重复创建；来源在预览后发生变化时会要求重新预览。</p><div class="actions batch-preview-actions" data-dialog-actions-source data-dialog-summary="${plan.count} 个实验 · 配置校验通过">${button('batch-confirm','创建全部实验草稿',`data-id="${esc(plan.id)}"`,'primary')}${button('batch-repreview','重新读取配置','','small')}</div></section>`;
}
async function pollBatch(){
  const b=batchDialog;if(!b?.job||b.polling||!$('#dialog').open||dialogKind!=='batch-import')return;
  b.polling=true;
  try{
    const data=await api(`/api/jobs/${b.job}`),job=data.job;
    if(batchDialog!==b||!$('#dialog').open||dialogKind!=='batch-import'||!$('#batch-import-preview'))return;
    if(['failed','canceled'].includes(job.status)){
      $('#batch-import-preview').innerHTML=`<div class="notice warning">${esc(job.error||'导入失败')}</div>`;b.job=null;batchBusy(false);return;
    }
    if(job.status==='success'){
      b.job=null;batchBusy(false);
      if(b.committing){toast(`已创建 ${job.result.count} 个实验草稿`);$('#dialog').close();location.hash='project/'+b.project;await load();return;}
      b.plan=job.result;batchPreview(b.plan);return;
    }
    batchBusy(true);const p=job.progress||{};$('#batch-import-preview').innerHTML=`<div class="batch-loading"><b>${b.committing?'正在创建实验草稿…':'后台正在读取和校验配置…'}</b><small>${esc(p.path||'')} ${p.files_done||0}${p.files_total?' / '+p.files_total:''}</small><a href="#events">可在操作记录查看</a></div>`;
  }catch(e){if(batchDialog===b&&dialogKind==='batch-import'&&$('#batch-import-preview'))$('#batch-import-preview').textContent=e.message;}
  finally{b.polling=false;}
}
render=function(){
  taskImportsBaseRender();const head=$('#main .page-actions')||$('#main .page-title .actions');
  if(head&&$('#main [data-action=new-task]')&&!head.querySelector('[data-action=batch-import]'))head.insertAdjacentHTML('beforeend',button('batch-import','批量导入实验',route()[0]==='project'?`data-project="${esc(route()[1])}"`:''));
};
handleAction=async function(el){
  if(el.dataset.action==='batch-import'){batchForm(el.dataset.project);return;}
  if(el.dataset.action==='batch-repreview'){batchDialog.plan=null;$('#batch-import-preview').innerHTML='';return;}
  if(el.dataset.action==='batch-confirm'){
    const b=batchDialog,result=await api(`/api/batch-imports/${el.dataset.id}/commit`,{});
    if(result.status==='success'&&result.count!==undefined){toast('此批次已创建，无重复实验');$('#dialog').close();await load();return;}
    b.job=result.id;b.committing=true;batchBusy(true);await pollBatch();return;
  }
  return taskImportsBaseAction(el);
};
submitForm=async function(form){
  if(form.id==='batch-import-form'){
    const b=batchDialog;if(b.job)return;const fd=new FormData(form);b.project=fd.get('project_id');b.source=fd.get('source_path');b.plan=null;b.committing=false;
    const result=await api(`/api/projects/${b.project}/batch-import/preview`,{source_path:fd.get('source_path')});b.job=result.id;
    batchBusy(true);await pollBatch();return;
  }
  if(form.id!=='task-form')return taskImportsBaseSubmit(form);
  const fd=new FormData(form),data=Object.fromEntries(fd.entries()),file=form.elements.script_upload.files[0];
  delete data.script_upload;delete data.project_script;data.script_path=null;data.script_scope=null;
  data.depends_on=fd.getAll('depends_on');data.server_ids=fd.getAll('server_ids');data.datasets=data.datasets.split(/[,，]/).map(v=>v.trim()).filter(Boolean);
  if(form.elements.project_script.value&&!file){[data.script_scope,data.script_path]=JSON.parse(form.elements.project_script.value);}
  let payload=data;
  if(file){payload=new FormData();payload.append('config',JSON.stringify(data));payload.append('script',file,file.name);}
  const project=form.elements.project_id.value;
  await api(form.dataset.task?`/api/tasks/${form.dataset.task}/update`:`/api/projects/${project}/tasks`,payload);
  closeDialogFor(form);toast('实验配置和脚本已保存');await load();
};
document.addEventListener('change',e=>{
  if(e.target.form?.id==='batch-import-form'&&['project_id','source_path'].includes(e.target.name)&&batchDialog?.plan){batchDialog.plan=null;$('#batch-import-preview').innerHTML='<p class="muted">地址或项目已修改，请重新读取配置。</p>';}
  const form=e.target.form;if(form?.id!=='task-form')return;
  if(e.target.name==='project_id'){form.elements.script_upload.value='';loadTaskScripts(form);}
  if(e.target.name==='script_upload'){
    const file=e.target.files[0];if(!file)return;
    if(!file.name.toLowerCase().endsWith('.sh')||file.size>2*1024**2){toast('请选择不超过 2 MB 的 .sh 文件',true);e.target.value='';styleControls();return;}
    form.elements.project_script.value='';form.elements.command.value='bash -- '+bashPath(file.name);
    if(!form.elements.name.value)form.elements.name.value=file.name.replace(/\.sh$/i,'').replace(/[^\p{L}\p{N}_.-]+/gu,'_').slice(0,100);
  }
  if(e.target.name==='project_script'&&e.target.value){
    const [,path]=JSON.parse(e.target.value);form.elements.script_upload.value='';form.elements.command.value='bash -- '+bashPath(path);
    if(!form.elements.name.value)form.elements.name.value=path.split('/').pop().replace(/\.sh$/i,'').replace(/[^\p{L}\p{N}_.-]+/gu,'_').slice(0,100);styleControls();
  }
});
if(document.body.dataset.authorized==='true')setInterval(pollBatch,700);
