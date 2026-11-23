'use strict';
const figureColors=['#2867b2','#147a6f','#985789','#b36b2e','#5862ae','#387589'];
const figureModes=[['bar','柱状图'],['line','折线图'],['point','点图'],['box','箱线图']];
const figurePageSize=8;

function figureConfig(e,metrics){
 if(!e.chart)e.chart={mode:'bar',metrics:metrics.slice(0,3).map(c=>c.id),filter:'',deviation:true,page:0};
 if(e.chart.metrics.length&&!metrics.some(c=>e.chart.metrics.includes(c.id)))e.chart.metrics=metrics.slice(0,3).map(c=>c.id);
 return e.chart;
}
function figureTick(value){
 if(!value)return '0';
 return Math.abs(value)>=1e6||Math.abs(value)<.001?value.toExponential(1):Number(value.toPrecision(4)).toString();
}
function figureQuantile(sorted,fraction){
 const i=(sorted.length-1)*fraction,low=Math.floor(i),part=i-low;
 return sorted[low]*(1-part)+sorted[Math.ceil(i)]*part;
}
function figureSeries(groups,col,box){
 return groups.map(g=>{
  const stat=g.values[col.id];
  if(!stat.n||!Number.isFinite(stat.mean))return null;
  const point={name:g.name,mean:stat.mean,std:stat.std,n:stat.n};
  if(box){
   const values=g.rows.map(r=>r.values[col.id]).filter(v=>v!=null&&v!=='').map(Number).filter(Number.isFinite).sort((a,b)=>a-b);
   Object.assign(point,{low:values[0],q1:figureQuantile(values,.25),median:figureQuantile(values,.5),q3:figureQuantile(values,.75),high:values.at(-1)});
  }
  return point;
 });
}
function resultMetricFigure(col,color,groups,config){
 const box=config.mode==='box',bar=config.mode==='bar',series=figureSeries(groups,col,box);
 const header=`<header><div><i style="background:${color}"></i><h3>${esc(col.name)}</h3>${col.unit?`<span class="figure-unit">${esc(col.unit)}</span>`:''}</div><small>${box?'中位数与四分位':config.deviation?'均值 ± SD':'均值'}</small></header>`;
 const values=[];
 for(const point of series){
  if(!point)continue;
  if(box){values.push(point.low,point.high);continue;}
  values.push(point.mean);
  if(config.deviation&&Number.isFinite(point.std))values.push(point.mean-point.std,point.mean+point.std);
 }
 if(!values.length)return `<figure class="metric-figure">${header}<p class="figure-empty">这些组尚未填写此指标。</p></figure>`;
 // Normalize before mapping to pixels so a narrow or very large range stays drawable.
 const finite=values.filter(Number.isFinite),scale=Math.max(...finite.map(Math.abs))||1;
 const normalized=finite.map(v=>v/scale);
 let low=Math.min(...normalized),high=Math.max(...normalized);
 if(bar){low=Math.min(0,low);high=Math.max(0,high);}
 if(low===high){const pad=Math.abs(low)*.1||1;low-=pad;high+=pad;}
 let intervals=4;
 const rawStep=(high-low)*scale/4,magnitude=10**Math.floor(Math.log10(rawStep));
 if(Number.isFinite(rawStep)&&magnitude>0){
  const step=magnitude*[1,2,2.5,5,10].find(v=>v>=rawStep/magnitude)/scale;
  const start=Math.floor(low/step),end=Math.ceil(high/step);
  if(Number.isFinite(start)&&end-start>=1&&end-start<=8&&Number.isFinite(end*step*scale)){
   low=start*step;high=end*step;intervals=end-start;
  }
 }
 const width=500,left=66,right=16,top=20,bottom=179;
 const step=(width-left-right)/groups.length,x=i=>left+step*(i+.5),y=v=>bottom-(v/scale-low)/(high-low)*(bottom-top);
 const ticks=Array.from({length:intervals+1},(_,i)=>{
  const py=bottom-i*(bottom-top)/intervals,v=(low+(high-low)*i/intervals)*scale;
  return `<line class="figure-grid" x1="${left}" y1="${py}" x2="${width-right}" y2="${py}"/><text class="figure-tick" x="${left-9}" y="${py+4}" text-anchor="end">${esc(figureTick(v))}</text>`;
 }).join('');
 let path='',connected=false;
 const marks=series.map((p,i)=>{
  if(!p){connected=false;return '';}
  const cx=x(i),cy=y(p.mean),w=Math.min(39,step*.48);
  if(config.mode==='line'){path+=(connected?' L':' M')+cx+' '+cy;connected=true;}
  const fmt=v=>resultNumber(v,col);
  let shape='',info=`${p.name} · ${col.name} · 均值 ${fmt(p.mean)}${p.std!=null?' ± '+fmt(p.std):''} · n=${p.n}`;
  function whisker(a,b){
   if(!Number.isFinite(a)||!Number.isFinite(b))return '';
   return `<path d="M${cx} ${y(a)} V${y(b)} M${cx-4} ${y(a)} h8 M${cx-4} ${y(b)} h8" fill="none" stroke="${color}" stroke-width="1.6"/>`;
  }
  if(box){
   const h=Math.max(1,y(p.q1)-y(p.q3));
   shape=whisker(p.low,p.high)+`<rect x="${cx-w/2}" y="${y(p.q3)}" width="${w}" height="${h}" fill="white" stroke="${color}"/><rect x="${cx-w/2}" y="${y(p.q3)}" width="${w}" height="${h}" fill="${color}" fill-opacity=".19"/><path d="M${cx-w/2} ${y(p.median)} h${w}" stroke="${color}" stroke-width="2.5"/>`;
   info=`${p.name} · ${col.name} · 最小 ${fmt(p.low)} · Q1 ${fmt(p.q1)} · 中位数 ${fmt(p.median)} · Q3 ${fmt(p.q3)} · 最大 ${fmt(p.high)} · n=${p.n}`;
  }else{
   if(bar)shape=`<rect x="${cx-w/2}" y="${Math.min(y(0),cy)}" width="${w}" height="${Math.max(1,Math.abs(cy-y(0)))}" rx="2.5" fill="${color}" fill-opacity=".82"/>`;
   if(config.deviation&&p.std!=null)shape+=whisker(p.mean-p.std,p.mean+p.std);
   if(!bar)shape+=`<circle cx="${cx}" cy="${cy}" r="4.5" fill="${color}" stroke="white" stroke-width="1.5"/>`;
  }
  return `<g class="figure-mark" tabindex="0" role="img" aria-label="${esc(info)}" data-figure-hint="${esc(info)}"><title>${esc(info)}</title><rect x="${cx-step/2+1}" y="${top-4}" width="${step-2}" height="${bottom-top+9}" fill="transparent"/>${shape}</g>`;
 }).join('');
 const labels=groups.map((g,i)=>{
  const max=Math.max(3,Math.min(18,Math.floor(step/10))),chars=Array.from(g.name),short=chars.length>max?chars.slice(0,max-1).join('')+'…':g.name;
  return `<text class="figure-label" x="${x(i)}" y="${bottom+24}" text-anchor="middle"><title>${esc(g.name)}</title>${esc(short)}</text>`;
 }).join('');
 const svg=`<svg viewBox="0 0 ${width} 216" role="group" aria-label="${esc(col.name)} ${figureModes.find(([key])=>key===config.mode)[1]}">${ticks}${path?`<path d="${path}" stroke="${color}" stroke-width="2.3" stroke-linejoin="round" fill="none"/>`:''}${marks}${labels}</svg>`;
 return `<figure class="metric-figure">${header}<div class="figure-plot">${svg}</div><figcaption aria-live="polite">悬停或聚焦数据点可查看数值</figcaption></figure>`;
}
function resultChartPanel(e,groups){
 const metrics=e.doc.columns.filter(c=>c.type==='number');
 if(!metrics.length)return empty('尚无数值指标','在「添加 / 管理指标」中添加数值列，录入结果后即可绘图。','');
 const config=figureConfig(e,metrics);
 const needle=config.filter.trim().toLocaleLowerCase();
 const filtered=groups.filter(g=>!needle||g.name.toLocaleLowerCase().includes(needle)||g.rows.some(r=>resultMatches(r,needle)));
 const pages=Math.max(1,Math.ceil(filtered.length/figurePageSize));
 config.page=Math.max(0,Math.min(config.page,pages-1));
 const selected=metrics.filter(c=>config.metrics.includes(c.id)),visible=filtered.slice(config.page*figurePageSize,(config.page+1)*figurePageSize);
 const modeChoices=figureModes.map(([key,label])=>`<label><input type="radio" name="figure-mode" data-figure-mode value="${key}" ${config.mode===key?'checked':''}><span>${label}</span></label>`).join('');
 const chips=metrics.map(c=>`<label><input type="checkbox" data-figure-metric="${esc(c.id)}" ${config.metrics.includes(c.id)?'checked':''}><span>${esc(c.name)}</span></label>`).join('');
 const options=`<div class="figure-controls"><fieldset class="figure-modes"><legend>图形</legend>${modeChoices}</fieldset><label class="figure-search">筛选实验组<input data-figure-filter value="${esc(config.filter)}" placeholder="组名、行名或实验"></label><fieldset class="figure-metrics"><legend>显示指标</legend>${chips}</fieldset>${config.mode!=='box'?`<label class="figure-deviation"><input type="checkbox" data-figure-deviation ${config.deviation?'checked':''}>显示样本标准差</label>`:''}</div>`;
 let plots;
 if(!selected.length)plots=empty('选择要展示的指标','勾选上方任意数值指标。','');
 else if(!visible.length)plots=empty(groups.length?'没有匹配的实验组':'尚无结果记录',groups.length?'调整筛选条件以查看图表。':'添加记录并录入指标后，图表会自动生成。','');
 else plots=`<div class="result-figure-grid">${selected.map(col=>resultMetricFigure(col,figureColors[metrics.indexOf(col)%figureColors.length],visible,config)).join('')}</div>`;
 return `<section class="result-figures">${options}<div class="figure-context"><span>${config.mode==='box'?'箱体为 Q1–Q3，须线为最小和最大值。':'横轴按表内顺序排列，每张图使用对应指标的单位。'}</span><span>${filtered.length} 组 · ${selected.length} 项指标</span></div>${plots}${pages>1?`<div class="result-pagination"><span>第 ${config.page+1} / ${pages} 页</span><div class="actions">${button('results-chart-page','上一页',`data-project="${e.id}" data-step="-1" ${config.page?'':'disabled'}`,'small')}${button('results-chart-page','下一页',`data-project="${e.id}" data-step="1" ${config.page+1<pages?'':'disabled'}`,'small')}</div></div>`:''}</section>`;
}
function refreshResultFigures(el){
 const e=resultEntry(route()[1]),section=el.closest('.result-figures');
 if(section)section.outerHTML=resultChartPanel(e,resultGroups(e.doc));
}
document.addEventListener('change',event=>{
 const el=event.target;
 if(!el.matches('[data-figure-mode],[data-figure-metric],[data-figure-deviation]'))return;
 const config=resultEntry(route()[1]).chart;
 if(el.hasAttribute('data-figure-mode'))config.mode=el.value;
 if(el.hasAttribute('data-figure-metric')){
  const id=el.dataset.figureMetric;config.metrics=config.metrics.filter(key=>key!==id);if(el.checked)config.metrics.push(id);
 }
 if(el.hasAttribute('data-figure-deviation'))config.deviation=el.checked;
 const attr=el.getAttributeNames().find(name=>name.startsWith('data-figure-'));
 refreshResultFigures(el);
 document.querySelector(`[${attr}="${CSS.escape(el.getAttribute(attr))}"]${el.type==='radio'?':checked':''}`)?.focus({preventScroll:true});
});
document.addEventListener('input',event=>{
 const el=event.target;if(!el.matches('[data-figure-filter]'))return;
 const config=resultEntry(route()[1]).chart;config.filter=el.value;config.page=0;
 const cursor=el.selectionStart;clearTimeout(config.timer);
 config.timer=setTimeout(()=>{if(!el.isConnected)return;refreshResultFigures(el);const next=document.querySelector('[data-figure-filter]');next?.focus();next?.setSelectionRange(cursor,cursor);},140);
});
function resultFigureHint(event){
 const mark=event.target.closest?.('[data-figure-hint]');
 if(mark)mark.closest('figure').querySelector('figcaption').textContent=mark.dataset.figureHint;
}
document.addEventListener('pointerover',resultFigureHint);
document.addEventListener('focusin',resultFigureHint);

