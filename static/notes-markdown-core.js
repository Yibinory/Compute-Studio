'use strict';
function noteEscape(text){return String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function noteMathToken(src,block){
  const match=src.match(block?/^ {0,3}(\$\$|\\\[)/:/^(\$\$|\\\[|\\\(|\$)/);
  if(!match)return;
  const open=match[1],close={'$$':'$$','\\[':'\\]','\\(':'\\)','$':'$'}[open],start=match[0].length;
  if(open==='$'&&/\s/.test(src[start]||''))return;
  let end=src.indexOf(close,start);
  while(end>=0){let slashes=0;for(let p=end-1;p>=0&&src[p]==='\\';p--)slashes++;
    if(slashes%2===0)break;end=src.indexOf(close,end+close.length);
  }
  if(end<0)return;
  const text=src.slice(start,end);
  if(!text.trim()||(open==='$'&&(/[\r\n`]/.test(text)||/\s/.test(text.slice(-1))||/\d/.test(src[end+1]||''))))return;
  let length=end+close.length;
  if(block&&src[length]==='\n')length++;
  return {type:block?'noteMathBlock':'noteMathInline',raw:src.slice(0,length),text,display:block||open==='$$'||open==='\\['};
}
const noteMarkdownParser=new marked.Marked({gfm:true,breaks:true,renderer:{html(token){return noteEscape(token.text);}},extensions:[
  {name:'noteMathBlock',level:'block',start(src){const i=src.search(/(?:^|\n) {0,3}(?:\$\$|\\\[)/);return i<0?undefined:i;},tokenizer(src){return noteMathToken(src,true);},renderer(token){return noteMathHTML(token);}},
  {name:'noteMathInline',level:'inline',start(src){const i=src.search(/\$\$|\\\[|\\\(|\$(?![\s$])/);return i<0?undefined:i;},tokenizer(src){return noteMathToken(src,false);},renderer(token){return noteMathHTML(token);}}
]});
function noteMathHTML(token){
  try{return katex.renderToString(token.text,{displayMode:token.display,throwOnError:false,trust:false,strict:'ignore',maxExpand:1000,maxSize:20,output:'htmlAndMathml'});}
  catch(error){return `<code class="note-math-error" title="${noteEscape(error.message)}">${noteEscape(token.text)}</code>`;}
}
