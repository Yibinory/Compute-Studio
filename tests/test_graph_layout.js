const assert=require('assert');
const {layoutTasks,assignTaskPorts,routeTaskEdges}=require('../static/graph-layout');
const tasks=[{id:'a',seq:1,started_at:60,depends_on:[]},{id:'b',seq:2,started_at:120,depends_on:['a']},{id:'c',seq:3,created_at:120,depends_on:['a']},{id:'d',seq:4,created_at:120,depends_on:['b','c']}];
const graph=layoutTasks(tasks),map=new Map(graph.nodes.map(n=>[n.id,n]));
for(const t of tasks)for(const dep of t.depends_on)assert(map.get(dep).y+228<map.get(t.id).y);
assert.strictEqual(map.get('b').y,map.get('c').y);
assert.notStrictEqual(map.get('b').x,map.get('c').x);
assert.throws(()=>layoutTasks([{id:'a',depends_on:['b']},{id:'b',depends_on:['a']}]),/循环/);
const bulk=layoutTasks(Array.from({length:30},(_,i)=>({id:String(i),created_at:120,depends_on:i>2?[String(i-3)]:[]})));
assert.strictEqual(new Set(bulk.nodes.map(n=>n.x+','+n.y)).size,30);
assert.strictEqual(layoutTasks([]).nodes.length,0);
const compact=layoutTasks(tasks,3,{nodeWidth:300,nodeHeight:144,gapX:38,gapY:34,axis:94,top:22}),cm=new Map(compact.nodes.map(n=>[n.id,n]));
for(const task of tasks)for(const id of task.depends_on)assert(cm.get(id).y+compact.nodeHeight<cm.get(task.id).y);
for(let i=0;i<compact.nodes.length;i++)for(let j=i+1;j<compact.nodes.length;j++){const a=compact.nodes[i],b=compact.nodes[j];assert(a.x+compact.nodeWidth<=b.x||b.x+compact.nodeWidth<=a.x||a.y+compact.nodeHeight<=b.y||b.y+compact.nodeHeight<=a.y);}
const chain=layoutTasks(Array.from({length:5},(_,i)=>({id:String(i),created_at:120,depends_on:i?[String(i-1)]:[]})),1,{nodeWidth:820,nodeHeight:128,gapY:34,axis:94,top:22});
assert(chain.height<850);assert.strictEqual(new Set(chain.nodes.map(n=>n.x)).size,1);
const differentDates=layoutTasks([{id:'root',created_at:60},{id:'left',created_at:120,depends_on:['root']},{id:'right',created_at:86400,depends_on:['root']},{id:'merge',created_at:86500,depends_on:['left','right']}]);
const dm=new Map(differentDates.nodes.map(n=>[n.id,n]));assert.strictEqual(dm.get('left').row,dm.get('right').row);assert.notStrictEqual(dm.get('left').x,dm.get('right').x);
const manyParallel=layoutTasks(Array.from({length:8},(_,i)=>({id:'p'+i,created_at:60+i*86400})));assert.strictEqual(new Set(manyParallel.nodes.map(n=>n.row)).size,1);assert.strictEqual(manyParallel.maxParallel,8);
const timed=layoutTasks([{id:'early',created_at:60},{id:'late',created_at:86400}],3,{mode:'time'});assert(timed.nodes[0].row<timed.nodes[1].row);
const redundant=layoutTasks([{id:'a'},{id:'b',depends_on:['a']},{id:'c',depends_on:['a','b']}]);assert(redundant.edges.find(e=>e.source==='a'&&e.target==='c').secondary);assert(!redundant.edges.find(e=>e.source==='b'&&e.target==='c').secondary);
for(const orientation of ['vertical','horizontal']){
  const g=layoutTasks(tasks,3,{nodeWidth:220,nodeHeight:64,gapX:36,gapY:24,axis:24,top:24,orientation}),nodes=new Map(g.nodes.map(n=>[n.id,n]));
  for(const t of tasks)for(const dep of t.depends_on)assert(orientation==='vertical'?nodes.get(dep).y+64<nodes.get(t.id).y:nodes.get(dep).x+220<nodes.get(t.id).x);
  assert(orientation==='vertical'?nodes.get('b').y===nodes.get('c').y:nodes.get('b').x===nodes.get('c').x);
  assert.strictEqual(g.edges.length,4);
  for(let i=0;i<g.nodes.length;i++)for(let j=i+1;j<g.nodes.length;j++){const a=g.nodes[i],b=g.nodes[j];assert(a.x+220<=b.x||b.x+220<=a.x||a.y+64<=b.y||b.y+64<=a.y);}
  assert(g.nodes.every(n=>n.x>=0&&n.y>=0&&n.x+220<=g.width&&n.y+64<=g.height));
}
const smallChain=layoutTasks(Array.from({length:5},(_,i)=>({id:String(i),depends_on:i?[String(i-1)]:[]})),3,{nodeWidth:248,nodeHeight:64,gapY:24,axis:24,top:24});
assert(smallChain.height<480);
for(const orientation of ['vertical','horizontal']){
  const g=layoutTasks([{id:'a'},{id:'b',depends_on:['a']},{id:'c',depends_on:['a','b']},{id:'d',depends_on:['a','b','c']}],3,{nodeWidth:248,nodeHeight:64,orientation});
  const original=JSON.stringify(g.edges),edges=assignTaskPorts(g),extent=orientation==='horizontal'?64:248;
  for(const node of g.nodes)for(const side of ['source','target']){
    const ports=edges.filter(e=>e[side]===node.id).map(e=>e[side+'Offset']);
    assert.strictEqual(new Set(ports).size,ports.length);
    assert(ports.every(p=>p>0&&p<extent));
  }
  assert.strictEqual(JSON.stringify(g.edges),original);
  const a=edges.filter(e=>e.source==='a');assert(a[0].sourceOffset!==a[1].sourceOffset);
  const d=edges.filter(e=>e.target==='d');assert.strictEqual(new Set(d.map(e=>e.targetOffset)).size,3);
  assert.deepStrictEqual(assignTaskPorts(g),edges);
}
for(const orientation of ['vertical','horizontal']){
  const fixture=[{id:'root'},...Array.from({length:7},(_,i)=>({id:'branch-'+i,depends_on:['root']})),
    ...Array.from({length:7},(_,i)=>({id:'leaf-'+i,depends_on:['branch-'+i,'root']})),
    {id:'end',depends_on:Array.from({length:7},(_,i)=>'leaf-'+i)}];
  const original=JSON.stringify(fixture),g=layoutTasks(fixture,3,{nodeWidth:192,nodeHeight:128,gapX:64,gapY:54,orientation});
  const routed=routeTaskEdges(g,assignTaskPorts(g));assert.strictEqual(routed.length,g.edges.length);
  assert.deepStrictEqual(routeTaskEdges(g,assignTaskPorts(g)),routed);assert.strictEqual(JSON.stringify(fixture),original);
  const cached=routeTaskEdges(g,assignTaskPorts(g)),expected=cached[0].points[0][0];cached[0].points[0][0]+=10000;
  assert.strictEqual(routeTaskEdges(g,assignTaskPorts(g))[0].points[0][0],expected,'cached paths are not shared mutable state');
  const changed=assignTaskPorts(g);changed[0].secondary=!changed[0].secondary;
  assert.strictEqual(routeTaskEdges(g,changed)[0].secondary,changed[0].secondary,'cached geometry preserves current edge metadata');
  for(const edge of routed){
    assert(edge.points.every(p=>p.every(Number.isFinite)));
    for(let i=1;i<edge.points.length;i++){
      const a=edge.points[i-1],b=edge.points[i];assert(a[0]===b[0]||a[1]===b[1],'orthogonal connector');
      for(const n of g.nodes){
        const hit=a[0]===b[0]?a[0]>n.x+.1&&a[0]<n.x+g.nodeWidth-.1&&Math.max(a[1],b[1])>n.y+.1&&Math.min(a[1],b[1])<n.y+g.nodeHeight-.1:
          a[1]>n.y+.1&&a[1]<n.y+g.nodeHeight-.1&&Math.max(a[0],b[0])>n.x+.1&&Math.min(a[0],b[0])<n.x+g.nodeWidth-.1;
        assert(!hit,'connector crosses card '+n.id);
      }
    }
  }
}
const separate=layoutTasks([{id:'old1'},{id:'old2',depends_on:['old1']},{id:'r'},...Array.from({length:5},(_,i)=>({id:'p'+i,depends_on:['r']}))]);
assert(Math.abs(separate.nodes.find(n=>n.id==='old1').x-separate.nodes.find(n=>n.id==='old2').x)<1,'independent chain stays aligned');
console.log('Graph layout and routing: both orientations, parallel/merge alignment, card avoidance, unique ports, deterministic routes, unchanged dependencies and cycle rejection passed.');
