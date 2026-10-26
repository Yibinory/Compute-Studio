'use strict';
const resultBooks=new Map(),resultPageSize=40;
const resultDatasetSelections=new Map(),resultDatasetChoices=new Map();
function resultDataset(id){if(!resultDatasetSelections.has(id)){let value='';try{value=sessionStorage.getItem('compute-result-dataset:'+id)||'';}catch{}resultDatasetSelections.set(id,value);}return state.runtime_features?.result_dataset_tables?resultDatasetSelections.get(id):'';}
function resultBookKey(id,dataset){return dataset?id+':'+encodeURIComponent(dataset):id;}
function resultDatasetSelect(id,dataset){resultDatasetSelections.set(id,dataset);try{sessionStorage.setItem('compute-result-dataset:'+id,dataset);}catch{}}
function resultUrl(e){return `/api/projects/${e.id}/results`+(e.dataset?'?dataset='+encodeURIComponent(e.dataset):'');}
function resultDraftKey(e){return 'compute-result-draft:'+resultBookKey(e.id,e.dataset);}
const resultUid=()=>crypto.randomUUID().replaceAll('-','');
const resultClone=value=>JSON.parse(JSON.stringify(value));
function resultEntry(id){const dataset=resultDataset(id),key=resultBookKey(id,dataset);if(!resultBooks.has(key))resultBooks.set(key,{id,dataset,loaded:false,loading:false,doc:null,version:0,dirty:false,error:'',conflict:false,view:'summary',page:0,query:'',groupFilter:null,expanded:new Set()});return resultBooks.get(key);}
function resultLocal(e){try{if(e.dirty)sessionStorage.setItem(resultDraftKey(e),JSON.stringify({doc:e.doc,pending:e.pending,version:e.version}));else sessionStorage.removeItem(resultDraftKey(e));e.backupFailed=false;}catch{e.backupFailed=true;}}
function resultActive(id,dataset){return route()[0]==='project'&&route()[1]===id&&projectTab==='results'&&(dataset===undefined||resultDataset(id)===dataset);}
function resultStatus(e){const node=$('[data-result-save-status]');if(!resultActive(e.id,e.dataset)||!node)return;node.textContent=e.conflict?'版本冲突，草稿保留':e.error?'未保存：'+e.error:e.saving?'正在保存…':e.dirty?'有修改，等待保存…':e.doc?.updated_at?'已保存 '+fmt(e.doc.updated_at):'尚无记录';node.classList.toggle('error-text',!!e.error);const alert=$('.result-save-alert');if(alert){alert.hidden=!e.error;$('.result-save-error',alert).textContent=e.error+(e.backupFailed?' 浏览器草稿备份不可用，请导出当前草稿。':' 本页草稿仍保留，可重试或导出。');}}
async function resultLoad(id,force=false){
 const e=resultEntry(id);if(e.loading||e.loaded&&!force)return;e.loading=true;
 try{
  const [server,choices]=await Promise.all([api(resultUrl(e)),state.runtime_features?.result_dataset_tables?api(`/api/projects/${id}/results/datasets`):Promise.resolve(null)]);if(choices)resultDatasetChoices.set(id,[...new Set([...(resultDatasetChoices.get(id)||[]),...choices.datasets,e.dataset].filter(Boolean))]);delete server.summary;e.doc=server;e.loaded=true;e.error='';e.conflict=false;e.dirty=false;e.pending=null;
  if(!force){try{const saved=JSON.parse(sessionStorage.getItem(resultDraftKey(e))||'null');if(saved?.doc&&Array.isArray(saved.doc.rows)&&Array.isArray(saved.doc.columns)){
   const ownRequest=saved.pending?.body?.request_id===server.request_id;
   if(ownRequest&&saved.version===saved.pending.version){sessionStorage.removeItem(resultDraftKey(e));}
   else {e.doc=saved.doc;e.version=saved.version||1;e.pending=saved.pending;e.dirty=true;
    if(e.doc.revision===server.revision||ownRequest){e.doc.revision=server.revision;if(ownRequest)e.pending=null;}
    else {e.conflict=true;e.error='结果表已被其他页面更新，已恢复本页草稿。请导出草稿后重新载入。';}
   }
  }}catch{}}
  if(force){e.version=0;resultLocal(e);}
  if(resultActive(id,e.dataset)){render();styleControls();}
  if(e.dirty&&!e.conflict&&!e.error)resultSchedule(e);
 }catch(error){e.error=error.message;if(resultActive(id,e.dataset)){$('.project-results')?.replaceChildren();const target=$('.project-results');if(target)target.innerHTML=`<p class="error-text">${esc(error.message)}</p>${button('results-reload','重新载入',`data-project="${id}"`,'small')}`;}}
 finally{e.loading=false;}
}
function resultSchedule(e){clearTimeout(e.timer);e.timer=setTimeout(()=>resultSave(e),800);}
function resultTouch(e){e.version++;e.dirty=true;if(!e.conflict)e.error='';clearTimeout(e.backupTimer);e.backupTimer=setTimeout(()=>resultLocal(e),200);resultStatus(e);resultSchedule(e);}
async function resultSave(e){
 if(e.saving)return e.saving;if(!e.loaded||!e.dirty||e.conflict)return;
 clearTimeout(e.timer);
 e.saving=(async()=>{
  while(e.dirty&&!e.conflict){
   if(!e.pending)e.pending={version:e.version,body:{dataset:e.dataset,revision:e.doc.revision,request_id:resultUid(),columns:resultClone(e.doc.columns),rows:resultClone(e.doc.rows)}};
   resultLocal(e);resultStatus(e);
   try{
    const saved=await api(resultUrl(e),e.pending.body),version=e.pending.version;e.pending=null;e.doc.revision=saved.revision;e.doc.updated_at=saved.updated_at;e.error='';
    if(e.version===version){e.doc=saved;e.dirty=false;}resultLocal(e);
   }catch(error){e.error=error.message;e.conflict=/其他页面更新/.test(error.message);if(!/连接已中断|请求尚未返回|无法读取/.test(error.message))e.pending=null;resultLocal(e);break;}
  }
 })();resultStatus(e);
 try{await e.saving;}finally{e.saving=null;resultStatus(e);}
}
function resultTask(row){return state.tasks.find(t=>t.id===row.task_id)||row.task_snapshot;}
function resultTaskTitle(row){const t=resultTask(row);return t?'#'+t.seq+' '+t.name:'选择关联实验';}
function resultNumber(value,col){if(value==null)return '—';const n=Number(value);if(!Number.isFinite(n))return String(value);return n!==0&&Math.abs(n)<10**(-col.precision)?n.toExponential(2):n.toLocaleString('en-US',{minimumFractionDigits:col.precision,maximumFractionDigits:col.precision,useGrouping:false});}
function resultGroups(doc){
 const groups=new Map();for(const row of doc.rows){const key=row.group?'g:'+row.group:'r:'+row.id;if(!groups.has(key))groups.set(key,{key,name:row.group||'未分组记录',rows:[],values:{}});groups.get(key).rows.push(row);}
 for(const g of groups.values())for(const c of doc.columns){const values=g.rows.map(r=>r.values[c.id]).filter(v=>v!=null&&v!=='');if(c.type==='text')g.values[c.id]={text:[...new Set(values.map(String))],n:values.length};else {const numbers=values.map(Number).filter(Number.isFinite);let mean=0,m2=0;for(let i=0;i<numbers.length;i++){const delta=numbers[i]-mean;mean+=delta/(i+1);m2+=delta*(numbers[i]-mean);}g.values[c.id]={n:numbers.length,mean:numbers.length?mean:null,std:numbers.length>1?Math.sqrt(m2/(numbers.length-1)):null};}}
 return [...groups.values()];
}
function resultMatches(row,query){return !query||(row.group+' '+row.seed+' '+resultTaskTitle(row)).toLocaleLowerCase().includes(query);}
function resultRowMarkup(e,row){
 const current=state.tasks.find(t=>t.id===row.task_id),outdated=current&&current.attempt_id&&current.attempt_id!==row.attempt_id;
 return `<tr class="result-record" data-result-row="${row.id}"><td><button class="result-task-picker" data-action="results-link" data-project="${e.id}" data-row="${row.id}" title="${esc(resultTaskTitle(row))}">${esc(resultTaskTitle(row))}<span>⌄</span></button><div class="result-provenance">${current?button('task-detail','详情',`data-id="${current.id}"`,'small'):''}${row.attempt_id?`<code title="关联运行 ${esc(row.attempt_id)}">${esc(row.attempt_id.slice(0,8))}</code>`:''}${outdated?`<span title="实验已有其他运行；此行保留原关联，请按需重新关联">已有新运行</span>`:''}</div></td><td><input data-result-field="group" value="${esc(row.group)}" placeholder="方法 / 数据集 / 设置" aria-label="实验组别"></td><td><input data-result-field="seed" value="${esc(row.seed)}" placeholder="—" aria-label="Seed"></td>${e.doc.columns.map(c=>`<td><input data-result-metric="${c.id}" value="${esc(row.values[c.id]??'')}" ${c.type==='number'?'inputmode="decimal"':''} placeholder="—" aria-label="${esc(c.name)}"></td>`).join('')}<td><textarea rows="1" data-result-field="notes" placeholder="记录观察、条件或结论" aria-label="备注">${esc(row.notes)}</textarea></td><td>${button('results-delete-row','移除',`data-project="${e.id}" data-row="${row.id}"`,'small result-delete')}</td></tr>`;
}
function resultSummaryMarkup(e,groups,comparison=groups){
 const best={};for(const c of e.doc.columns)if(c.type==='number'&&c.direction!=='none'){const values=comparison.map(g=>g.values[c.id].mean).filter(v=>v!=null&&Number.isFinite(v));if(values.length>1)best[c.id]=(c.direction==='min'?Math.min:Math.max)(...values);}
 return groups.map(g=>{
  const tasks=[...new Map(g.rows.filter(r=>r.task_id).map(r=>[r.task_id,r])).values()],seeds=g.rows.map(r=>r.seed).filter(Boolean),notes=[...new Set(g.rows.map(r=>r.notes).filter(Boolean))],expanded=e.expanded.has(g.key);
  return `<tr class="result-group"><td><div class="result-task-links">${tasks.slice(0,2).map(r=>state.tasks.some(t=>t.id===r.task_id)?button('task-detail',esc(resultTaskTitle(r)),`data-id="${r.task_id}" title="${esc(resultTaskTitle(r))}"`,'small'):esc(resultTaskTitle(r))).join('')||'<span class="muted">尚未关联</span>'}${tasks.length>2?`<small>共 ${tasks.length} 个实验，展开查看</small>`:''}</div></td><td><strong>${esc(g.name)}</strong><small>${g.rows.length} 条记录${seeds.length===g.rows.length?'':' · Seed 未标齐'}</small></td><td><span class="result-seed-count">${seeds.length?new Set(seeds).size+' seeds':'—'}</span><small title="${esc(seeds.join(', '))}">${esc(seeds.slice(0,4).join(' / '))}${seeds.length>4?' …':''}</small></td>${e.doc.columns.map(c=>{const v=g.values[c.id],isBest=best[c.id]!=null&&v.mean===best[c.id];return `<td class="result-stat ${isBest?'result-best':''}">${c.type==='number'?`<strong>${esc(resultNumber(v.mean,c))}</strong>${v.std!=null?`<span class="result-std"> ± ${esc(resultNumber(v.std,c))}</span>`:''}${v.n!==g.rows.length?`<small>有效 n=${v.n}/${g.rows.length}</small>`:''}`:`<span title="${esc(v.text.join(' / '))}">${esc(v.text.slice(0,3).join(' / ')||'—')}</span>`}</td>`;}).join('')}<td class="result-summary-notes"><span title="${esc(notes.join('\n\n').slice(0,1000))}">${esc(!notes.length?'—':notes.length===1?notes[0].slice(0,300):notes.length+' 条备注')}</span></td><td>${button('results-expand',expanded?'收起':'展开',`data-project="${e.id}" data-key="${esc(g.key)}"`,'small')}</td></tr>`+(expanded?g.rows.slice(0,20).map(r=>resultRowMarkup(e,r)).join('')+(g.rows.length>20?`<tr><td colspan="${e.doc.columns.length+5}">${button('results-group-rows','查看本组全部记录',`data-project="${e.id}" data-key="${esc(g.key)}"`,'small')}</td></tr>`:''):'');
 }).join('');
}
function resultDatasetBar(e){
 if(!state.runtime_features?.result_dataset_tables)return '';
 const names=[...new Set([...(resultDatasetChoices.get(e.id)||[]),...state.datasets.map(d=>d.name),...state.tasks.filter(t=>t.project_id===e.id).flatMap(t=>t.datasets||[]),e.dataset].filter(Boolean))].sort((a,b)=>a.localeCompare(b));
 return `<div class="result-dataset-bar"><label>当前数据集<select data-result-dataset data-project="${e.id}" aria-label="结果数据集"><option value="">未指定数据集（原有记录）</option>${names.map(name=>`<option value="${esc(name)}" ${name===e.dataset?'selected':''}>${esc(name)}</option>`).join('')}</select></label><div class="result-dataset-description"><strong>${esc(e.dataset||'未指定数据集')}</strong><small>每个数据集独立维护指标、记录与多 Seed 汇总</small></div><div class="actions">${button('results-new-dataset','＋ 其他数据集',`data-project="${e.id}"`,'small')}${!e.dataset?button('results-assign-dataset','为当前表指定数据集',`data-project="${e.id}"`,'small'):''}</div></div>`;
}
function resultPage(id){
 const e=resultEntry(id);if(!e.loaded)return `<section class="project-results"><div class="result-loading">正在读取项目结果表…</div></section>`;
 const groups=resultGroups(e.doc),query=e.query.trim().toLocaleLowerCase(),all=e.view==='summary'?groups.filter(g=>g.rows.some(r=>resultMatches(r,query))):e.doc.rows.filter(r=>(e.groupFilter===null||r.group===e.groupFilter)&&resultMatches(r,query));
 const pages=Math.max(1,Math.ceil(all.length/resultPageSize));e.page=Math.min(e.page,pages-1);const visible=all.slice(e.page*resultPageSize,(e.page+1)*resultPageSize);
 const headers=`<tr><th class="result-link-column">关联实验</th><th class="result-group-column">实验组别</th><th class="result-seed-column">Seed</th>${e.doc.columns.map(c=>`<th title="${esc(c.name)} · ${c.type==='number'?(c.direction==='min'?'越小越好':c.direction==='max'?'越大越好':'不比较方向'):'文本'}">${esc(c.name)}${c.unit?` <small>(${esc(c.unit)})</small>`:''}${c.type==='number'&&c.direction!=='none'?`<span class="result-direction">${c.direction==='min'?'↓':'↑'}</span>`:''}</th>`).join('')}<th class="result-notes-column">备注</th><th class="result-action-column">操作</th></tr>`;
 return `<section class="project-results"><div class="result-heading"><div><h2>结果整理</h2><p>同一组别整理相同方法、数据集与设置；各 Seed 独立记录。</p></div><div class="actions">${button('results-add-row','＋ 添加记录',`data-project="${id}"`,'primary')}${button('results-columns','＋ 添加 / 管理指标',`data-project="${id}"`,'result-metrics-button')}</div></div>${resultDatasetBar(e)}<div class="result-toolbar"><div class="view-switch">${button('results-view','汇总对比',`data-project="${id}" data-view="summary"`,e.view==='summary'?'active':'')}${button('results-view','逐次记录',`data-project="${id}" data-view="rows"`,e.view==='rows'?'active':'')}</div><input class="result-search" data-result-search value="${esc(e.query)}" placeholder="搜索组别、Seed 或实验" aria-label="搜索结果"><div class="result-count">${e.doc.columns.length} 个指标 · ${e.doc.rows.length} 条记录 · ${groups.length} 组</div><div class="actions"><a href="/api/projects/${id}/results/export?view=${e.view==='summary'?'summary':'rows'}&dataset=${encodeURIComponent(e.dataset)}" class="download-button small" data-action="results-export" data-project="${id}">导出 CSV</a>${button('results-save','保存',`data-project="${id}"`,'small')}${button('results-reload','重新载入',`data-project="${id}"`,'small')}</div></div><div class="result-save-line"><span data-result-save-status role="status"></span><span>${e.view==='summary'?'均值 ± 样本标准差（n−1）；空缺不参与统计，单次结果只显示数值。':'直接在表格录入，修改后自动保存。左侧可选择关联实验。'}</span></div><aside class="result-save-alert" hidden><p class="result-save-error"></p><div class="actions">${button('results-save','重试保存',`data-project="${id}"`,'small')}${button('results-draft-export','导出本页草稿',`data-project="${id}"`,'small')}${button('results-reload','重新载入',`data-project="${id}"`,'small')}</div></aside>${e.groupFilter!==null?`<p class="result-filter-note">仅查看：${esc(e.groupFilter)} ${button('results-clear-group','显示全部记录',`data-project="${id}"`,'small')}</p>`:''}${e.doc.rows.length?`<div class="result-scroll-hint">← 左右滑动表格查看全部指标 →</div><div class="result-table-scroll"><table class="result-table"><thead>${headers}</thead><tbody>${e.view==='summary'?resultSummaryMarkup(e,visible,all):visible.map(r=>resultRowMarkup(e,r)).join('')||`<tr><td colspan="${e.doc.columns.length+5}" class="muted">没有匹配的记录</td></tr>`}</tbody></table></div><div class="result-pagination"><span>${all.length} ${e.view==='summary'?'组':'条'} · 第 ${e.page+1} / ${pages} 页</span><div class="actions">${button('results-prev','上一页',`data-project="${id}" ${e.page?'':'disabled'}`,'small')}${button('results-next','下一页',`data-project="${id}" ${e.page+1<pages?'':'disabled'}`,'small')}</div></div>`:empty('把实验结果整理在一起','先管理指标列，再添加记录。可一次创建多个 Seed，随后为每行关联实验。',button('results-add-row','添加第一条记录',`data-project="${id}"`,'primary'))}</section>`;
}
function resultTaskOptions(id,current='',query=''){
 const tasks=state.tasks.filter(t=>t.project_id===id),needle=query.trim().toLocaleLowerCase(),matches=tasks.filter(t=>('#'+t.seq+' '+t.name).toLocaleLowerCase().includes(needle)).slice(0,300),selected=tasks.find(t=>t.id===current);
 if(selected&&!matches.some(t=>t.id===selected.id))matches.unshift(selected);
 return '<option value="">暂不关联实验</option>'+matches.map(t=>`<option value="${t.id}" ${t.id===current?'selected':''}>#${t.seq} ${esc(t.name)} · ${esc(labels[t.status]||t.status)}</option>`).join('');
}
function resultTaskSelect(id,current=''){return `<label class="full">关联实验（可选）<input data-result-task-search placeholder="搜索实验名称或 #编号" aria-label="搜索关联实验"><select name="task_id">${resultTaskOptions(id,current)}</select><small>来自当前项目；最多显示前 300 个匹配项。多个 Seed 可关联同一个实验，也可在表格左侧分别关联。</small></label>`;}
function resultDownload(name,text,type='application/json'){const link=document.createElement('a'),url=URL.createObjectURL(new Blob([text],{type}));link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
const resultsBaseProjectPage=projectPage;
projectPage=function(id){
 let html=resultsBaseProjectPage(id);if(!state.runtime_features?.result_tables||!projectBy(id))return html;
 html=html.replace(/(<div class="tabs">[\s\S]*?)(<\/div>)/,`$1${button('project-tab','结果整理','data-tab="results"',projectTab==='results'?'active':'')}$2`);
 if(projectTab==='results'){const start=html.indexOf('<div class="tabs">'),end=html.indexOf('</div>',start)+6;html=html.slice(0,end)+resultPage(id);html=html.replace(/<button data-action="new-task"[^>]*>[\s\S]*?<\/button>/,'');}
 return html;
};
const resultsBaseRender=render;
render=function(){resultsBaseRender();if(route()[0]==='project'&&projectTab==='results'&&state.runtime_features?.result_tables){const tabs=$('#main>.tabs'),active=tabs?.querySelector('[data-tab=results]');if(tabs&&active)tabs.scrollLeft=Math.max(0,active.offsetLeft-tabs.offsetLeft+active.offsetWidth-tabs.clientWidth+10);const e=resultEntry(route()[1]);if(!e.loaded)resultLoad(e.id);else resultStatus(e);}};
const resultsBaseAction=handleAction;
handleAction=async function(el){
 const action=el.dataset.action;if(!action?.startsWith('results-'))return resultsBaseAction(el);const id=el.dataset.project,e=resultEntry(id);if(!e.loaded&&action!=='results-reload')return;
 if(action==='results-new-dataset'||action==='results-assign-dataset'){
  const assign=action==='results-assign-dataset';
  modal(assign?'为当前结果表指定数据集':'选择其他数据集',`<form id="result-dataset-form" data-project="${id}" data-assign="${assign}"><p class="muted">${assign?'将当前未指定的指标和全部记录移入所选数据集，关联实验和运行 ID 保留。目标已有结果表时会拒绝覆盖。':'为数据集单独维护指标与结果。可选择已登记的数据集，也可直接填写名称，无需上传或重新执行实验。'}</p><label>数据集名称<input name="dataset_name" required maxlength="120" list="result-dataset-options" placeholder="例如 BraTS2018 或 Fundus"><datalist id="result-dataset-options">${(resultDatasetChoices.get(id)||[]).map(name=>`<option value="${esc(name)}">`).join('')}</datalist></label>${footer(assign?'指定归属并保留记录':'打开数据集结果表')}</form>`,'result-dataset');return;
 }
 if(action==='results-add-row'){modal('添加结果记录',`<form id="result-add-form" data-project="${id}"><div class="form-grid">${resultTaskSelect(id)}<label class="full">实验组别<input name="group" maxlength="200" placeholder="如：方法 A / 数据集 / test；同组按 Seed 汇总"></label><label class="full">Seed<input name="seeds" placeholder="单个 42，或多个 42, 43, 44；可留空"><small>多个 Seed 需要填写组别。创建后在表格中分别填写各次指标。</small></label></div>${footer('添加并录入指标')}</form>`,'result-add');return;}
 if(action==='results-link'){const row=e.doc.rows.find(r=>r.id===el.dataset.row);modal('关联实验',`<form id="result-link-form" data-project="${id}" data-row="${row.id}"><div class="form-grid">${resultTaskSelect(id,row.task_id)}</div><p class="muted">关联时记录实验当前的运行 ID；后续重跑不会覆盖本行结果。</p>${footer('保存关联')}</form>`,'result-link');return;}
 if(action==='results-columns'){modal('管理结果指标',`<form id="result-columns-form" data-project="${id}"><p class="muted">数值列支持均值与样本标准差；文本列保留原文。删除列会移除该列的手工记录值。</p><div class="result-metric-add"><div><strong>添加新指标</strong><small>当前 ${e.doc.columns.length} / 24 个指标；默认的两列可重命名。</small></div><label class="result-new-column">新增指标名称<input name="new_name" maxlength="80" placeholder="留空不新增；新增默认为数值列"></label><small>填写名称后点击下方“保存指标设置”，即可在表格中录入新指标。</small></div><div class="result-column-editor">${e.doc.columns.map(c=>`<section data-result-column="${c.id}"><label>指标名称<input name="name_${c.id}" value="${esc(c.name)}" required maxlength="80"></label><label>类型<select name="type_${c.id}"><option value="number" ${c.type==='number'?'selected':''}>数值</option><option value="text" ${c.type==='text'?'selected':''}>文本</option></select></label><label>比较方向<select name="direction_${c.id}">${[['max','越大越好'],['min','越小越好'],['none','不标最佳']].map(([v,l])=>`<option value="${v}" ${c.direction===v?'selected':''}>${l}</option>`).join('')}</select></label><label>小数位<input type="number" name="precision_${c.id}" value="${c.precision}" min="0" max="10" required></label><label>单位<input name="unit_${c.id}" value="${esc(c.unit)}" maxlength="40" placeholder="如 %、秒"></label><label class="result-column-remove"><input type="checkbox" name="remove_${c.id}">删除列及其值</label></section>`).join('')}</div>${footer('保存指标设置')}</form>`,'result-columns');return;}
 if(action==='results-delete-row'){modal('移除结果记录',`<p>将从结果表中移除此行手工记录。关联实验、运行文件及输出保留。</p><div class="actions">${button('close-dialog','取消')}${button('results-confirm-delete','移除此行',`data-project="${id}" data-row="${el.dataset.row}"`,'danger')}</div>`,'result-delete');return;}
 if(action==='results-confirm-delete'){e.doc.rows=e.doc.rows.filter(r=>r.id!==el.dataset.row);closeDialogFor(el);resultTouch(e);render();return;}
 if(action==='results-view'){e.view=el.dataset.view;e.page=0;e.groupFilter=null;render();return;}
 if(action==='results-expand'){e.expanded.has(el.dataset.key)?e.expanded.delete(el.dataset.key):e.expanded.add(el.dataset.key);render();return;}
 if(action==='results-group-rows'){const group=resultGroups(e.doc).find(g=>g.key===el.dataset.key);e.groupFilter=group.rows[0].group;e.view='rows';e.page=0;render();return;}
 if(action==='results-clear-group'){e.groupFilter=null;e.page=0;render();return;}
 if(action==='results-prev'||action==='results-next'){e.page+=action==='results-next'?1:-1;render();return;}
 if(action==='results-save'){await resultSave(e);if(e.error)toast(e.error,true);return;}
 if(action==='results-draft-export'){resultDownload('project-result-draft.json',JSON.stringify(e.doc,null,2));return;}
 if(action==='results-export'){await resultSave(e);if(e.error||e.dirty){toast('当前修改尚未保存，请先修正或使用“导出本页草稿”保留记录',true);return;}location.href=el.href;return;}
 if(action==='results-reload'){
  if(e.saving)await e.saving;
  if(e.dirty){modal('重新载入结果表',`<p>当前有尚未保存的修改。可先导出本页草稿；重新载入会使用服务器保存的版本。</p><div class="actions">${button('results-draft-export','导出本页草稿',`data-project="${id}"`)}${button('close-dialog','保留当前页面')}${button('results-confirm-reload','确认重新载入',`data-project="${id}"`,'primary')}</div>`,'result-reload');return;}
  await resultLoad(id,true);return;
 }
 if(action==='results-confirm-reload'){clearTimeout(e.timer);closeDialogFor(el);await resultLoad(id,true);return;}
};
const resultsBaseSubmit=submitForm;
submitForm=async function(form){
 if(form.id==='result-dataset-form'){
  if(form.dataset.saving)return;form.dataset.saving='true';
  try{
   const id=form.dataset.project,e=resultEntry(id),dataset=String(new FormData(form).get('dataset_name')).trim();if(!dataset)throw Error('请填写数据集名称');
   await resultSave(e);if(e.error||e.dirty)throw Error('当前结果尚未保存，请先处理保存问题再切换数据集。');
   if(form.dataset.assign==='true'){
    if(e.dataset)throw Error('请先切回未指定数据集的原结果表');
    const cached=resultBooks.get(resultBookKey(id,dataset));if(cached?.dirty)throw Error('目标数据集有未保存的本页草稿，请先切换并保存。');
    form.dataset.requestId=form.dataset.requestId||resultUid();
    await api(`/api/projects/${id}/results/assign-dataset`,{dataset,revision:e.doc.revision,request_id:form.dataset.requestId});
    clearTimeout(e.timer);e.loaded=false;e.doc=null;e.version=0;e.pending=null;resultLocal(e);resultBooks.delete(resultBookKey(id,dataset));
   }
   resultDatasetSelect(id,dataset);resultDatasetChoices.set(id,[...new Set([...(resultDatasetChoices.get(id)||[]),dataset])]);
   closeDialogFor(form);await resultLoad(id);const target=resultEntry(id);
   if(target.loaded&&!target.doc.revision){resultTouch(target);await resultSave(target);}
   render();styleControls();
  }finally{delete form.dataset.saving;}
  return;
 }
 if(!['result-add-form','result-link-form','result-columns-form'].includes(form.id))return resultsBaseSubmit(form);
 const e=resultEntry(form.dataset.project),fd=new FormData(form);
 if(form.id==='result-add-form'){
  const group=String(fd.get('group')).trim(),seeds=String(fd.get('seeds')).split(/[,，;；\s]+/).map(v=>v.trim()).filter(Boolean),task_id=fd.get('task_id')||null,task=state.tasks.find(t=>t.id===task_id);
  if(seeds.length>1&&!group)throw Error('多个 Seed 请填写实验组别，以便正确汇总');if(e.doc.rows.length+Math.max(1,seeds.length)>5000)throw Error('每张数据集结果表最多 5000 条记录');
  const normalized=seeds.map(s=>/^[+-]?\d{1,20}$/.test(s)?String(BigInt(s)):s);
  if(new Set(normalized).size!==normalized.length||group&&normalized.some(s=>e.doc.rows.some(r=>r.group===group&&r.seed===s)))throw Error('同一组内 Seed 不能重复');
  for(const seed of normalized.length?normalized:[''])e.doc.rows.push({id:resultUid(),group,seed,task_id,attempt_id:task?.attempt_id||null,values:{},notes:''});e.view='rows';e.page=Math.floor((e.doc.rows.length-1)/resultPageSize);e.query='';e.groupFilter=null;
 }else if(form.id==='result-link-form'){
  const row=e.doc.rows.find(r=>r.id===form.dataset.row),task=state.tasks.find(t=>t.id===fd.get('task_id'));row.task_id=task?.id||null;row.attempt_id=task?.attempt_id||null;row.task_snapshot=task?{id:task.id,name:task.name,seq:task.seq}:null;
 }else {
  const columns=[];for(const c of e.doc.columns)if(!fd.has('remove_'+c.id))columns.push({...c,name:String(fd.get('name_'+c.id)).trim(),type:fd.get('type_'+c.id),direction:fd.get('direction_'+c.id),precision:Number(fd.get('precision_'+c.id)),unit:String(fd.get('unit_'+c.id)).trim()});
  const newName=String(fd.get('new_name')).trim();if(newName)columns.push({id:resultUid(),name:newName,type:'number',direction:'max',precision:4,unit:''});if(!columns.length||columns.length>24)throw Error('请保留 1–24 个指标列');
  if(columns.some(c=>!c.name)||new Set(columns.map(c=>c.name.toLocaleLowerCase())).size!==columns.length)throw Error('指标名称不能留空或重复');
  const ids=new Set(columns.map(c=>c.id));for(const row of e.doc.rows){row.values=Object.fromEntries(Object.entries(row.values).filter(([id])=>ids.has(id)));for(const c of columns)if(c.type==='text'&&row.values[c.id]!=null)row.values[c.id]=String(row.values[c.id]);}e.doc.columns=columns;
 }
 closeDialogFor(form);resultTouch(e);render();styleControls();
};
document.addEventListener('input',event=>{
 const el=event.target;
 if(el.matches('[data-result-field],[data-result-metric]')){const e=resultEntry(route()[1]),row=e.doc?.rows.find(r=>r.id===el.closest('[data-result-row]')?.dataset.resultRow);if(!row)return;if(el.dataset.resultMetric)row.values[el.dataset.resultMetric]=el.value;else row[el.dataset.resultField]=el.value;resultTouch(e);}
 if(el.matches('[data-result-search]')){const e=resultEntry(route()[1]);e.query=el.value;e.page=0;clearTimeout(e.searchTimer);const pos=el.selectionStart;e.searchTimer=setTimeout(()=>{if(!el.isConnected)return;render();const input=$('[data-result-search]');input?.focus();input?.setSelectionRange(pos,pos);},160);}
 if(el.matches('[data-result-task-search]')){const form=el.form,select=form.elements.task_id,current=select.value;select.innerHTML=resultTaskOptions(form.dataset.project,current,el.value);}
});
window.addEventListener('pagehide',()=>{for(const e of resultBooks.values())if(e.dirty)resultLocal(e);});

document.addEventListener('change',async event=>{
 const select=event.target;if(!select.matches('[data-result-dataset]'))return;
 const id=select.dataset.project,e=resultEntry(id),dataset=select.value;select.disabled=true;
 try{
  await resultSave(e);if(e.error||e.dirty)throw Error('当前结果尚未保存，请先处理保存问题再切换数据集。');
  resultDatasetSelect(id,dataset);render();styleControls();
 }catch(error){select.value=e.dataset;toast(error.message,true);}
 finally{if(select.isConnected)select.disabled=false;}
});
