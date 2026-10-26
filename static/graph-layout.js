'use strict';
// Dependency ranks determine parallelism; dates only order siblings.
function layoutTasks(tasks, columns=3, geometry={}) {
  const {nodeWidth=274,nodeHeight=228,gapX=28,gapY=36,axis=118,top=40,mode='dependencies',orientation='vertical'}=geometry;
  const map=new Map(tasks.map(t=>[t.id,t]));
  const timestamp=t=>t.started_at||t.created_at||0;
  const times=[...new Set(tasks.flatMap(t=>[Math.floor(timestamp(t)/60),Math.floor((t.finished_at||timestamp(t))/60)]))].sort((a,b)=>a-b);
  const pending=new Set(map.keys()), placed=new Map(), rows=new Map();
  const order=[];
  while(pending.size){
    const projected=t=>Math.max(timestamp(t),...(t.depends_on||[]).map(id=>placed.get(id)?.finished_at||placed.get(id)?.timelineTime||0));
    const ready=[...pending].map(id=>map.get(id)).filter(t=>(t.depends_on||[]).every(id=>!map.has(id)||placed.has(id))).sort((a,b)=>projected(a)-projected(b)||(a.seq||0)-(b.seq||0));
    if(!ready.length)throw Error('依赖图存在循环');
    const task=ready[0];
    const timelineTime=projected(task);
    const rank=Math.max(0,...(task.depends_on||[]).filter(id=>placed.has(id)).map(id=>placed.get(id).rank+1));
    const dependencyRow=Math.max(0,...(task.depends_on||[]).filter(id=>placed.has(id)).map(id=>placed.get(id).row+1));
    const row=mode==='time'?Math.max(times.indexOf(Math.floor(timelineTime/60)),dependencyRow):rank;
    const group=rows.get(row)||[], col=group.length;
    const item={...task,row,rank,col,timelineTime,x:axis+col*(nodeWidth+gapX),y:top+row*(nodeHeight+gapY)};
    group.push(item);rows.set(row,group);placed.set(task.id,item);pending.delete(task.id);order.push(item);
  }
  const occupied=[...rows.keys()].sort((a,b)=>a-b);
  const widest=Math.max(1,...[...rows.values()].map(group=>group.length));
  // Complete ranks remain on the same row. Scrolling handles wide graphs;
  // wrapping parallel nodes onto new rows would imply a false serial order.
  for(let pass=0;pass<Math.min(6,Math.max(1,columns));pass++)for(const row of occupied){
    const group=rows.get(row),score=n=>{const parents=(n.depends_on||[]).map(id=>placed.get(id)).filter(Boolean);return parents.length?parents.reduce((s,p)=>s+(p.col+.5)/rows.get(p.row).length,0)/parents.length:(n.col+.5)/group.length;};
    group.sort((a,b)=>score(a)-score(b)||a.col-b.col);group.forEach((n,i)=>n.col=i);
  }
  for(const item of order){const group=rows.get(item.row);item.x=axis+(widest-group.length)*(nodeWidth+gapX)/2+item.col*(nodeWidth+gapX);item.row=occupied.indexOf(item.row);item.y=top+item.row*(nodeHeight+gapY);}
  const ancestors=new Map();
  function parentSet(id){if(ancestors.has(id))return ancestors.get(id);const result=new Set();for(const parent of map.get(id)?.depends_on||[]){if(!map.has(parent))continue;result.add(parent);for(const above of parentSet(parent))result.add(above);}ancestors.set(id,result);return result;}
  const edges=order.flatMap(n=>(n.depends_on||[]).filter(id=>map.has(id)).map(id=>({source:id,target:n.id,secondary:(n.depends_on||[]).some(other=>other!==id&&parentSet(other).has(id))})));
  // Keep a branch aligned through successive ranks. Isotonic projection
  // preserves node order and minimum spacing while centering forks/merges.
  const primary=edges.filter(e=>!e.secondary),spacing=nodeWidth+gapX;
  function align(group,parents){
    const desired=group.map((n,i)=>{const peers=primary.filter(e=>e[parents?'target':'source']===n.id).map(e=>placed.get(e[parents?'source':'target']));return {value:(peers.length?peers.reduce((s,p)=>s+p.x-axis,0)/peers.length:n.x-axis)-i*spacing,count:1,start:i,end:i};});
    const blocks=[];for(const point of desired){blocks.push(point);while(blocks.length>1&&blocks.at(-2).value>blocks.at(-1).value){const b=blocks.pop(),a=blocks.pop(),count=a.count+b.count;blocks.push({value:(a.value*a.count+b.value*b.count)/count,count,start:a.start,end:b.end});}}
    for(const block of blocks)for(let i=block.start;i<=block.end;i++)group[i].x=axis+i*spacing+Math.max(0,Math.min((widest-group.length)*spacing,block.value));
  }
  for(let pass=0;pass<5;pass++){
    for(const row of [...occupied].reverse())align(rows.get(row),false);
    for(const row of occupied)align(rows.get(row),true);
  }
  if(orientation==='horizontal')for(const item of order){const cross=(item.x-axis)/(nodeWidth+gapX);item.x=axis+item.row*(nodeWidth+gapX);item.y=top+cross*(nodeHeight+gapY);}
  return {nodes:order,edges,nodeWidth,nodeHeight,orientation,maxParallel:widest,width:axis+Math.max(0,(orientation==='horizontal'?occupied.length:widest)*(nodeWidth+gapX)-gapX)+32,height:top+Math.max(0,(orientation==='horizontal'?widest:occupied.length)*(nodeHeight+gapY)-gapY)+30,rows:occupied.map((row,i)=>[i,rows.get(row)])};
}
function assignTaskPorts(graph){
  const nodes=new Map(graph.nodes.map(n=>[n.id,n])),horizontal=graph.orientation==='horizontal';
  const edges=graph.edges.map(e=>({...e})),incoming=new Map(),outgoing=new Map();
  for(const edge of edges){if(!incoming.has(edge.target))incoming.set(edge.target,[]);incoming.get(edge.target).push(edge);if(!outgoing.has(edge.source))outgoing.set(edge.source,[]);outgoing.get(edge.source).push(edge);}
  const cross=n=>horizontal?n.y+graph.nodeHeight/2:n.x+graph.nodeWidth/2;
  const extent=horizontal?graph.nodeHeight:graph.nodeWidth,inset=Math.min(16,extent/4);
  for(const [groups,key,peer,direction] of [[outgoing,'source','target',1],[incoming,'target','source',-1]])for(const group of groups.values()){
    group.sort((a,b)=>cross(nodes.get(a[peer]))-cross(nodes.get(b[peer]))||direction*(nodes.get(a[peer]).row-nodes.get(b[peer]).row)||a[peer].localeCompare(b[peer]));
    group.forEach((edge,index)=>{edge[key+'Offset']=inset+(extent-2*inset)*(index+1)/(group.length+1);edge[key+'Stub']=3+7*(group.length-index)/(group.length+1);});
  }
  return edges;
}
// Route each dependency around card rectangles. Grid lines lie inside the
// inter-row/inter-column gutters; long edges no longer all detour to the right.
const routedEdgeCache=new Map();
function routeTaskEdges(graph,edges){
  const cacheKey=JSON.stringify([graph.orientation,graph.nodeWidth,graph.nodeHeight,graph.width,graph.height,graph.nodes.map(n=>[n.id,n.x,n.y,n.row]),edges.map(e=>[e.source,e.target,e.sourceOffset,e.targetOffset])]);
  const cached=routedEdgeCache.get(cacheKey);
  if(cached)return edges.map((edge,i)=>({...edge,points:cached[i].map(point=>[...point])}));
  const horizontal=graph.orientation==='horizontal',w=horizontal?graph.nodeHeight:graph.nodeWidth,h=horizontal?graph.nodeWidth:graph.nodeHeight;
  const nodes=new Map(graph.nodes.map(n=>[n.id,{...n,x:horizontal?n.y:n.x,y:horizontal?n.x:n.y}]));
  const rects=[...nodes.values()].map(n=>({left:n.x-6,right:n.x+w+6,top:n.y-6,bottom:n.y+h+6}));
  const xs=[...new Set([10,...rects.flatMap(r=>[r.left-3,r.right+3]),(horizontal?graph.height:graph.width)-10])].sort((a,b)=>a-b);
  const ys=[...new Set([...nodes.values()].flatMap(n=>[n.y-12,n.y+h+12]))].sort((a,b)=>a-b);
  const tracks=[];
  const clear=(a,b)=>!rects.some(r=>a[0]===b[0]?a[0]>r.left&&a[0]<r.right&&Math.max(a[1],b[1])>r.top&&Math.min(a[1],b[1])<r.bottom:a[1]>r.top&&a[1]<r.bottom&&Math.max(a[0],b[0])>r.left&&Math.min(a[0],b[0])<r.right);
  const routed=edges.map(edge=>{
    const source=nodes.get(edge.source),target=nodes.get(edge.target),sx=source.x+edge.sourceOffset,sy=source.y+h,tx=target.x+edge.targetOffset,ty=target.y;
    let points;
    if(target.row-source.row===1){
      const low=sy+12,high=ty-12,left=Math.min(sx,tx),right=Math.max(sx,tx);
      let track=0;while(tracks.some(t=>t.row===source.row&&t.track===track&&t.left<right+8&&left<t.right+8))track++;
      tracks.push({row:source.row,track,left,right});const mid=low+(high-low)*(.25+(track%7)/12);
      points=[[sx,sy],[sx,mid],[tx,mid],[tx,ty]];
    }else{
      const start=[sx,sy+12],end=[tx,ty-12],gx=[...new Set([...xs,sx,tx])].sort((a,b)=>a-b),gy=ys.filter(y=>y>=start[1]&&y<=end[1]);
      if(!gy.includes(start[1]))gy.push(start[1]);if(!gy.includes(end[1]))gy.push(end[1]);gy.sort((a,b)=>a-b);
      const startX=gx.indexOf(sx),startY=gy.indexOf(start[1]),endX=gx.indexOf(tx),endY=gy.indexOf(end[1]),queue=[{x:startX,y:startY,dir:1,cost:0,path:[start]}],seen=new Map();let found=null;
      while(queue.length){
        queue.sort((a,b)=>b.cost-a.cost);const current=queue.pop(),key=current.x+','+current.y+','+current.dir;
        if((seen.get(key)??Infinity)<=current.cost)continue;seen.set(key,current.cost);
        if(current.x===endX&&current.y===endY){found=current.path;break;}
        for(const [dx,dy,dir] of [[1,0,0],[-1,0,0],[0,1,1]]){
          const x=current.x+dx,y=current.y+dy;if(x<0||x>=gx.length||y>=gy.length)continue;
          const a=[gx[current.x],gy[current.y]],b=[gx[x],gy[y]];if(!clear(a,b))continue;
          const cost=current.cost+Math.abs(b[0]-a[0])+Math.abs(b[1]-a[1])+(dir===current.dir?0:22);
          if((seen.get(x+','+y+','+dir)??Infinity)>cost)queue.push({x,y,dir,cost,path:[...current.path,b]});
        }
      }
      if(!found){const lane=(horizontal?graph.height:graph.width)-10;found=[start,[lane,start[1]],[lane,end[1]],end];}
      points=[[sx,sy],...found,[tx,ty]];
    }
    // Remove collinear grid steps so rounded corners retain their full radius.
    const clean=points.filter((p,i)=>!i||i===points.length-1||!((points[i-1][0]===p[0]&&p[0]===points[i+1][0])||(points[i-1][1]===p[1]&&p[1]===points[i+1][1])));
    return {...edge,points:horizontal?clean.map(([x,y])=>[y,x]):clean};
  });
  routedEdgeCache.set(cacheKey,routed.map(edge=>edge.points.map(point=>[...point])));
  if(routedEdgeCache.size>6)routedEdgeCache.delete(routedEdgeCache.keys().next().value);
  return routed;
}
if(typeof module!=='undefined')module.exports={layoutTasks,assignTaskPorts,routeTaskEdges};
