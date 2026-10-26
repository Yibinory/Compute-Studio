"""Project-scoped immutable content and lightweight run catalogs."""
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import threading
from pathlib import Path

from .files import confined, digest, file_metadata, relative


IGNORED={'.git','.venv','node_modules','__pycache__','.pytest_cache'}
OBJECT_LOCK_GUARD=threading.Lock()


def patterns(value):
    if not isinstance(value,list) or any(not isinstance(p,str) or not p or p.startswith('/') or '\\' in p or '..' in p.split('/') for p in value):
        raise ValueError('文件选择须为安全的相对路径或 glob 数组')
    return list(dict.fromkeys(value))


def matches(path,selection):
    return any(fnmatch.fnmatchcase(path,p) or Path(path).match(p) or p.startswith('**/') and fnmatch.fnmatchcase(path,p[3:]) for p in selection)


def own_outputs(task):
    inputs=task.get('input_manifest',{})
    return {p:v for p,v in task.get('result_manifest',{}).items() if not p.startswith('deps/') and inputs.get(p,{}).get('sha256')!=v['sha256']}


def object_files(value):
    """Validated logical paths to existing project content, never external paths."""
    if not isinstance(value,dict) or len(value)>10000:raise ValueError('内容引用须为相对文件路径到 hash／大小的映射，最多 10000 项')
    result={}
    for raw,item in value.items():
        if not isinstance(raw,str) or raw in ('.',''):raise ValueError('内容引用须提供相对文件路径')
        path=relative(raw)
        if path=='deps' or path.startswith('deps/'):raise ValueError('内容引用不能占用 deps 保留目录')
        if not isinstance(item,dict):raise ValueError('内容引用须提供 sha256 和 size')
        sha=item.get('sha256');size=item.get('size')
        if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('内容引用 hash 无效')
        if isinstance(size,bool) or not isinstance(size,int) or size<0:raise ValueError('内容引用大小须为非负整数字节数')
        if any(path==p or path.startswith(p+'/') or p.startswith(path+'/') for p in result):raise ValueError('内容引用路径重复或文件目录冲突')
        result[path]={'sha256':sha,'size':size}
    return result


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as f:
        json.dump(value,f,ensure_ascii=False);temporary=Path(f.name)
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


class Content:
    def __init__(self,engine,project_id):
        self.engine=engine;self.project_id=project_id;self.project=engine.local_project(project_id)
        if self.linked_directory(self.project) or self.project.resolve().parent!=(engine.root/'projects').resolve():raise ValueError('项目目录越界或为链接')
        for name in ('objects','runs','code','tasks'):
            if self.linked_directory(self.project/name):raise ValueError('项目子目录为链接，拒绝内容操作')
        self.root=self.project/'objects';self.root.mkdir(exist_ok=True)

    @staticmethod
    def linked_directory(path):
        return path.is_symlink() or path.exists() and bool(getattr(path.lstat(),'st_file_attributes',0)&0x400)

    def path(self,sha):
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('非法内容 hash')
        folder=self.root/sha[:2];dest=folder/sha
        if self.linked_directory(folder) or dest.is_symlink():raise ValueError('内容路径含链接')
        return dest

    @staticmethod
    def stamp(path):
        s=path.stat();return [s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino]

    def hash_file(self,path,check=None):
        path=Path(path);before=self.stamp(path);key=str(path.resolve())
        try:cached=self.engine.store.get('file_hashes',key)
        except ValueError:cached={}
        if cached.get('stamp')==before:return cached['sha256']
        hook=getattr(self,'hash_progress',None)
        sha=digest(path,progress=(lambda n:hook(path,n,before[0])) if hook else None,check=check)
        if self.stamp(path)!=before:raise ValueError('计算 hash 期间文件发生变化：'+str(path))
        self.engine.store.put('file_hashes',{'id':key,'project_id':self.project_id,'stamp':before,'sha256':sha})
        return sha

    def contains(self,sha,size,check=None):
        with self.object_lock(sha):return self._contains(sha,size,check)

    def object_lock(self,sha):
        with OBJECT_LOCK_GUARD:
            if not hasattr(self.engine,'content_locks'):self.engine.content_locks={}
            return self.engine.content_locks.setdefault((self.project_id,sha),threading.RLock())

    def _contains(self,sha,size,check=None):
        path=self.path(sha)
        if not path.is_file() or path.is_symlink() or path.stat().st_size!=size:return False
        return self.hash_file(path,check)==sha

    def ingest(self,source,item=None,check=None,link=False):
        source=Path(source)
        if source.is_symlink() or not source.is_file():raise ValueError('不支持符号链接或特殊文件')
        sha=self.hash_file(source,check);size=source.stat().st_size
        with self.object_lock(sha):return self._ingest(source,sha,size,item,check,link)

    def _ingest(self,source,sha,size,item,check,link):
        if self.hash_file(source,check)!=sha:raise ValueError('入库前源文件发生变化')
        if item and (sha!=item['sha256'] or size!=item['size']):raise ValueError('本机文件被修改或删除：'+str(source))
        dest=self.path(sha)
        if not self.contains(sha,size,check):
            dest.parent.mkdir(exist_ok=True)
            temporary=dest.with_name(sha+'.partial')
            if temporary.exists():temporary.unlink()
            try:
                if link:os.link(source,temporary)
                else:
                    with source.open('rb') as reader,temporary.open('wb') as writer:
                        while True:
                            if check:check()
                            data=reader.read(1024*1024)
                            if not data:break
                            writer.write(data)
                if not link and self.hash_file(temporary,check)!=sha:raise ValueError('内容入库校验失败')
                if dest.exists():os.chmod(dest,stat.S_IWRITE|stat.S_IREAD)
                os.replace(temporary,dest)
                for p in (dest,source) if link else (dest,):
                    self.engine.store.put('file_hashes',{'id':str(p.resolve()),'project_id':self.project_id,'stamp':self.stamp(p),'sha256':sha})
            finally:temporary.unlink(missing_ok=True)
        return {'sha256':sha,'size':size}

    def tree(self,root,selection=None,exclude=None,check=None):
        root=Path(root);result={}
        if self.project.resolve() not in root.resolve().parents:raise ValueError('输入目录越界')
        for entry in file_metadata(root,check=check):
            p=entry['path']
            if any(part in IGNORED for part in Path(p).parts) or p.endswith(('.pyc','.pyo')):continue
            if selection is not None and not matches(p,selection) or exclude and matches(p,exclude):continue
            result[p]=self.ingest(confined(root,p),check=check)
        return result

    def catalog(self,attempt):
        run=confined(self.project/'runs',attempt)
        if self.linked_directory(run):raise ValueError('运行目录为链接')
        return run/'catalog.json'

    def read_catalog(self,attempt):
        path=self.catalog(attempt)
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}

    def return_destination(self,attempt,scope):
        if scope not in ('result','control','run.log'):raise ValueError('无效回传目标')
        run=self.catalog(attempt).parent
        target=run/scope
        if self.linked_directory(target):raise ValueError('回传目标是链接或目录联接，拒绝覆盖其他文件')
        return target

    def save_catalog(self,attempt,**value):
        prior=self.read_catalog(attempt);atomic_json(self.catalog(attempt),{**prior,**value,'version':2})

    def build(self,task,check=None,save=True):
        check=check or (lambda:None)
        version=self.engine.code_sources.snapshot(self.project_id,check) if hasattr(self.engine,'code_sources') else None
        follow=task.get('follow_project_files',[])
        selected=task.get('code_files');exclude=task.get('code_exclude') or []
        if version:
            all_public=version['files'];code_revision=version['id']
        else:
            # Detect edits across multiple files during capture, including additions
            # and deletions. Running workspaces only ever use the resulting hashes.
            from .source_files import scan
            before=scan(self.project/'code',[],check,ignore=IGNORED)
            inclusion=None if selected is None else list(dict.fromkeys(selected+follow))
            all_public=self.tree(self.project/'code',selection=inclusion,exclude=exclude,check=check)
            if before!=scan(self.project/'code',[],check,ignore=IGNORED):raise ValueError('公共代码在准备期间发生变化，等待编辑完成后重试')
            code_revision=hashlib.sha256(json.dumps(all_public,sort_keys=True).encode()).hexdigest()
        all_public={p:v for p,v in all_public.items() if not matches(p,exclude)}
        public={p:v for p,v in all_public.items() if selected is None or matches(p,selected)}
        followed={p:v for p,v in all_public.items() if matches(p,follow)}
        for pattern in follow:
            if not any(matches(p,[pattern]) for p in followed):raise ValueError('跟随项目代码的规则没有匹配文件：'+pattern)
        public.update(followed)
        for item in public.values():
            if not self.contains(item['sha256'],item['size'],check):raise ValueError('已固定的项目代码内容缺失，请核验源码同步')
        task_root=self.project/'tasks'/task['id']/'files'
        from .source_files import scan
        before_task=scan(task_root,[],check,ignore=IGNORED)
        files=dict(public);files.update(self.tree(task_root,check=check));files.update(followed)
        if before_task!=scan(task_root,[],check,ignore=IGNORED):raise ValueError('任务文件在准备期间发生变化，未使用不完整版本')
        if any(p=='deps' or p.startswith('deps/') for p in files):raise ValueError('deps 是保留目录')
        references=object_files(task.get('object_files',{}))
        for path,item in references.items():
            if any(path==p or path.startswith(p+'/') or p.startswith(path+'/') for p in files):raise ValueError('内容引用与代码／任务文件冲突：'+path)
            if not self.contains(item['sha256'],item['size'],check):raise ValueError('项目内容引用缺失或损坏：'+path+' / '+item['sha256'])
            files[path]=item
        selection=task.get('dependency_files',{})
        dependency_counts={}
        for dep_id in task['depends_on']:
            dep=self.engine.store.get('tasks',dep_id)
            if dep['status']!='success' or not dep.get('synced_at'):raise ValueError('前序尚未成功回传')
            inventory=own_outputs(dep);chosen=selection.get(dep_id)
            if chosen is not None:
                # Explicit rules can address the predecessor's input snapshot
                # (including an earlier dependency) without forwarding it by default.
                cat=self.read_catalog(dep['attempt_id'])
                inventory={**dep.get('input_manifest',{}),**dep.get('result_manifest',{}),**cat.get('inputs',{}),**cat.get('artifacts',{}),**cat.get('outputs',{})}
                for pattern in chosen:
                    if not any(matches(p,[pattern]) for p in inventory):raise ValueError('前序没有匹配的输出：'+dep['name']+' / '+pattern)
                inventory={p:v for p,v in inventory.items() if matches(p,chosen)}
            for p,item in inventory.items():
                if not self.contains(item['sha256'],item['size'],check):
                    legacy=confined(self.project/'runs'/dep['attempt_id']/'result',p)
                    if not legacy.is_file():raise ValueError('前序本机结果被修改或删除，请恢复：'+dep['name']+' / '+p)
                    self.ingest(legacy,item,check)
                files['deps/'+dep_id+'/'+p]=item
            dependency_counts[dep_id]={'files':len(inventory),'bytes':sum(v['size'] for v in inventory.values())}
        input_revision=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
        if save:self.save_catalog(task['attempt_id'],inputs=files,public=public,read_only_files=list(references),dependencies=dependency_counts,created_at=time.time(),code_revision=code_revision,input_revision=input_revision)
        return files,public

    def receive(self,remote,spec,inventory,check=None,progress=None):
        unique={v['sha256']:v for v in inventory.values()};missing={h:v for h,v in unique.items() if not self.contains(h,v['size'],check)}
        total=sum(v['size'] for v in missing.values());done=0;downloaded=0;resumed=0
        for sha,item in missing.items():
            if check:check()
            with self.object_lock(sha):
                if self.contains(sha,item['size'],check):continue
                dest=self.path(sha);dest.parent.mkdir(exist_ok=True);temporary=dest.with_name(sha+'.download')
                for attempt in range(2):
                    transfer=remote.download_object(spec,sha,temporary,check=check,progress=(lambda amount,length:progress(done+amount,total)) if progress else None)
                    downloaded+=(transfer or {}).get('downloaded_bytes',item['size']);resumed+=(transfer or {}).get('resumed_bytes',0)
                    if digest(temporary,check=check)==sha:break
                    # A partial may have been externally changed. Retry this
                    # one immutable object from zero, never rerun the experiment.
                    temporary.unlink(missing_ok=True)
                    if attempt:raise ValueError('回传 hash 不一致：'+sha)
                os.replace(temporary,dest);self.hash_file(dest,check);done+=item['size']
            if progress:progress(done,total)
        return {'downloaded_bytes':downloaded,'reused_bytes':max(0,sum(v['size'] for v in unique.values())-downloaded),'resumed_bytes':resumed}

    def artifact(self,attempt,path):
        cat=self.read_catalog(attempt);inventory=cat.get('artifacts',cat.get('outputs',{}));item=inventory.get(relative(path))
        if item:
            if not self.contains(item['sha256'],item['size']):raise ValueError('本机结果内容缺失或损坏，请核验存储')
            return self.path(item['sha256'])
        return confined(self.project/'runs'/attempt/'result',path)

    def stats(self):
        physical=0;count=0
        for folder in self.root.iterdir():
            if folder.is_dir():
                for p in folder.iterdir():
                    if p.is_file() and len(p.name)==64:physical+=p.stat().st_size;count+=1
        logical=0;runs=0;legacy=0;run_rows=[]
        labels={}
        for task in self.engine.store.summaries('tasks'):
            if task['project_id']!=self.project_id:continue
            for attempt in [task.get('attempt_id')]+[a['attempt_id'] for a in task.get('previous_attempts',[])]:
                if attempt:labels[attempt]=task['name']
        for run in (self.project/'runs').glob('*'):
            if not run.is_dir():continue
            runs+=1;cat=self.read_catalog(run.name)
            inputs=sum(v['size'] for v in cat.get('inputs',{}).values());outputs=sum(v['size'] for v in cat.get('outputs',{}).values());old_bytes=0
            logical+=inputs+sum(v['size'] for v in cat.get('artifacts',cat.get('outputs',{})).values())
            for scope in ('input','result'):
                old_bytes+=sum(p.stat().st_size for p in (run/scope).rglob('*') if p.is_file() and not p.is_symlink())
            legacy+=old_bytes;run_rows.append({'attempt_id':run.name,'name':labels.get(run.name,'未完成／历史运行'),'input_bytes':inputs,'output_bytes':outputs,'legacy_bytes':old_bytes,'catalog':bool(cat)})
        return {'objects':count,'object_bytes':physical,'referenced_bytes':logical,'legacy_bytes':legacy,'runs':runs,'saved_bytes':max(0,logical-physical),'run_rows':run_rows}

    def run_files(self,attempt,scope='outputs'):
        if len(attempt)!=32 or any(c not in '0123456789abcdef' for c in attempt):raise ValueError('运行 ID 无效')
        if scope not in ('inputs','outputs','workspace'):raise ValueError('文件范围无效')
        cat=self.read_catalog(attempt)
        if cat:
            if scope=='inputs':return cat.get('inputs',{})
            if scope=='outputs':return cat.get('outputs',cat.get('artifacts',{}))
            return cat.get('artifacts',{**cat.get('inputs',{}),**cat.get('outputs',{})})
        folder=self.project/'runs'/attempt/('input' if scope=='inputs' else 'result')
        if self.linked_directory(folder) or self.project.resolve() not in folder.resolve().parents:raise ValueError('历史目录为链接或越界')
        return {e['path']:{'size':e['size']} for e in file_metadata(folder)}

    def compact(self,job=None):
        e=self.engine
        if any(t['project_id']==self.project_id and t['status'] in ('staging','launching','running','suspended','canceling','syncing','sync_failed','lost') for t in e.store.summaries('tasks')):raise ValueError('项目仍有执行或待回传的实验，不能整理')
        runs=sorted(p for p in (self.project/'runs').glob('*') if p.is_dir() and len(p.name)==32)
        done=0;removed=0
        last=[0]
        def report(path,amount,total):
            now=time.monotonic()
            if now-last[0]<.5:return
            last[0]=now;e.progress(job,phase='hashing',bytes=done+amount,total=0,label='核验并复用历史文件',path=str(path.relative_to(self.project)))
        self.hash_progress=report
        for run in runs:
            if self.linked_directory(run):raise ValueError('运行目录为链接，拒绝整理')
            check=lambda:e.check_cancel(job)
            cat=self.read_catalog(run.name)
            for scope,key in (('input','inputs'),('result','artifacts')):
                tree=run/scope
                if not tree.is_dir():continue
                if self.linked_directory(tree) or self.project.resolve() not in tree.resolve().parents:raise ValueError('历史目录为链接或越界')
                inventory=dict(cat.get(key,{}))
                for entry in file_metadata(tree,check=check):
                    check();p=entry['path'];item=self.ingest(confined(tree,p),check=check,link=True);inventory[p]=item;done+=item['size']
                    e.progress(job,phase='hashing',bytes=done,total=0,label='核验并复用历史文件',path=run.name+'/'+scope+'/'+p)
                # Publish references before removal. A canceled/restarted migration
                # can always resume, and downloads can fall back to the catalog.
                self.save_catalog(run.name,**{key:inventory})
                cat=self.read_catalog(run.name)
                for p,item in inventory.items():
                    check();source=confined(tree,p)
                    if not self.contains(item['sha256'],item['size'],check):raise ValueError('保留内容已丢失，停止整理')
                    if source.is_file():
                        if self.hash_file(source,check)!=item['sha256']:raise ValueError('整理期间历史文件发生变化')
                        dest=self.path(item['sha256']);shared=source.stat().st_ino==dest.stat().st_ino
                        source.unlink();removed+=item['size']
                        if shared:self.engine.store.put('file_hashes',{'id':str(dest.resolve()),'project_id':self.project_id,'stamp':self.stamp(dest),'sha256':item['sha256']})
                if self.linked_directory(tree) or self.project.resolve() not in tree.resolve().parents:raise ValueError('清理前历史目录发生变化，已停止')
                shutil.rmtree(tree)
            if cat.get('artifacts') is not None:
                outputs={p:v for p,v in cat['artifacts'].items() if not p.startswith('deps/') and cat.get('inputs',{}).get(p,{}).get('sha256')!=v['sha256']}
                self.save_catalog(run.name,outputs=outputs)
        return {'processed_bytes':done,'removed_logical_bytes':removed,**self.stats()}
