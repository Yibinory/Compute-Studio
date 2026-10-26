'use strict';
const studioBaseOverview=overview,studioBaseProjects=projectsPage;
toast=function(message,error=false){
  const root=$('#toasts');if(!root)return;
  for(const old of root.children)if(old.textContent===message)old.remove();
  while(root.children.length>=2)root.firstElementChild.remove();
  const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=message;root.append(el);setTimeout(()=>el.remove(),error?12000:4500);
};
overview=function(){
  const servers=state.servers||[],online=servers.filter(s=>s.connected).length,projects=state.projects||[];
  const hero=`<section class="studio-hero"><div class="studio-hero-copy"><span class="studio-kicker">${state.settings.demo?'演示工作空间':'研究工作空间'} <i></i> COMPUTE STUDIO</span><h2>把实验的每一步，连接起来。</h2><p>${online} 台服务器已连接 · ${projects.length} 个项目空间<br>代码跟随算力，结果回到项目。</p><div class="studio-hero-links"><a href="#projects">进入项目空间 <span>↗</span></a><a href="#benchmarks">了解 GPU 算力 <span>↗</span></a></div></div><div class="studio-hero-art"><div class="studio-orbit"></div><img src="/static/assets/compute-studio-logo.png" alt="" width="140" height="140"><span>CONNECTED COMPUTE</span></div></section>`;
  return studioBaseOverview().replace('<div class="stats">',hero+'<div class="stats">');
};
projectsPage=function(projects){return studioBaseProjects(projects).replace('<div class="grid">','<div class="grid projects-grid">');};
const studioIcons={
 overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
 projects:'M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
 tasks:'M6 3v12a4 4 0 0 0 4 4h8 M6 7h8a4 4 0 0 1 4 4v8 M3 3h6 M15 16l3 3 3-3',
 uploads:'M12 16V3 M7 8l5-5 5 5 M3 15v5a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-5',
 datasets:'M3 5c0-4 18-4 18 0s-18 4-18 0z M3 5v14c0 4 18 4 18 0V5 M3 12c0 4 18 4 18 0',
 benchmarks:'M4 18V9 M10 18V4 M16 18v-7 M22 21H2',
 servers:'M4 3h16v7H4z M4 14h16v7H4z M7 6h1 M7 17h1 M12 6h5 M12 17h5',
 settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
 events:'M8 5h13 M8 12h13 M8 19h13 M3 5h1 M3 12h1 M3 19h1'
};
document.querySelectorAll('[data-nav]').forEach(el=>{const d=studioIcons[el.dataset.nav];if(!d)return;const text=el.querySelector('span');el.innerHTML=`<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>${text.outerHTML}`;});
