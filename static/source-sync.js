'use strict';
const sourceStatus={managed:'受管目录',waiting:'等待首次同步',checking:'后台检查中',ready:'已同步',paused:'自动同步已暂停',error:'同步暂缓'};
const sourceDefaults=['runs/**','outputs/**','datasets/**','weights/**','checkpoints/**','experiment_inputs/**','analysis/**','batch_import/**'];
const sourceFor=id=>(state.code_sources||[]).find(s=>s.id===id);
const sourceBaseFilesPage=projectFilesPage,sourceBaseAction=handleAction,sourceBaseSubmit=submitForm;
function sourcePanel(project){
 const s=sourceFor(project.id),bound=!!s?.path;
 return `<section class="card source-card"><div class="card-head"><div><small class="source-eyebrow">CODE SOURCE</small><h3>源码与自动同步</h3></div><span class="badge ${s?.status==='error'?'amber':bound?'blue':''}">${esc(sourceStatus[s?.status]||'受管目录')}</span></div><p class="source-path" title="${esc(s?.path||'')}">${bound?esc(s.path):'未配置外部路径，使用本机保存目录下 projects / 项目 ID / code'}</p><p class="muted">${bound?`每 ${s.interval_seconds} 秒后台检查，文件稳定后按差异发布。最后同步：${s.synced_at?fmt(s.synced_at):'尚未同步'}${s.revision?' · 版本 '+esc(s.revision.slice(0,8)):''}`:'在此目录手动修改的代码，会在下次实验准备时重新核验。也可以绑定平时编辑或 AI 修改的本机文件夹。'}</p>${s?.error?`<p class="error-text">${esc(s.error)}</p>`:''}<div class="source-safety"><b>运行中的实验固定使用启动时的输入版本</b><span>源码更新用于后续运行；服务器按需补传 hash，不覆盖活动工作区。任务专属快照默认优先，需主动设置跟随规则才能更新同名代码。</span></div><div class="actions">${button('source-config','配置源码路径',`data-project="${project.id}"`,'primary small')}${bound?button('source-sync-now','立即同步',`data-project="${project.id}"`,'small')+button('source-toggle',s.enabled?'暂停自动同步':'恢复自动同步',`data-project="${project.id}"`,'small'):''}${button('source-follow','批量设置代码跟随',`data-project="${project.id}"`,'small')}${button('source-history','同步记录',`data-project="${project.id}"`,'small')}<a href="#events">后台操作 →</a></div></section>`;
}
projectFilesPage=function(project,tasks){return (state.runtime_features?.code_sync?sourcePanel(project):'')+sourceBaseFilesPage(project,tasks);};
async function sourceConfiguration(id){
 const s=await api(`/api/projects/${id}/source`);
 modal('项目源码路径',`<form id="source-config-form" data-project="${id}"><p class="muted">读取控制器电脑的目录，单向同步到工具公共代码副本；不会反向修改你的原文件。</p><label>本机源码文件夹（留空则使用受管 code 目录）<input name="path" value="${esc(s.path||'')}" placeholder="D:\\MyCode\\my-project"></label><div class="form-grid"><label>后台检查间隔（秒）<input type="number" name="interval_seconds" value="${s.interval_seconds||10}" min="5" max="3600" required></label><label class="checkbox"><input type="checkbox" name="enabled" ${s.path?s.enabled?'checked':'':'checked'}>启用自动同步</label><label class="full">排除目录／文件（每行一个 glob）<textarea name="exclude" rows="5">${esc((s.exclude||sourceDefaults).join('\n'))}</textarea></label></div><div class="notice">首次同步会让公共副本与来源目录（排除规则后）一致；原公共文件保留 hash 历史。源目录失联或为空时保留旧版本并阻止使用未核验代码启动。绑定后请在原目录编辑，避免与工具副本冲突。</div><p class="muted">连续修改多份文件前，可暂停自动同步，或在源码根目录放置 <code>.compute-sync.lock</code>；修改完成后删除锁文件，再同步。运行中的实验不受影响。</p>${footer('保存配置')}</form>`,'source-config');
}
function sourceFollowForm(pid){
 const tasks=state.tasks.filter(t=>t.project_id===pid&&['draft','queued','waiting','blocked','paused','failed','canceled'].includes(t.status));
 modal('批量设置代码跟随',`<form id="source-follow-form" data-project="${pid}"><p>选中的路径在每次新运行时优先使用项目当前代码，即使任务设置了 <code>code_files: []</code>，也会显式加入匹配文件。</p><div class="notice warning">只选择需要更新的源码／脚本。配置、拆分、数据和种子文件建议保留任务快照；不要随意用 ** 覆盖所有专属文件。未匹配的规则会阻止启动；运行中及已完成任务不在本次修改范围内。</div><label>跟随路径（每行一条；留空则恢复任务快照优先）<textarea name="patterns" rows="4" placeholder="src/**&#10;scripts/**&#10;*.py"></textarea></label><div class="source-task-list">${tasks.map(t=>`<label><input type="checkbox" name="task_ids" value="${t.id}"><span>${esc(t.name)}<small>${esc((t.follow_project_files||[]).join('、')||'当前：任务快照优先')}</small></span>${badge(t.status)}</label>`).join('')||'<p class="muted">当前没有可编辑实验；新批量配置可使用 follow_project_files 字段。</p>'}</div>${footer('保存选中实验的规则')}</form>`,'source-follow');
}
let sourceHistoryView=null;
async function sourceHistory(pid,revision=null,offset=0){
 if(!revision){
  const data=await api(`/api/projects/${pid}/source/history`);
  modal('代码同步记录',`<p class="muted">新增、修改和删除均记录前后 hash。记录对应已发布版本，原始源码目录从不被删除。</p><div class="source-history">${data.versions.map(v=>`<article><div><b>${esc(v.id.slice(0,8))}</b><small>${fmt(v.at)}</small></div><span>新增 ${v.added} · 修改 ${v.modified} · 删除 ${v.deleted}</span>${button('source-record','文件明细',`data-project="${pid}" data-revision="${v.id}"`,'small')}</article>`).join('')||'<p class="muted">尚无目录同步记录；未绑定时，实际使用的输入版本会记录在运行详情中。</p>'}</div>`,'source-history');return;
 }
 const v=await api(`/api/projects/${pid}/source/history/${revision}?offset=${offset}`);sourceHistoryView={pid,revision,offset,total:v.total};
 modal('同步文件明细 · '+revision.slice(0,8),`<p class="source-path">${esc(v.source)}</p><p class="muted">${fmt(v.at)} · ${v.total} 个变更 · 显示 ${offset+1}–${Math.min(offset+v.changes.length,v.total)}</p><div class="source-diffs">${v.changes.map(c=>`<article><span class="badge ${c.action==='deleted'?'amber':'blue'}">${({added:'新增',modified:'修改',deleted:'删除'})[c.action]}</span><div><b>${esc(c.path)}</b>${c.before?`<small title="${esc(c.before.sha256)}">原 hash ${esc(c.before.sha256.slice(0,16))} · ${size(c.before.size)}</small>`:''}${c.after?`<small title="${esc(c.after.sha256)}">新 hash ${esc(c.after.sha256.slice(0,16))} · ${size(c.after.size)}</small>`:''}</div></article>`).join('')||'<p>文件内容无变化，仅更新了来源指纹。</p>'}</div><div class="actions">${button('source-record-prev','上一页',offset?'':'disabled','small')}${button('source-record-next','下一页',offset+100<v.total?'':'disabled','small')}</div>`,'source-record');
}
handleAction=async function(el){
 const action=el.dataset.action,pid=el.dataset.project;
 if(action==='source-config'){await sourceConfiguration(pid);return;}
 if(action==='source-follow'){sourceFollowForm(pid);return;}
 if(action==='source-history'||action==='source-record'){await sourceHistory(pid,el.dataset.revision);return;}
 if(action==='source-record-prev'||action==='source-record-next'){const v=sourceHistoryView;await sourceHistory(v.pid,v.revision,Math.max(0,v.offset+(action.endsWith('prev')?-100:100)));return;}
 if(action==='source-sync-now'){await api(`/api/projects/${pid}/source/sync`,{});toast('同步已加入后台，可在操作记录查看或中断');await load();return;}
 if(action==='source-toggle'){const s=await api(`/api/projects/${pid}/source`);await api(`/api/projects/${pid}/source`,{path:s.path,enabled:!s.enabled,exclude:s.exclude,interval_seconds:s.interval_seconds});toast(s.enabled?'自动同步已暂停；新运行使用最后发布版本':'自动同步已恢复');await load();return;}
 return sourceBaseAction(el);
};
submitForm=async function(form){
 if(form.id==='source-config-form'){
  const fd=new FormData(form),result=await api(`/api/projects/${form.dataset.project}/source`,{path:fd.get('path'),enabled:fd.has('enabled'),interval_seconds:Number(fd.get('interval_seconds')),exclude:contentLines(fd.get('exclude'))});
  closeDialogFor(form);toast(result.job?'配置已保存，首次同步在后台进行':'配置已保存');await load();return;
 }
 if(form.id==='source-follow-form'){
  const fd=new FormData(form),ids=fd.getAll('task_ids');if(!ids.length)throw Error('请选择实验');
  await api('/api/tasks/scheduling',{task_ids:ids,changes:{follow_project_files:contentLines(fd.get('patterns'))}});closeDialogFor(form);toast('跟随规则已保存，下一次运行生效');await load();return;
 }
 return sourceBaseSubmit(form);
};
const sourceBaseJobs=jobsPanel;
jobsPanel=function(){return sourceBaseJobs().replace(/<article[^>]*data-job-id="([^"]+)"[\s\S]*?<\/article>/g,(html,id)=>{
 const job=state.jobs.find(j=>j.id===id);if(!job?.result?.record_id)return html;
 const r=job.result,pid=transferArgs(job)[0];
 return html.replace('<div class="actions">',`<p class="source-job-summary">版本 ${esc(r.revision.slice(0,8))} · 新增 ${r.added} · 修改 ${r.modified} · 删除 ${r.deleted}</p><div class="actions">${button('source-record','查看逐文件变更',`data-project="${pid}" data-revision="${r.record_id}"`,'small')}`);
});};
const sourceBaseTaskDetail=taskDetail;
taskDetail=async function(id){
 await sourceBaseTaskDetail(id);if(!state.runtime_features?.code_sync||!$('#dialog').open||dialogKind!=='task-detail'||selectedTask!==id)return;
 const t=state.tasks.find(v=>v.id===id);if(!t)return;
 $('#node-inspection')?.insertAdjacentHTML('afterbegin',`<section class="source-input-version"><b>运行输入版本</b><code title="${esc(t.input_revision||'')}">${esc(t.input_revision?.slice(0,16)||'该历史运行未记录版本')}</code><small>项目代码 ${esc(t.code_revision?.slice(0,8)||'—')} · ${t.follow_project_files?.length?'跟随：'+esc(t.follow_project_files.join('、')):'任务专属快照优先'}。本次已固定的输入不会随源码修改。</small></section>`);
};
