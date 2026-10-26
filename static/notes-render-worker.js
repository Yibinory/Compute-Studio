'use strict';
importScripts('/static/vendor/marked/lib/marked.umd.js','/static/vendor/katex/dist/katex.min.js','/static/notes-markdown-core.js');
const PAGE_CHARS=12000;
let pages=[],links={};
function textSlices(text){const slices=[];for(let i=0;i<text.length;){let end=Math.min(i+PAGE_CHARS,text.length);if(end<text.length&&/[\uD800-\uDBFF]/.test(text[end-1]))end--;slices.push(text.slice(i,end));i=end;}return slices;}
function atoms(token){
  if((token.raw||'').length<=PAGE_CHARS)return [token];
  if(token.type==='code')return textSlices(token.text).map(text=>({...token,text,raw:text}));
  if(token.type==='paragraph'&&token.tokens){
    const pieces=[];let group=[],size=0;
    for(const item of token.tokens){
      const items=item.type==='text'&&item.text.length>PAGE_CHARS?textSlices(item.text).map(text=>({...item,text,raw:text})): [item];
      for(const part of items){const count=(part.raw||part.text||'').length;if(group.length&&size+count>PAGE_CHARS){pieces.push({...token,tokens:group,raw:' '.repeat(size)});group=[];size=0;}group.push(part);size+=count;}
    }
    if(group.length)pieces.push({...token,tokens:group,raw:' '.repeat(size)});return pieces;
  }
  if(token.type==='table'&&token.rows?.length>40)return Array.from({length:Math.ceil(token.rows.length/40)},(_,i)=>({...token,rows:token.rows.slice(i*40,(i+1)*40),raw:' '.repeat(PAGE_CHARS)}));
  if(token.type==='list'&&token.items?.length>40)return Array.from({length:Math.ceil(token.items.length/40)},(_,i)=>({...token,items:token.items.slice(i*40,(i+1)*40),start:token.ordered?Number(token.start||1)+i*40:token.start,raw:' '.repeat(PAGE_CHARS)}));
  if(token.type==='blockquote'&&token.tokens?.length>1)return token.tokens.flatMap(atoms).map(item=>({...token,tokens:[item],raw:item.raw}));
  return [token];
}
async function sourceText(source){
  if(source.file)return source.file.text();
  if(source.body!==undefined)return source.body;
  const response=await fetch(source.url,{credentials:'same-origin'});
  if(!response.ok)throw Error('文档读取失败：HTTP '+response.status);
  if(!response.body)return response.text();
  const reader=response.body.getReader(),decoder=new TextDecoder('utf-8'),parts=[];let received=0,last=0;
  for(;;){const {done,value}=await reader.read();if(done)break;received+=value.byteLength;parts.push(decoder.decode(value,{stream:true}));if(Date.now()-last>200){postMessage({type:'progress',received,total:Number(response.headers.get('content-length'))||0});last=Date.now();}}
  parts.push(decoder.decode());return parts.join('');
}
function page(index){
  index=Math.max(0,Math.min(pages.length-1,index));const tokens=pages[index]||[];tokens.links=links;
  postMessage({type:'page',index,total:pages.length||1,html:noteMarkdownParser.parser(tokens)});
}
self.onmessage=async event=>{
  try{
    if(event.data.type==='page'){page(event.data.index);return;}
    const body=(await sourceText(event.data.source)).replace(/^\uFEFF/,'');postMessage({type:'parsing',chars:body.length});
    const tokens=noteMarkdownParser.lexer(body);links=tokens.links;pages=[];let group=[],count=0;
    for(const token of tokens)for(const item of atoms(token)){
      const size=(item.raw||'').length;
      if(group.length&&(count+size>PAGE_CHARS||group.length>=100)){pages.push(group);group=[];count=0;}
      group.push(item);count+=size;
    }
    if(group.length||!pages.length)pages.push(group);page(0);
  }catch(error){postMessage({type:'error',message:error.message});}
};
