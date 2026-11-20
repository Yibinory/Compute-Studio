'use strict';
toast=function(message,error=false){
  const root=$('#toasts');if(!root)return;
  for(const old of root.children)if(old.textContent===message)old.remove();
  while(root.children.length>=2)root.firstElementChild.remove();
  const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=message;root.append(el);setTimeout(()=>el.remove(),error?12000:4500);
};
function memoryOverview(gpus) {
  let used = 0, total = 0, samples = 0;
  for (const gpu of gpus) {
    const capacity = gpu.memory_total_mb, usage = gpu.memory_used_mb;
    if (!Number.isFinite(capacity) || !Number.isFinite(usage) || capacity <= 0 || usage < 0 || usage > capacity) continue;
    used += usage;
    total += capacity;
    samples++;
  }
  const percent = samples ? Math.round(100 * used / total) : 0;
  const gib = mb => (mb / 1024).toLocaleString('zh-CN', { maximumFractionDigits: 1 });
  const detail = samples
    ? `共 ${gib(total)} GiB · 剩余 ${gib(total - used)} GiB · 已采样 ${samples} / ${gpus.length} 张卡`
    : '等待在线服务器上报显存采样';
  return `<section class="studio-hero" aria-labelledby="pool-heading">
    <div class="pool-summary">
      <h2 id="pool-heading">GPU 显存占用</h2>
      <p class="pool-value">${samples ? gib(used) : '—'} <small>GiB</small></p>
      <p class="pool-detail">${detail}</p>
      <div class="pool-links"><a href="#projects">项目空间 <span aria-hidden="true">↗</span></a>
      <a href="#benchmarks">GPU 性能测试 <span aria-hidden="true">↗</span></a></div>
    </div>
    <div class="pool-visual"><div class="pool-ring" style="--used:${percent}%">
      <div><strong>${samples ? percent + '<small>%</small>' : '—'}</strong><span>显存占用率</span></div>
    </div></div>
  </section>`;
}
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
function studioIcon(name, className) {
  return `<svg class="${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${studioIcons[name]}"/></svg>`;
}
document.querySelectorAll('[data-nav]').forEach(link => {
  link.insertAdjacentHTML('afterbegin', studioIcon(link.dataset.nav, 'nav-icon'));
});
$('.skip-content')?.addEventListener('click', () => $('#main').focus());
