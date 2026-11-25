'use strict';

function updateVersionLabel(version) {
  return 'v' + version;
}

function paintUpdateDialog(result, state, checked=true) {
  const latest=state.latest, waiting=state.pending;
  const versionSummary=`<div class="update-version-summary"><span>当前版本 <b>${esc(updateVersionLabel(state.current))}</b></span>${latest?'<span>最新发布 <b>'+esc(updateVersionLabel(latest.version))+'</b></span>':''}</div>`;
  const pending=waiting?`<div class="notice">已校验并准备好 ${esc(updateVersionLabel(waiting))}。请在后台工作完成后重启控制器，新版本将在下次空闲启动时启用。</div>`:'';
  let message='';
  if (state.check_error) message=`<p class="error-text">${esc(state.check_error)}</p>`;
  else if (checked&&!latest) message='<p>GitHub 暂无正式发布。</p>';
  else if (!checked) message='';
  else if (!state.available) message='<p>当前已是最新版本。</p>';
  else if (!state.has_package) message='<p>发现新版。请查看发布页的安装说明。</p>';
  else if (!state.can_install) message='<p>请从正式控制器安装这个版本。</p>';
  const install=state.available&&state.has_package&&state.can_install&&waiting!==latest?.version
    ?button('update-stage','下载并准备安装',`data-version="${esc(latest.version)}"`,'primary small'):'';
  result.innerHTML=versionSummary+pending+(state.error?`<p class="error-text">${esc(state.error)}</p>`:'')+message+
    (latest?.notes?`<pre class="update-release-notes">${esc(latest.notes)}</pre>`:'')+
    `<div class="actions">${install}${button('update-recheck','重新检查','','small')}<a href="${esc(latest?.url||state.releases_url)}" target="_blank" rel="noopener">GitHub 发布 ↗</a></div>`;
}

async function checkUpdateDialog(result) {
  const current=await api('/api/update/status');
  try {
    const latest=await api('/api/update/check');
    if(result.isConnected&&dialogKind==='controller-update')paintUpdateDialog(result,latest);
  } catch(error) {
    if(result.isConnected&&dialogKind==='controller-update')paintUpdateDialog(result,{...current,check_error:error.message});
  }
}

const applyUpdateBaseAction=handleAction;
handleAction=async function(el){
  const action=el.dataset.action;
  if (action==='update-open') {
    modal('工具更新','<div id="update-version-result" aria-live="polite">正在检查 GitHub 正式发布…</div>','controller-update');
    await checkUpdateDialog($('#update-version-result'));
    return;
  }
  if (action==='update-recheck') {
    const result=$('#update-version-result');
    await checkUpdateDialog(result);
    return;
  }
  if (action==='update-stage') {
    const result=$('#update-version-result');
    const label=el.textContent;
    el.textContent='正在下载并校验…';
    let status;
    try {
      status=await api('/api/update/stage',{version:el.dataset.version});
    } catch(error) {
      if(el.isConnected)el.textContent=label;
      if(result.isConnected)toast(error.message,true);
      return;
    }
    if(result.isConnected&&dialogKind==='controller-update')paintUpdateDialog(result,status,false);
    toast('新版已准备好，将在下次空闲重启时启用');
    return;
  }
  return applyUpdateBaseAction(el);
};
