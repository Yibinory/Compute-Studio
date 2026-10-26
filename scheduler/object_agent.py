"""SSH-side content-addressed project storage; imported by the small agent."""
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import contextvars
from pathlib import Path

STOP=contextvars.ContextVar('content-stop',default=None)


def check():
    path=STOP.get()
    if path and path.exists():raise ValueError('内容操作已中断，已完成文件保留')


def sha_path(base,sha):
    if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('非法内容 hash')
    root=base/'objects';folder=root/sha[:2];dest=folder/sha
    if root.is_symlink() or folder.is_symlink() or dest.is_symlink():raise ValueError('内容路径含符号链接')
    return dest


def child(base,path):
    if base.is_symlink():raise ValueError('文件根目录含符号链接')
    if not isinstance(path,str) or not path or path.startswith('/') or '\\' in path or '..' in path.split('/') or '\x00' in path:raise ValueError('非法文件路径')
    p=base/path
    for parent in p.parents:
        if parent==base:break
        if parent.is_symlink():raise ValueError('文件父目录含符号链接')
    return p


def stamp(path):
    s=path.stat();return [s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino]


def file_hash(path):
    h=hashlib.sha256();before=stamp(path)
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            check();h.update(chunk)
    if stamp(path)!=before:raise ValueError('计算 hash 期间文件发生变化')
    return h.hexdigest()


def atomic(path,value):
    if path.parent.is_symlink():raise ValueError('元数据目录含符号链接')
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.tmp')
    if temporary.is_symlink():raise ValueError('元数据临时文件含符号链接')
    temporary.write_text(json.dumps(value),encoding='utf-8');os.replace(temporary,path)


def read(path):
    return json.loads(path.read_text()) if path.is_file() else {}


@contextlib.contextmanager
def index(base):
    meta=base/'_meta';meta.mkdir(exist_ok=True)
    if meta.is_symlink():raise ValueError('索引目录含符号链接')
    if any((meta/name).is_symlink() for name in ('content.lock','objects.sqlite3','objects.sqlite3-journal')):raise ValueError('索引文件含符号链接')
    with (meta/'content.lock').open('a+b') as lock:
        if os.name=='posix':
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX)
        db=sqlite3.connect(str(meta/'objects.sqlite3'),timeout=60)
        db.execute('CREATE TABLE IF NOT EXISTS objects (sha TEXT PRIMARY KEY,size INTEGER,stamp TEXT,path TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY,sha TEXT,stamp TEXT)')
        try:
            with db:yield db
        finally:db.close()


def register(db,base,sha):
    p=sha_path(base,sha);s=stamp(p)
    db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?,?)',(sha,s[0],json.dumps(s),str(p)))


def cached_hash(db,path):
    current=stamp(path);row=db.execute('SELECT sha,stamp FROM files WHERE path=?',(str(path),)).fetchone()
    if row and json.loads(row[1])==current:return row[0]
    sha=file_hash(path);db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)',(str(path),sha,json.dumps(stamp(path))))
    return sha


def valid(db,base,sha,size=None,force=False):
    p=sha_path(base,sha)
    if not p.is_file() or size is not None and p.stat().st_size!=size:
        db.execute('DELETE FROM objects WHERE sha=?',(sha,));return False
    current=stamp(p);row=db.execute('SELECT stamp FROM objects WHERE sha=?',(sha,)).fetchone()
    if force or not row or json.loads(row[0])!=current:
        if file_hash(p)!=sha:db.execute('DELETE FROM objects WHERE sha=?',(sha,));return False
        register(db,base,sha)
    return True


def store_file(db,base,path,sha=None):
    sha=sha or file_hash(path);dest=sha_path(base,sha)
    if not valid(db,base,sha,path.stat().st_size):
        dest.parent.mkdir(parents=True,exist_ok=True);temporary=dest.with_name(sha+'.store')
        if temporary.exists():temporary.unlink()
        try:os.link(path,temporary)
        except OSError:shutil.copy2(path,temporary)
        os.replace(temporary,dest)
    if os.name=='posix':os.chmod(dest,0o444)
    register(db,base,sha)
    return sha


def clone(source,dest):
    dest.parent.mkdir(parents=True,exist_ok=True);temporary=dest.with_name(dest.name+'.materialize')
    if dest.is_symlink():dest.unlink()
    if temporary.is_symlink():raise ValueError('工作区临时文件含符号链接')
    try:
        copied=False
        if os.name=='posix':
            import fcntl
            try:
                with source.open('rb') as a,temporary.open('wb') as b:fcntl.ioctl(b,0x40049409,a.fileno())
                shutil.copystat(source,temporary);copied=True
            except OSError:pass
        if not copied:shutil.copy2(source,temporary)
        os.chmod(temporary,0o644);os.replace(temporary,dest)
    finally:
        if temporary.exists():temporary.unlink()


def managed_hash(path,base):
    if not path.is_symlink():return None
    target=path.resolve()
    if re.fullmatch('[a-f0-9]{64}',target.name) and target==sha_path(base,target.name).resolve():return target.name
    raise ValueError('非工具管理的符号链接：'+str(path))


def seal_alias(path,base,sha):
    if os.name=='posix':
        import uuid
        temporary=path.with_name('.alias-'+uuid.uuid4().hex)
        try:temporary.symlink_to(sha_path(base,sha));os.replace(temporary,path)
        finally:
            if temporary.is_symlink():temporary.unlink()
    # Windows filesystem doubles use copies; real execution hosts are Linux.


def run_path(base,attempt):
    if not re.fullmatch('[a-f0-9]{32}',attempt or ''):raise ValueError('非法运行 ID')
    run=base/'runs'/attempt
    if run.is_symlink() or (base/'runs').is_symlink():raise ValueError('运行目录含符号链接')
    return run


def manifest_cached(base,path):
    if path.is_symlink():raise ValueError('清单目录含符号链接')
    files={}
    with index(base) as db:
        for current,dirs,names in os.walk(path,followlinks=False):
            dirs[:]=[d for d in dirs if d not in ('.git','.venv','__pycache__','node_modules','.pytest_cache')]
            if Path(current)==base:dirs[:]=[d for d in dirs if d not in ('objects','_meta')]
            for d in dirs:
                if (Path(current)/d).is_symlink():raise ValueError('目录含符号链接')
            for name in names:
                p=Path(current)/name
                sha=managed_hash(p,base)
                if sha:
                    if not valid(db,base,sha):raise ValueError('引用的服务器内容已丢失：'+sha)
                else:sha=cached_hash(db,p)
                files[p.relative_to(path).as_posix()]={'sha256':sha,'size':p.stat().st_size}
    return {'files':files,'exists':path.is_dir(),'skipped':[]}


def dispatch(action,spec,base,status):
    token=spec.get('content_token')
    if token is not None and not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('非法中断标识')
    stop=base/'_meta'/('stop-'+token) if token else None
    if action=='content_stop':
        if stop is None or stop.parent.is_symlink() or stop.is_symlink():raise ValueError('中断标记路径无效')
        stop.parent.mkdir(exist_ok=True);stop.touch();return {'stopped':True}
    context=STOP.set(stop)
    try:return _dispatch(action,spec,base,status)
    finally:
        STOP.reset(context)
        if stop and stop.exists():stop.unlink()


def _dispatch(action,spec,base,status):
    if action=='content_replica':
        inventory=manifest_cached(base,base/'code')['files'] if (base/'code').is_dir() else {}
        return {'files':{'code/'+p:v for p,v in inventory.items()},'exists':base.is_dir(),'skipped':[]}
    if action=='content_export':
        files={}
        for scope in ('code','runs','code-history'):
            folder=base/scope
            if folder.is_dir():files.update({scope+'/'+p:v for p,v in manifest_cached(base,folder)['files'].items()})
        with index(base) as db:
            for p,item in files.items():
                source=child(base,p)
                if not source.is_symlink():store_file(db,base,source,item['sha256'])
        return {'files':files,'exists':base.is_dir(),'skipped':[]}
    if action=='content_need':
        missing=[];available=[]
        with index(base) as db:
            for sha,item in spec['objects'].items():
                check()
                (available if valid(db,base,sha,item['size']) else missing).append(sha)
        return {'missing':missing,'available':available,'checked_at':time.time()}
    if action=='content_prepare':
        sha=spec['sha256'];dest=sha_path(base,sha);dest.parent.mkdir(parents=True,exist_ok=True)
        partial=dest.with_name(sha+'.upload')
        if partial.is_symlink():raise ValueError('临时文件含符号链接')
        return {'path':str(partial)}
    if action=='content_commit':
        sha=spec['sha256'];dest=sha_path(base,sha);partial=dest.with_name(sha+'.upload')
        if partial.is_symlink() or file_hash(partial)!=sha or partial.stat().st_size!=spec['size']:raise ValueError('上传内容核验失败')
        with index(base) as db:
            os.replace(partial,dest)
            if os.name=='posix':os.chmod(dest,0o444)
            register(db,base,sha)
        return {'ok':True}
    if action=='content_read':
        with index(base) as db:
            if not valid(db,base,spec['sha256']):raise ValueError('服务器内容缺失或损坏')
        return {'path':str(sha_path(base,spec['sha256']))}
    if action=='content_assemble':
        run=run_path(base,spec['attempt_id']);state=status(spec)
        if state['state'] not in ('absent',):raise ValueError('运行已经启动，不能重新整理输入')
        files=spec['files'];public=spec.get('public',{});read_only=spec.get('read_only_files',[])
        if not isinstance(read_only,list) or any(not isinstance(p,str) or p not in files for p in read_only):raise ValueError('只读内容引用须属于输入清单')
        workspace=run/'workspace';workspace.mkdir(parents=True,exist_ok=True)
        if (workspace/'deps').is_dir() and os.name=='posix':
            if (workspace/'deps').is_symlink():raise ValueError('前序目录含非管理链接')
            for current,dirs,names in os.walk(workspace/'deps',followlinks=False):
                if any((Path(current)/d).is_symlink() for d in dirs):raise ValueError('前序目录含非管理链接')
                os.chmod(current,0o755)
        baseline={};code_history={};public_state=read(base/'_meta'/'public.json')
        with index(base) as db:
            missing=[v['sha256'] for v in [*files.values(),*public.values()] if not valid(db,base,v['sha256'],v['size'])]
            if missing:return {'missing':list(dict.fromkeys(missing))}
            for p,item in public.items():
                dest=child(base/'code',p);sha=item['sha256'];source=sha_path(base,sha)
                old=None
                if dest.is_file():
                    prior=public_state.get(p,{})
                    old=prior.get('sha256') if prior.get('stamp')==stamp(dest) else file_hash(dest)
                    if old!=sha:
                        store_file(db,base,dest,old);code_history[p]={'sha256':old,'size':dest.stat().st_size}
                if old!=sha:clone(source,dest)
                public_state[p]={'sha256':sha,'stamp':stamp(dest)}
            for p,item in files.items():
                check()
                dest=child(workspace,p);source=sha_path(base,item['sha256'])
                dest.parent.mkdir(parents=True,exist_ok=True)
                if (p.startswith('deps/') or p in read_only) and os.name=='posix':
                    if dest.exists() or dest.is_symlink():
                        if dest.is_symlink():managed_hash(dest,base)
                        dest.unlink()
                    dest.symlink_to(source)
                else:clone(source,dest)
                baseline[p]=stamp(dest)
            for dep in spec.get('dependency_ids',[]):
                if not re.fullmatch('[a-f0-9]{32}',dep):raise ValueError('前序 ID 无效')
                child(workspace,'deps/'+dep).mkdir(parents=True,exist_ok=True)
            if (workspace/'deps').is_dir() and os.name=='posix':
                for current,dirs,names in os.walk(workspace/'deps',topdown=False,followlinks=False):os.chmod(current,0o555)
            atomic(run/'inputs.json',{'files':files,'stamps':baseline,'version':2})
            atomic(base/'_meta'/'public.json',public_state)
            if code_history:atomic(base/'code-history'/(str(time.time_ns())+'.json'),code_history)
        return {'missing':[],'files':len(files),'dependency_bytes':sum(v['size'] for p,v in files.items() if p.startswith('deps/'))}
    if action=='content_results':
        run=run_path(base,spec['attempt_id']);workspace=run/'workspace'
        if status(spec)['state']!='finished':raise ValueError('实验尚未结束，不能封存结果')
        if not workspace.is_dir():raise ValueError('远程运行文件已被删除，不能回传成功')
        baseline=read(run/'inputs.json');inputs=baseline.get('files',{});stamps=baseline.get('stamps',{});outputs={};extras={}
        import fnmatch
        selection=spec.get('output_files')
        with index(base) as db:
            for current,dirs,names in os.walk(workspace,followlinks=False):
                dirs[:]=[d for d in dirs if d not in ('.git','.venv','__pycache__','node_modules','.pytest_cache') and not (Path(current)==workspace and d=='deps')]
                for d in dirs:
                    if (Path(current)/d).is_symlink():raise ValueError('输出目录包含非管理的链接')
                for name in names:
                    check()
                    p=Path(current)/name;rel=p.relative_to(workspace).as_posix()
                    if not p.is_file():raise ValueError('输出含特殊文件')
                    sha=managed_hash(p,base)
                    if sha and not valid(db,base,sha):raise ValueError('结果引用丢失')
                    if not sha:sha=inputs[rel]['sha256'] if rel in inputs and stamp(p)==stamps.get(rel) else file_hash(p)
                    if inputs.get(rel,{}).get('sha256')==sha:
                        if not p.is_symlink():store_file(db,base,p,sha);seal_alias(p,base,sha);register(db,base,sha)
                        continue
                    size=p.stat().st_size
                    if not p.is_symlink():store_file(db,base,p,sha);seal_alias(p,base,sha);register(db,base,sha)
                    selected=selection is None or any(fnmatch.fnmatchcase(rel,g) or Path(rel).match(g) or g.startswith('**/') and fnmatch.fnmatchcase(rel,g[3:]) for g in selection)
                    (outputs if selected else extras)[rel]={'sha256':sha,'size':size}
            atomic(run/'outputs.json',{'files':outputs,'version':2,'sealed_at':time.time()})
            atomic(run/'unselected.json',{'files':extras})
        return {'files':outputs,'exists':True,'skipped':[]}
    if action=='content_scan':
        good=0;bad=[];total=0
        with index(base) as db:
            for folder in (base/'objects').glob('*'):
                if not folder.is_dir() or folder.is_symlink():continue
                for p in folder.iterdir():
                    check()
                    if not re.fullmatch('[a-f0-9]{64}',p.name):continue
                    if valid(db,base,p.name,force=True):good+=1;total+=p.stat().st_size
                    else:bad.append(p.name)
            for sha, in db.execute('SELECT sha FROM objects').fetchall():
                if not sha_path(base,sha).is_file():db.execute('DELETE FROM objects WHERE sha=?',(sha,))
            atomic(base/'_meta'/'scan.json',{'checked_at':time.time(),'objects':good,'bytes':total,'invalid':bad})
        return read(base/'_meta'/'scan.json')
    if action=='content_stats':
        with index(base) as db:
            count,total=db.execute('SELECT count(*),coalesce(sum(size),0) FROM objects').fetchone()
        return {**read(base/'_meta'/'scan.json'),'objects':count,'bytes':total}
    if action=='content_compact':
        runs=list((base/'runs').glob('*'))
        for run in runs:
            if status({**spec,'attempt_id':run.name})['state'] in ('running','paused','starting','lost'):raise ValueError('远程还有活动实验，拒绝整理')
        processed=0
        with index(base) as db:
            for run in runs:
                workspace=run/'workspace'
                if not workspace.is_dir():continue
                catalog={}
                for current,dirs,names in os.walk(workspace,followlinks=False):
                    for d in dirs:
                        if (Path(current)/d).is_symlink():raise ValueError('目录含非管理链接')
                    for name in names:
                        check()
                        p=Path(current)/name
                        if re.search(r'\.upload-[a-f0-9]{64}$',name):p.unlink();continue
                        sha=managed_hash(p,base)
                        if not sha:
                            sha=store_file(db,base,p);seal_alias(p,base,sha);register(db,base,sha)
                        elif not valid(db,base,sha):raise ValueError('内容引用损坏')
                        catalog[p.relative_to(workspace).as_posix()]={'sha256':sha,'size':p.stat().st_size};processed+=1
                atomic(run/'legacy-files.json',catalog)
            count,total=db.execute('SELECT count(*),coalesce(sum(size),0) FROM objects').fetchone()
        return {'files':processed,'compacted':True,'objects':count,'bytes':total}
    raise ValueError('未知内容操作')
