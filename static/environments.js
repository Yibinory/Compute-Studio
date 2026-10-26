'use strict';
const environmentBaseRender=render,environmentBaseLoad=load,environmentBaseAction=handleAction,environmentBaseSubmit=submitForm,environmentBaseServerForm=serverForm,environmentBaseTaskForm=taskForm,environmentBaseDetail=taskDetail;
const environmentDrafts=new Map(),environmentRequested=new Set();
function environmentOptions(server,current='',fallback='使用默认执行方式'){
  const entries=server?.conda?.environments||[],known=entries.some(e=>e.prefix===current);
  return `<option value="">${esc(fallback)}</option>`+(!known&&current?`<option value="${esc(current)}" title="${esc(current)}" selected>${esc(current.split('/').filter(Boolean).pop()||current)} · 当前配置</option>`:'')+entries.map(e=>`<option value="${esc(e.prefix)}" title="${esc(e.prefix)}" ${current===e.prefix?'selected':''}>${esc(e.name)}</option>`).join('');
}
function environmentHint(server){
  const c=server?.conda||{};
  if(c.status==='failed')return '读取失败：'+(c.error||'请检查连接和 Conda 路径');
  if(c.status==='ready')return `${c.environments.length} 个环境 · 读取于 ${fmt(c.checked_at)}`;
  return '尚未读取环境；环境列表来自已保存的服务器配置。';
}
function environmentRefresh(server){return button('conda-refresh','↻ 读取环境',`data-server="${esc(server.id)}"`,'small');}
function projectEnvironmentsPage(project){
  if(!environmentDrafts.has(project.id))environmentDrafts.set(project.id,{...project.server_conda_envs});
  const draft=environmentDrafts.get(project.id);
  return `<div class="environment-intro"><div class="environment-symbol">⌘</div><div><h2>同一个项目，适配每一台服务器</h2><p>实验未单独指定环境时，使用该项目在目标服务器上的默认环境。留空则继承服务器默认配置。</p></div></div><form id="project-environments-form" data-project="${esc(project.id)}"><div class="environment-matrix">${state.servers.map(s=>`<article class="card environment-row"><div><h3>${esc(s.name)}</h3><small class="muted">${esc(s.host)} · 服务器默认：${esc(s.default_conda_env||'默认执行方式')}</small></div><div><label>项目默认 Conda 环境<select name="env_${esc(s.id)}" data-environment-server="${esc(s.id)}">${environmentOptions(s,draft[s.id]||'','继承服务器默认环境')}</select></label><small class="environment-hint" data-environment-hint="${esc(s.id)}">${esc(environmentHint(s))}</small></div><div class="environment-row-actions">${environmentRefresh(s)}</div></article>`).join('')||empty('先添加服务器','服务器配置完成后，即可为项目选择各自的运行环境。')}</div><div class="environment-save"><small class="muted">配置用于之后启动的实验，已启动实验继续使用原环境。</small><button type="submit" class="primary" ${state.servers.length?'':'disabled'}>保存项目环境</button></div></form>`;
}
async function requestEnvironment(server,explicit=false){
  const key=JSON.stringify([server.id,server.host,server.port,server.username,server.conda_executable,server.credential_revision]);
  if(!explicit&&(server.conda?.status!=='unknown'||environmentRequested.has(key)))return;
  environmentRequested.add(key);
  try{await api(`/api/servers/${server.id}/environments/refresh`,{});if(explicit)toast('环境读取已提交，记录可在操作记录查看');await load(false);}
  catch(e){toast(e.message,true);}
}
function refreshEnvironmentControls(){
  document.querySelectorAll('[data-environment-server]').forEach(select=>{
    const s=serverBy(select.dataset.environmentServer);if(!s)return;
    const fingerprint=JSON.stringify(s.conda),current=select.value;
    if(select.dataset.environmentInventory!==fingerprint){select.innerHTML=environmentOptions(s,current,select.dataset.fallback||'继承服务器默认环境');select.dataset.environmentInventory=fingerprint;}
  });
  document.querySelectorAll('[data-environment-hint]').forEach(el=>{el.textContent=environmentHint(serverBy(el.dataset.environmentHint));});
  document.querySelectorAll('[data-action=conda-refresh]').forEach(el=>{
    if(el.dataset.pending)return;
    const active=state.jobs?.some(j=>j.operation==='discover_conda:'+JSON.stringify([el.dataset.server])&&['queued','running'].includes(j.status));
    el.disabled=!!active;el.title=active?'环境读取已提交，可在操作记录查看':'重新读取已保存服务器的 Conda 环境';
  });
}
serverForm=async function(id){
  await environmentBaseServerForm(id);const f=$('#server-form');if(!f)return;
  const s=serverBy(id),label=f.elements.benchmark_env.closest('label');
  label.outerHTML=`<label>GPU 基准测试环境<select name="benchmark_env" ${id?`data-environment-server="${esc(id)}"`:''} data-fallback="使用服务器默认 Python">${environmentOptions(s,s?.benchmark_env||'','使用服务器默认 Python')}</select></label><label class="full">服务器默认 Conda 环境<select name="default_conda_env" ${id?`data-environment-server="${esc(id)}"`:''} data-fallback="使用默认执行方式">${environmentOptions(s,s?.default_conda_env||'','使用默认执行方式')}</select><small>项目和实验未指定环境时使用此配置。</small></label><div class="full environment-selector-foot">${id?`<small class="environment-hint" data-environment-hint="${esc(id)}">${esc(environmentHint(s))}</small>${environmentRefresh(s)}`:'<small class="muted">先保存服务器，再进入编辑页面选择环境。</small>'}<p class="muted">修改 SSH 连接或 Conda 可执行文件后，请先保存服务器再读取环境列表。</p></div>`;
  refreshEnvironmentControls();if(s)requestEnvironment(s);
};
taskForm=function(project,task){environmentBaseTaskForm(project,task);const input=$('#task-form [name=conda_env]');if(input){input.placeholder='留空使用项目 / 服务器默认环境';input.closest('label').insertAdjacentHTML('beforeend','<small>单独指定时优先于项目和服务器默认配置。</small>');}};
taskDetail=async function(id){
  await environmentBaseDetail(id);
  if(!$('#dialog').open||dialogKind!=='task-detail'||selectedTask!==id)return;
  const task=state.tasks.find(t=>t.id===id),grid=$('#dialog-body .info-grid');if(!task||!grid)return;
  grid.querySelector('.environment-inspection')?.remove();
  const source={task:'实验配置',task_server:'实验按服务器配置',project:'项目默认',server:'服务器默认',shell:'默认执行方式'}[task.conda_env_source];
  const configured=Object.entries(task.server_conda_envs||{}).map(([sid,value])=>`<span>${esc(serverBy(sid)?.name||sid)}<code>${esc(value||'继承项目／服务器默认环境')}</code></span>`).join('');
  const actual=task.resolved_conda_env!==undefined;
  grid.insertAdjacentHTML('beforeend',`<div class="environment-inspection"><small>${actual?'本次运行环境':'实验环境配置'}${source?' · '+source:''}</small><code class="break">${esc(actual?(task.resolved_conda_env||'默认执行方式'):task.conda_env|| (configured?'按执行服务器选择下方配置':'启动时继承项目 / 服务器配置'))}</code>${configured?'<div class="task-environment-map">'+configured+'</div>':''}</div>`);
};

render=function(){environmentBaseRender();refreshEnvironmentControls();if(route()[0]==='project'&&projectTab==='environments')for(const s of state.servers)requestEnvironment(s);};
load=async function(...args){await environmentBaseLoad(...args);refreshEnvironmentControls();};
handleAction=async function(el){if(el.dataset.action==='conda-refresh'){await requestEnvironment(serverBy(el.dataset.server),true);return;}return environmentBaseAction(el);};
submitForm=async function(form){if(form.id!=='project-environments-form')return environmentBaseSubmit(form);const values={};for(const s of state.servers)values[s.id]=form.elements['env_'+s.id].value;await api(`/api/projects/${form.dataset.project}/environments`,{server_conda_envs:values});environmentDrafts.delete(form.dataset.project);toast('项目默认环境已保存');await load();};
document.addEventListener('change',e=>{if(e.target.form?.id==='project-environments-form'){const project=e.target.form.dataset.project;environmentDrafts.get(project)[e.target.dataset.environmentServer]=e.target.value;}});
