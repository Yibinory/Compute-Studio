"""Fetch and stage only release ZIPs from the official Compute Studio repository."""
import hashlib
import io
import json
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

from studio_launch import INSTALL_ROOT, PROGRAM_ROOT, UPDATE_DIR, program_version, update_state, version_key, version_source, write_update_state
from .files import relative
from .local_storage import root_lock
from .policy import ACTIVE
from .uploads import BUSY


REPO = 'Yibinory/Compute-Studio'
RELEASES = f'https://github.com/{REPO}/releases'
LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
MAX_ZIP = 32 * 1024**2
MAX_EXPANDED = 96 * 1024**2
HOSTS = {'api.github.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
ROOT_FILES = {'VERSION','app.py','studio_cli.py','studio_launch.py','requirements.txt','requirements-dev.txt',
              'README.md','AGENTS.md','CONTRIBUTING.md','THIRD_PARTY_NOTICES.md','.gitignore','.gitattributes',
              'start_web.bat','start_demo.bat'}
PROGRAM_DIRS = {'scheduler','static','templates','docs','tests','examples','scripts'}
FORBIDDEN = {'.git','.env','.venv','__pycache__','storage-location.json','storage-location-demo.json',
             'access-token.txt','session.key','controller.lock','compute.sqlite3','data','data-demo',
             'datasets','backups','logs','outputs','runs','.compute-updates'}
REQUIRED = {'VERSION','app.py','studio_launch.py','studio_cli.py','requirements.txt',
            'scheduler/engine.py','scheduler/updates.py','templates/index.html','static/app.js'}


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url=urlsplit(newurl)
        if url.scheme!='https' or url.netloc not in HOSTS:
            raise ValueError('GitHub 更新请求被重定向到未知地址')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch(url, limit):
    request=Request(url,headers={'User-Agent':'Compute-Studio','Accept':'application/vnd.github+json'})
    deadline=time.monotonic()+40
    try:
        with build_opener(GitHubRedirect()).open(request,timeout=12) as response:
            result=bytearray()
            while block:=response.read(min(1024**2,limit+1-len(result))):
                result.extend(block)
                if len(result)>limit:
                    raise ValueError('更新下载超过大小限制')
                if time.monotonic()>deadline:
                    raise ValueError('更新下载超时，请重试')
            return bytes(result)
    except HTTPError:
        raise
    except (URLError, OSError) as exc:
        raise ValueError('无法连接 GitHub，请检查控制器的网络后重试') from exc


def official_release():
    try:
        data=json.loads(fetch(LATEST, 512*1024))
    except HTTPError as exc:
        if exc.code==404:
            return None
        if exc.code in (403,429):
            raise ValueError('GitHub 暂时限制更新查询，请稍后重试') from exc
        raise ValueError(f'GitHub 更新查询暂时不可用（HTTP {exc.code}）') from exc
    except json.JSONDecodeError as exc:
        raise ValueError('GitHub 返回的发布信息无法读取') from exc
    if data.get('draft') or data.get('prerelease'):
        return None
    tag=data.get('tag_name','')
    version=tag.removeprefix('v')
    version_key(version)
    expected=f'compute-studio-v{version}.zip'
    package=f'{RELEASES}/download/{tag}/{expected}'
    asset=next((item for item in data.get('assets',[]) if item.get('name')==expected),{})
    digest=asset.get('digest') or ''
    ready=(asset.get('browser_download_url')==package and
           isinstance(asset.get('size'),int) and 0<asset['size']<=MAX_ZIP and
           bool(re.fullmatch(r'sha256:[a-f0-9]{64}',digest)))
    return {'version':version,'notes':str(data.get('body') or '')[:6000],'published_at':data.get('published_at'),
            'url':f'{RELEASES}/tag/{tag}','asset':{'url':package,'sha256':digest[7:],'size':asset['size']} if ready else None}


def extract_release(payload,target):
    try:
        archive=ZipFile(io.BytesIO(payload))
    except BadZipFile as exc:
        raise ValueError('发布包不是有效的 ZIP') from exc
    with archive:
        entries=archive.infolist()
        if len(entries)>10000 or sum(item.file_size for item in entries)>MAX_EXPANDED:
            raise ValueError('发布包解压后的文件过多或体积过大')
        seen,files=set(),set()
        for item in entries:
            path=item.filename.rstrip('/')
            if path!=relative(path):
                raise ValueError('发布包包含无效路径')
            parts=path.split('/')
            mode=stat.S_IFMT(item.external_attr>>16)
            if (path.casefold() in seen or any(p.casefold() in FORBIDDEN for p in parts)
                or not (path in ROOT_FILES or parts[0] in PROGRAM_DIRS)
                or mode not in (0,stat.S_IFREG,stat.S_IFDIR)):
                raise ValueError('发布包含重复、链接或非程序文件')
            seen.add(path.casefold())
            if item.is_dir():
                continue
            files.add(path)
            output=target.joinpath(*parts)
            output.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(item) as source,output.open('xb') as dest:
                shutil.copyfileobj(source,dest)
        if not REQUIRED<=files:
            raise ValueError('发布包缺少控制器程序文件')


def workspace_busy(root):
    path=Path(root)/'compute.sqlite3'
    if not path.is_file():
        return False
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        for kind,status in db.execute("SELECT kind,json_extract(data,'$.status') FROM records "
                                     "WHERE kind IN ('jobs','uploads','tasks')"):
            if status in {'jobs':{'queued','running'},'uploads':BUSY,'tasks':ACTIVE}[kind]:
                return True
        return False
    finally:
        db.close()


class Updates:
    def __init__(self,allow_install,home=INSTALL_ROOT,source=PROGRAM_ROOT):
        self.allow_install=allow_install
        self.home=home
        self.source=source
        self.version=program_version(source)
        self.lock=threading.Lock()

    def status(self):
        selected=update_state(self.home)
        return {'current':self.version,'pending':selected.get('pending'),'error':selected.get('error'),
                'can_install':self.allow_install,'releases_url':RELEASES}

    def check(self):
        release=official_release()
        newer=release is not None and version_key(release['version'])>version_key(self.version)
        latest={k:v for k,v in release.items() if k!='asset'} if release else None
        return {**self.status(),'latest':latest,'available':newer,
                'has_package':bool(release and release['asset'])}

    def stage(self,version):
        if not self.allow_install:
            raise ValueError('请从正式控制器执行工具更新')
        if version_key(version)<=version_key(self.version):
            raise ValueError('请选择比当前版本新的正式发布')
        if not self.lock.acquire(blocking=False):
            raise ValueError('已有更新正在准备')
        try:
            release=official_release()
            if not release or release['version']!=version or not release['asset']:
                raise ValueError('这个版本没有可核验的正式安装包，请重新检查更新')
            asset=release['asset']
            try:
                payload=fetch(asset['url'],asset['size'])
            except HTTPError as exc:
                raise ValueError(f'安装包下载失败（HTTP {exc.code}）') from exc
            if len(payload)!=asset['size'] or hashlib.sha256(payload).hexdigest()!=asset['sha256']:
                raise ValueError('安装包的 SHA-256 校验失败')
            self._stage_package(version,payload)
            return self.status()
        finally:
            self.lock.release()

    def _stage_package(self,version,payload):
        updates=self.home/UPDATE_DIR
        versions=updates/'versions'
        versions.mkdir(parents=True,exist_ok=True)
        guard=root_lock(updates)
        try:
            selected=update_state(self.home)
            package_hash=hashlib.sha256(payload).hexdigest()
            target=versions/version
            if target.exists():
                target=version_source(version,self.home)
                receipt=target/'.package-sha256'
                if not receipt.is_file() or receipt.read_text(encoding='ascii').strip()!=package_hash:
                    raise ValueError('已存在 SHA-256 不同的同版本程序；请使用新修订号')
                write_update_state({**selected,'pending':version,'error':None},self.home)
                return
            with tempfile.TemporaryDirectory(prefix='stage-',dir=updates) as work:
                working=Path(work)
                package=working/'package';package.mkdir()
                extract_release(payload,package)
                if program_version(package)!=version:
                    raise ValueError('安装包版本与 GitHub 发布不一致')
                if (package/'requirements.txt').read_text().splitlines()!=(self.source/'requirements.txt').read_text().splitlines():
                    raise ValueError('这个版本变更了运行依赖，请按发布页说明安装')
                smoke=("from pathlib import Path; import sys; from app import create_app; "
                       "a=create_app(Path(sys.argv[1])); "
                       "assert a.test_client().get('/').status_code==200; a.extensions['engine'].close()")
                try:
                    result=subprocess.run([sys.executable,'-B','-c',smoke,str(working/'test-data')],cwd=package,
                                          capture_output=True,timeout=35)
                except (OSError,subprocess.TimeoutExpired) as exc:
                    raise ValueError('新版程序未通过启动检查') from exc
                if result.returncode:
                    raise ValueError('新版程序未通过启动检查')
                (package/'.package-sha256').write_text(package_hash,encoding='ascii')
                package.rename(target)
                try:
                    write_update_state({**selected,'pending':version,'error':None},self.home)
                except OSError:
                    target.rename(package)
                    raise
        finally:
            guard.close()
