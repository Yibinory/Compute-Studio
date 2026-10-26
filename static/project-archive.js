'use strict';
let projectsShowArchived=false;
try{projectsShowArchived=sessionStorage.getItem('compute-show-archived-projects')==='true';}catch{}
function projectsArchivedVisibility(show){projectsShowArchived=show;try{sessionStorage.setItem('compute-show-archived-projects',String(show));}catch{}render();styleControls();}
const projectArchiveBasePage=projectsPage;
projectsPage=function(){
 if(!state.runtime_features?.project_archive)return projectArchiveBasePage();
 const all=state.projects||[],archived=all.filter(p=>p.archived).length,visible=all.filter(p=>projectsShowArchived||!p.archived);
 let html=projectArchiveBasePage(visible);
 if(all.length&&!visible.length)html=html.slice(0,html.indexOf('<div class="empty">'))+empty('暂无未归档项目','项目已保留在归档中，开启显示后即可查看或取消归档。',button('projects-show-archived','显示归档项目','','primary'));
 const controls=`<div class="project-archive-toolbar"><span>未归档 <b>${all.length-archived}</b> · 已归档 <b>${archived}</b></span><label><input type="checkbox" data-project-archive-visibility ${projectsShowArchived?'checked':''}>显示归档项目</label></div>`;
 return html.replace(/(<div class="grid projects-grid">|<div class="empty">)/,controls+'$1');
};
const projectArchiveBaseAction=handleAction;
handleAction=async function(el){
 if(el.dataset.action==='project-archive'){
  const archived=el.dataset.archived==='true';await api(`/api/projects/${el.dataset.id}/archive`,{archived});toast(archived?'项目已归档，可开启“显示归档项目”查看':'已取消归档，项目已恢复显示');await load();return;
 }
 if(el.dataset.action==='projects-show-archived'){projectsArchivedVisibility(true);return;}
 return projectArchiveBaseAction(el);
};
const projectArchiveBaseRender=render;
render=function(){projectArchiveBaseRender();if(!state.runtime_features?.project_archive)return;const [page,id]=route(),project=projectBy(id);if(page==='project'&&project?.archived)$('#main')?.insertAdjacentHTML('afterbegin',`<aside class="project-archive-banner"><div><strong>此项目已归档</strong><p>归档仅整理项目空间的显示；实验调度、文件和结果继续保留。</p></div>${button('project-archive','取消归档',`data-id="${id}" data-archived="false"`,'small')}</aside>`);};
document.addEventListener('change',event=>{if(event.target.matches('[data-project-archive-visibility]'))projectsArchivedVisibility(event.target.checked);});
