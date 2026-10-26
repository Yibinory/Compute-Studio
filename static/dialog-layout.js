'use strict';
(()=>{
  const dialog=document.querySelector('#dialog'),body=document.querySelector('#dialog-body');if(!dialog||!body)return;
  const dock=document.createElement('div');dock.id='dialog-actions';dock.hidden=true;dock.setAttribute('role','group');dock.setAttribute('aria-label','当前窗口操作');dialog.append(dock);
  let queued=false,currentSource=null,signature='';
  function viewport(){
    const view=window.visualViewport,zoom=parseFloat(getComputedStyle(document.documentElement).zoom)||1;
    dialog.style.setProperty('--dialog-viewport-height',(view?.height||innerHeight)/zoom+'px');
    dialog.style.setProperty('--dialog-viewport-width',(view?.width||innerWidth)/zoom+'px');
    dialog.style.setProperty('--dialog-inset-top',(view?.offsetTop||0)/zoom+'px');
    dialog.style.setProperty('--dialog-inset-left',(view?.offsetLeft||0)/zoom+'px');
    dialog.style.setProperty('--dialog-inset-bottom',Math.max(0,innerHeight-(view?.height||innerHeight)-(view?.offsetTop||0))/zoom+'px');
    dialog.style.setProperty('--dialog-inset-right',Math.max(0,innerWidth-(view?.width||innerWidth)-(view?.offsetLeft||0))/zoom+'px');
  }
  function chooseSource(){
    const explicit=body.querySelector('[data-dialog-actions-source]');if(explicit)return explicit;
    const footers=body.querySelectorAll('.form-footer');if(footers.length)return footers[footers.length-1];
    const last=body.lastElementChild;if(last?.matches('.actions'))return last;
    if(last?.matches('button[data-action]'))return last;
    return null;
  }
  function sync(){
    queued=false;document.documentElement.classList.toggle('dialog-open',dialog.open);viewport();
    const source=dialog.open?chooseSource():null;
    if(currentSource!==source){currentSource?.removeAttribute('data-dialog-docked');currentSource=source;signature='';}
    if(!source){dock.hidden=true;dock.replaceChildren();return;}
    if(!source.hasAttribute('data-dialog-docked'))source.setAttribute('data-dialog-docked','');
    const fingerprint=source.outerHTML;if(fingerprint===signature)return;signature=fingerprint;
    const controls=source.matches('button')?[source]:[...source.children];dock.replaceChildren();
    if(source.dataset.dialogSummary){const summary=document.createElement('span');summary.className='dialog-action-summary';summary.textContent=source.dataset.dialogSummary;dock.append(summary);}
    for(const original of controls){
      if(original.matches('input'))continue;
      const copy=original.cloneNode(true);copy.removeAttribute('id');
      if(original.matches('button')){
        copy.type='button';copy.removeAttribute('form');copy.removeAttribute('data-action');
        copy.dataset.dialogProxyAction=original.dataset.action||'submit';
        if(original.closest('form')?.id)copy.dataset.dialogProxyForm=original.closest('form').id;
        copy.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();if(original.isConnected&&!original.disabled)original.click();});
      }
      dock.append(copy);
    }
    dock.hidden=!dock.children.length;
  }
  function schedule(){if(!queued){queued=true;requestAnimationFrame(sync);}}
  new MutationObserver(schedule).observe(body,{childList:true,subtree:true,attributes:true,characterData:true});
  new MutationObserver(schedule).observe(dialog,{attributes:true,attributeFilter:['open']});
  const originalModal=modal;
  modal=function(title,content,kind=''){const result=originalModal(title,content,kind);dialog.dataset.kind=kind;body.scrollTop=0;body.scrollLeft=0;dialog.scrollTop=0;signature='';sync();return result;};
  dialog.addEventListener('close',sync);
  window.addEventListener('resize',viewport);window.visualViewport?.addEventListener('resize',viewport);window.visualViewport?.addEventListener('scroll',viewport);
  viewport();
})();
