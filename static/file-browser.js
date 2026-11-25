'use strict';

const filePageSize = 100;
const folderIcon = '<span class="file-folder-icon" aria-hidden="true"></span>';
const fileIcon = '<span class="file-document-icon" aria-hidden="true"></span>';

function fileBrowseParams(view) {
  return new URLSearchParams({directory:view.directory || '', query:view.query || '', offset:view.offset || 0, limit:filePageSize});
}

function fileDisplayName(path, page) {
  const prefix = page.directory ? page.directory + '/' : '';
  const label = page.folders && path.startsWith(prefix) ? path.slice(prefix.length) : path;
  return `<span class="file-entry-name" title="${esc(path)}">${fileIcon}<span>${esc(label)}</span></span>`;
}

function fileEntryType(path) {
  const name = path.split('/').at(-1), dot = name.lastIndexOf('.');
  const extension = dot > 0 ? name.slice(dot+1) : '';
  return /^[a-z0-9]{1,10}$/i.test(extension) ? extension.toUpperCase()+' 文件' : '文件';
}

function fileBrowserCompatibility() {
  return state.runtime_features?.file_browser ? '' :
    '<p class="file-browser-compatibility">目录浏览将在控制器重启后启用。当前显示按路径排列的文件。</p>';
}

function fileBreadcrumb(page, action, attrs = '') {
  if (!page.folders) return '';
  const parts = page.directory ? page.directory.split('/') : [];
  const links = ['根目录', ...parts].map((name, i) => {
    const path = parts.slice(0, i).join('/');
    if (i === parts.length) return `<span class="file-current-dir" aria-current="location" title="${esc(path)}">${esc(name)}</span>`;
    return button(action, esc(name), `data-directory="${esc(path)}" ${attrs}`, 'file-crumb');
  });
  const up = button(action, '↑', `data-directory="${esc(parts.slice(0,-1).join('/'))}" ${attrs} aria-label="上一级" title="返回上一级" ${parts.length?'':'disabled'}`, 'file-up');
  return `<div class="file-browser-location">${up}<nav class="file-breadcrumb" aria-label="当前文件夹">${links.join('<span class="file-crumb-divider" aria-hidden="true">›</span>')}</nav></div>`;
}

function fileFolderRows(page, columns, action, attrs = '', typeColumn = -1) {
  const cells = Array.from({length:columns-1},(_,i)=>i+1===typeColumn ? '<td class="file-entry-type">文件夹</td>' : '<td></td>').join('');
  return (page.folders || []).map(path => `<tr class="file-folder-row"><td>${button(action,
    folderIcon + `<span>${esc(path.split('/').at(-1))}</span>`,
    `type="button" data-directory="${esc(path)}" title="${esc(path)}" ${attrs}`, 'file-folder-button')}</td>${cells}</tr>`).join('');
}

function fileEntryCount(page) { return page.entry_count ?? page.matched_count ?? page.total ?? 0; }
function filePageLength(page) { return (page.folders?.length || 0) + page.files.length; }
function filePageRange(page) {
  const total = fileEntryCount(page), from = filePageLength(page) ? page.offset + 1 : 0;
  return `${from.toLocaleString()}–${(from ? from + filePageLength(page) - 1 : 0).toLocaleString()} / ${total.toLocaleString()}`;
}

const mountedFileBrowsers = new WeakMap();

function mountFileBrowser(target, options) {
  const view = {directory:'', query:'', offset:0, revision:0, options, target};
  mountedFileBrowsers.set(target, view);
  target.innerHTML = `<form data-file-browser-search class="file-browser-search"><label>在当前目录及子目录搜索<input name="query" type="search" autocomplete="off" placeholder="文件名或目录"></label><button type="submit" class="small">搜索</button></form><div data-file-browser-page aria-live="polite"></div>`;
  return readFileBrowser(view);
}

async function readFileBrowser(view) {
  const {target, options} = view, revision = ++view.revision;
  const pane = $('[data-file-browser-page]', target);
  const active = () => target.isConnected && mountedFileBrowsers.get(target) === view && view.revision === revision;
  pane.setAttribute('aria-busy','true');
  if (!pane.firstChild) pane.textContent = '正在读取文件…';
  try {
    const page = await api(options.url + fileBrowseParams(view));
    if (!active()) return;
    afterControlInteraction(pane, () => {
      if (!active()) return;
      if (page.offset && page.offset >= fileEntryCount(page)) {
        view.offset = fileEntryCount(page) ? Math.floor((fileEntryCount(page)-1)/filePageSize)*filePageSize : 0;
        readFileBrowser(view);
        return;
      }
      const rows = fileFolderRows(page, options.columns.length, 'file-browser-dir') + page.files.map(file => options.row(file,page)).join('');
      pane.innerHTML = (options.summary?.(page) || '') + fileBreadcrumb(page,'file-browser-dir') +
        (rows ? `<div class="file-browser-table file-details-table"><table><thead><tr>${options.columns.map(c=>'<th scope="col">'+esc(c)+'</th>').join('')}</tr></thead><tbody>${rows}</tbody></table></div>`
          : `<p class="file-browser-empty">${view.query ? '没有匹配的文件' : esc(options.empty || '此目录没有文件')}</p>`) +
        `<div class="file-browser-pagination"><small>${filePageRange(page)}</small><div class="actions">${button('file-browser-prev','上一页',page.offset?'':'disabled','small')}${button('file-browser-next','下一页',page.offset+filePageSize<fileEntryCount(page)?'':'disabled','small')}</div></div>`;
      pane.removeAttribute('aria-busy');
    });
  } catch (error) {
    if (!active()) return;
    pane.innerHTML = fileBreadcrumb({directory:view.directory,folders:[]},'file-browser-dir') +
      `<p class="error-text">${esc(error.message)}</p>` + button('file-browser-retry','重试','','small');
    pane.removeAttribute('aria-busy');
  }
}

const fileBrowserBaseAction = handleAction;
handleAction = async function(el) {
  const action = el.dataset.action;
  if (action === 'replica-files' && state.runtime_features?.file_browser) {
    const url = `/api/projects/${encodeURIComponent(el.dataset.project)}/replicas/${encodeURIComponent(el.dataset.server)}`;
    modal('远程文件清单', '<p class="muted" id="replica-inventory-date"></p><label>清单范围<select id="replica-file-scope"><option value="differences">公共代码差异</option><option value="files">全部远程文件</option></select></label><div id="replica-inventory"></div>', 'replica');
    const target = $('#replica-inventory'), scope = $('#replica-file-scope');
    const read = () => {
      const differences = scope.value === 'differences';
      return mountFileBrowser(target,{
        url:url+'?scope='+scope.value+'&',
        columns:differences?['名称','差异状态']:['名称','大小','SHA-256'],
        empty:differences?'此目录没有代码差异':'此目录没有文件',
        summary:page=>{
          $('#replica-inventory-date').textContent = '核验 '+fmt(page.checked_at)+' · '+page.total_count.toLocaleString()+' 个文件 · '+page.difference_count.toLocaleString()+' 项差异';
          return '';
        },
        row:(file,page)=>`<tr><td>${fileDisplayName(file.path,page)}</td>${differences
          ? '<td>'+esc(file.state)+'</td>'
          : `<td>${size(file.size)}</td><td><code title="${esc(file.sha256)}">${esc(file.sha256?.slice(0,16) || '—')}</code></td>`}</tr>`
      });
    };
    scope.addEventListener('change',read);
    return read();
  }
  if (!action.startsWith('file-browser-')) return fileBrowserBaseAction(el);
  const view = mountedFileBrowsers.get(el.closest('[data-file-browser-page]')?.parentElement);
  if (!view) return;
  if (action === 'file-browser-hash') {
    const data = await api(view.options.url+'hash_path='+encodeURIComponent(el.dataset.path));
    if (el.isConnected) {
      const hash = document.createElement('code');
      hash.title = data.sha256;
      hash.textContent = data.sha256.slice(0,16)+'…';
      el.replaceWith(hash);
    }
    return;
  }
  if (action === 'file-browser-dir') {
    Object.assign(view,{directory:el.dataset.directory, offset:0, query:''});
    $('[name=query]',view.target).value = '';
  }
  if (action === 'file-browser-prev') view.offset = Math.max(0,view.offset-filePageSize);
  if (action === 'file-browser-next') view.offset += filePageSize;
  return readFileBrowser(view);
};

function fileDownload(url, path) {
  return `<a href="${esc(url+encodeURIComponent(path))}" download>下载</a>`;
}

const flatArtifactFiles = artifactFiles;
artifactFiles = function(id, attempt) {
  if (!state.runtime_features?.file_browser) return flatArtifactFiles(id,attempt);
  const target = $('#artifact-files');
  if (!target) return;
  const url = `/api/tasks/${encodeURIComponent(id)}/artifacts?attempt_id=${encodeURIComponent(attempt)}&`;
  return mountFileBrowser(target,{
    url, columns:['名称','大小','SHA-256','操作'],
    summary:page=>`<p class="server-subtitle break">${esc(page.local_path)}</p>`,
    empty:'此目录暂无已回传文件',
    row:(file,page)=>`<tr><td>${fileDisplayName(file.path,page)}</td><td>${size(file.size)}</td><td>${file.sha256
      ? `<code title="${esc(file.sha256)}">${esc(file.sha256.slice(0,16))}…</code>`
      : button('file-browser-hash','计算 SHA-256',`data-path="${esc(file.path)}"`,'small')}</td><td>${fileDownload(url+'path=',file.path)}</td></tr>`
  });
};

const fileBrowserBaseSubmit = submitForm;
submitForm = async function(form) {
  if (!form.hasAttribute('data-file-browser-search')) return fileBrowserBaseSubmit(form);
  const view = mountedFileBrowsers.get(form.parentElement);
  if (view) {
    Object.assign(view,{query:form.elements.query.value.trim(),offset:0});
    return readFileBrowser(view);
  }
};
