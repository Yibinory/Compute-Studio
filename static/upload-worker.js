'use strict';
let stopped=false,controllers=new Set(),currentUpload=null,config;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function request(url,options={},abortable=true){
 const controller=new AbortController();if(abortable)controllers.add(controller);
 try{const response=await fetch(url,{...options,credentials:'same-origin',signal:controller.signal,headers:{'X-CSRF-Token':config.csrf,...options.headers}}),value=await response.json();if(!response.ok)throw Error(value.error||'请求失败');return value;}
 finally{controllers.delete(controller);}
}
async function stopRemote(){if(currentUpload)await request(`/api/uploads/${currentUpload.id}/interrupt`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'},false).catch(error=>postMessage({type:'notice',message:'当前步骤未能中断：'+error.message}));}
async function run(message){
 config=message;const files=message.files,metadata=[];let total=0;
 try{
  for(let i=0;i<files.length;i++){
   if(stopped)break;const f=files[i];metadata.push({path:message.paths?.[i]||(f.webkitRelativePath?f.webkitRelativePath.split('/').slice(1).join('/'):f.name),size:f.size,last_modified:f.lastModified});total+=f.size;
   if(i%1000===0){postMessage({type:'preparing',count:i+1,total:files.length,bytes:total});await pause(0);}
  }
  if(stopped){postMessage({type:'canceled'});return;}
  postMessage({type:'preparing',count:files.length,total:files.length,bytes:total});
  const endpoint=message.resume?`/api/uploads/${message.resume}/resume`:'/api/uploads';
  currentUpload=await request(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...message.data,files:metadata})},false);
  postMessage({type:'bound',upload:{...currentUpload,files:undefined}});
  if(stopped){await stopRemote();postMessage({type:'canceled'});return;}
  let cursor=0,completed=currentUpload.received_bytes||0,last=0,started=Date.now(),initial=completed;
  async function send(parts,blobs,path){
   let value;
   for(let attempt=0;attempt<3;attempt++){
    if(stopped)return;
    try{value=await request(`/api/uploads/${currentUpload.id}/batch`,{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Upload-Batch':JSON.stringify(parts)},body:new Blob(blobs)});break;}
    catch(error){if(stopped)return;if(attempt===2)throw error;await pause(400*(attempt+1));}
   }
   completed=Math.max(completed,value.received_bytes);const seconds=(Date.now()-started)/1000,speed=seconds?(completed-initial)/seconds:0;
   if(Date.now()-last>200||completed===currentUpload.total_bytes){last=Date.now();postMessage({type:'progress',upload:value,progress:{...value.progress,path,bytes:completed,bytes_per_second:speed,eta_seconds:speed?(currentUpload.total_bytes-completed)/speed:null}});}
  }
  async function lane(){
   while(cursor<files.length&&!stopped){
    const index=cursor++,file=files[index],offset=currentUpload.files?.[index]?.received||0;if(offset===file.size)continue;
    if(file.size-offset<=256*1024){
     const parts=[{index,offset,length:file.size-offset}],blobs=[file.slice(offset)],names=[metadata[index].path];let bytes=file.size-offset;
     while(cursor<files.length&&parts.length<64){const i=cursor,f=files[i],o=currentUpload.files?.[i]?.received||0,n=f.size-o;if(n>256*1024||bytes+n>1024*1024)break;cursor++;if(!n)continue;parts.push({index:i,offset:o,length:n});blobs.push(f.slice(o));bytes+=n;names.push(metadata[i].path);}
     await send(parts,blobs,names[names.length-1]);
    }else for(let o=offset;o<file.size&&!stopped;o+=currentUpload.chunk_size){const n=Math.min(currentUpload.chunk_size,file.size-o);await send([{index,offset:o,length:n}],[file.slice(o,o+n)],metadata[index].path);}
   }
  }
  await Promise.all(Array.from({length:3},()=>lane()));
  if(stopped){await stopRemote();postMessage({type:'canceled'});return;}
  const upload=await request(`/api/uploads/${currentUpload.id}/finish`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'},false);
  if(stopped){await stopRemote();postMessage({type:'canceled'});return;}postMessage({type:'complete',upload});
 }catch(error){const canceled=stopped;stopped=true;controllers.forEach(c=>c.abort());await stopRemote();postMessage({type:canceled?'canceled':'error',message:error.message});}
}
onmessage=e=>{if(e.data.type==='cancel'){stopped=true;controllers.forEach(c=>c.abort());}else if(e.data.type==='start')run(e.data);};
