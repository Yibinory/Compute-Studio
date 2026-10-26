const assert=require('assert');
const {benchmarkAvailability:availability}=require('../static/benchmark-state');
const server={id:'a',connected:true},gpu={index:0,uuid:'GPU-1',is_idle:true,memory_used_mb:0,utilization_gpu:0,processes:[]};
assert(!availability(server,gpu).disabled);
assert.strictEqual(availability({...server,connected:false},gpu).label,'等待服务器连接');
const occupied=availability(server,{...gpu,is_idle:false,process_count:2,memory_used_mb:2048});
assert(occupied.disabled&&occupied.reason.includes('2 个计算进程')&&occupied.reason.includes('0%'));
for(const status of ['queued','running']){
 const result=availability(server,gpu,[{status,operation:'benchmark:["a",0]'}]);
 assert(result.disabled&&result.label===(status==='queued'?'测试排队中':'正在测试'));
}
assert(availability(server,gpu,[],[{status:'staging',server_id:'b',gpu_uuid:'GPU-1'}]).disabled);
assert(availability(server,gpu,[{status:'running',operation:'benchmark:["b",2]'}],[],[{id:'b',snapshot:{gpus:[{index:2,uuid:'GPU-1'}]}}]).disabled);
assert(!availability(server,gpu,[{status:'success',operation:'benchmark:["a",0]'}]).disabled);
console.log('Benchmark availability: idle, disconnected, occupied, queue, running and physical GPU aliases passed.');
