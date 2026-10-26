"""One-way editor source synchronization with immutable, per-run input versions."""
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .content import Content,atomic_json,patterns
from .file_process import run
from .files import confined
from .source_files import scan,stamp
from .storage import uid
from .operations import OperationCanceled

DEFAULT_EXCLUDE=['runs/**','outputs/**','datasets/**','weights/**','checkpoints/**','experiment_inputs/**','analysis/**','batch_import/**']


class CodeSources:
    def __init__(self,engine):
        self.engine=engine;self.guard=threading.RLock();self.locks={};self.pending={};self.last={}
        self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='code-source')

    def close(self):self.worker.shutdown(wait=True)

    def lock(self,pid):
        with self.guard:return self.locks.setdefault(pid,threading.RLock())

    def get(self,pid):
        self.engine.store.get('projects',pid)
        try:return self.engine.store.get('code_sources',pid)
        except ValueError:return {'id':pid,'path':'','enabled':False,'interval_seconds':10,'exclude':DEFAULT_EXCLUDE,'status':'managed'}

    def bound(self,pid):return bool(self.get(pid).get('path'))

    def folder(self,pid):
        path=confined(self.engine.local_project(pid),'source-sync');path.mkdir(exist_ok=True);return path

    def recover_publication(self,pid,value,pool):
        pending=self.folder(pid)/'pending.json'
        if pending.exists():
            recovery=json.loads(pending.read_text(encoding='utf-8'))
            if recovery.get('target_revision')!=value.get('revision'):
                self.restore(pool,recovery['before'],recovery['touched'])
                atomic_json(self.folder(pid)/'mirror.json',{'revision':value.get('revision'),'stamps':scan(pool.project/'code',[],lambda:None)})
                target=recovery.get('target_revision')
                if target:
                    for group in ('versions','history','changes'):confined(self.folder(pid),group+'/'+target+'.json').unlink(missing_ok=True)
            pending.unlink()

    def source(self,raw):
        value=Path(str(raw).strip()).expanduser()
        if not value.is_absolute() or str(value).startswith('\\\\') or not value.is_dir():raise ValueError('请填写控制器本机已存在的源码文件夹绝对路径')
        # Reject linked ancestors, drive roots, and storage recursion.
        for p in (value,*value.parents):
            if p.exists() and Content.linked_directory(p):raise ValueError('源码路径不支持符号链接或目录联接')
        value=value.resolve();root=self.engine.root.resolve()
        if value==Path(value.anchor) or value==root or root in value.parents or value in root.parents:
            raise ValueError('源码目录不能是磁盘根目录、工具保存目录，或其父／子目录')
        return value

    def configure(self,pid,data):
        e=self.engine
        lock=self.lock(pid)
        if not lock.acquire(blocking=False):raise ValueError('源码正在同步，请等待完成或中断后再保存配置')
        try:
            if e.storage_moving:raise ValueError('保存目录正在迁移')
            old=self.get(pid);raw=str(data.get('path','')).strip()
            self.recover_publication(pid,old,Content(e,pid))
            enabled=data.get('enabled',True)
            if not isinstance(enabled,bool):raise ValueError('enabled 须为布尔值')
            interval=data.get('interval_seconds',10)
            if isinstance(interval,bool) or not isinstance(interval,(int,float)) or not 5<=interval<=3600:raise ValueError('检查间隔须为 5–3600 秒')
            exclude=patterns(data.get('exclude',DEFAULT_EXCLUDE))
            if len(exclude)>200:raise ValueError('排除规则最多 200 条')
            if raw:raw=str(self.source(raw))
            if e.uploads and e.uploads.pending(pid):raise ValueError('公共文件正在导入或清除，请先完成')
            same=raw==old.get('path') and exclude==old.get('exclude')
            value={**old,'path':raw,'enabled':enabled if raw else False,'interval_seconds':int(interval),
                   'exclude':exclude,'configuration_id':uid(),'status':'waiting' if raw else 'managed','error':'','configured_at':time.time()}
            if same and old.get('manifest'):value['status']='ready' if enabled else 'paused'
            elif not raw:value.pop('manifest',None);value.pop('revision',None)
            # Keep the previous mirror version across a source change for rollback.
            if not same:value.pop('root_identity',None)
            e.store.put('code_sources',value);self.last[pid]=0
            e.store.event('项目代码同步配置 · '+e.store.get('projects',pid)['name']+' · '+(raw if raw else '以受管 code 目录为准'))
            return value
        finally:lock.release()

    def synchronize_now(self,pid,job=None):return self.synchronize(pid,job=job,force=True)

    def read_version(self,pid,value):
        if not value.get('manifest'):return {}
        return json.loads(confined(self.folder(pid),value['manifest']).read_text(encoding='utf-8'))

    def _worker(self,source,exclude,check,job=None,**options):
        def update(kind,event,scratch,pid):
            if kind=='progress' and job:self.engine.progress(job,**{k:v for k,v in event.items() if k!='type'},worker_pid=pid)
        return run(self.engine.root,'source-capture',source=str(source),exclude=exclude,check=check,update=update,**options)

    @staticmethod
    def copy_object(pool,path,item):
        path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name('.source-'+uid())
        try:
            shutil.copyfile(pool.path(item['sha256']),temporary)
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)

    def restore(self,pool,before,touched):
        code=pool.project/'code'
        for rel in sorted(touched,key=lambda p:(p.count('/'),p),reverse=True):
            target=confined(code,rel)
            if target.is_file():target.unlink()
        for current,dirs,files in os.walk(code,topdown=False):
            path=Path(current)
            if path!=code and not any(path.iterdir()):path.rmdir()
        for rel in touched:
            if rel in before:self.copy_object(pool,confined(code,rel),before[rel])

    def synchronize(self,pid,job=None,check=None,force=False):
        e=self.engine;check=check or (lambda:e.check_cancel(job))
        automatic=None
        with self.lock(pid):
            value=self.get(pid)
            if not value.get('path'):return {'status':'managed','changed':False}
            if not value.get('enabled') and not force:return {'status':'paused','changed':False}
            value.update(status='checking',error='');e.store.put('code_sources',value)
            try:
                if e.storage_moving:raise ValueError('本机保存目录正在迁移')
                if e.uploads and e.uploads.pending(pid,exclude_job=(job or {}).get('id')):raise ValueError('等待项目文件导入完成')
                pool=Content(e,pid);self.recover_publication(pid,value,pool)
                source=self.source(value['path']);code=pool.project/'code';folder=self.folder(pid)
                pending=folder/'pending.json'
                prior=self.read_version(pid,value)
                # A bound mirror has one writer; never silently overwrite manual edits.
                mirror_state=json.loads((folder/'mirror.json').read_text(encoding='utf-8')) if (folder/'mirror.json').exists() else {}
                expected_mirror=mirror_state.get('stamps',{}) if mirror_state.get('revision')==value.get('revision') else prior.get('mirror_stamps',{})
                if prior and scan(code,[],check)!=expected_mirror:
                    raise ValueError('工具 code 副本被手动修改；请把修改合并回源码目录，或解绑后使用该副本，未覆盖冲突')
                observed=self._worker(source,value['exclude'],check,scan_only=True)
                if value.get('root_identity') and observed['root_identity']!=value['root_identity']:
                    raise ValueError('源码根目录身份变化，可能挂载或文件夹被替换，请重新保存路径配置后同步')
                if prior.get('source_stamps')==observed['stamps'] and value.get('synced_configuration')==value['configuration_id'] and all(pool.contains(v['sha256'],v['size'],check) for v in prior.get('files',{}).values()):
                    value.update(status='ready' if value['enabled'] else 'paused',checked_at=time.time());e.store.put('code_sources',value)
                    return {'status':value['status'],'changed':False,'revision':value.get('revision')}
                if job is None:
                    automatic={'id':uid(),'label':'自动同步项目代码','operation':'synchronize:'+json.dumps([pid]),'status':'running',
                               'created_at':time.time(),'cancelable':True,'progress':{'phase':'source_sync','label':'等待源码稳定并检查增量'}}
                    e.store.put('jobs',automatic);e.cancel_signals[automatic['id']]=threading.Event()
                    previous_check=check
                    def check():previous_check();e.check_cancel(automatic)
                if e.stop_event.wait(getattr(self,'settle_seconds',2)):check()
                check()
                scratch=folder/('stage-'+uid());scratch.mkdir()
                try:
                    cache={p:{**v,'source_stamp':prior.get('source_stamps',{}).get(p)} for p,v in prior.get('files',{}).items()
                           if pool.contains(v['sha256'],v['size'],check)} if value.get('synced_configuration')==value['configuration_id'] else {}
                    captured=self._worker(source,value['exclude'],check,job=job or automatic,stage=str(scratch),expected=observed['stamps'],prior=cache)
                    files={p:{k:v[k] for k in ('sha256','size')} for p,v in captured['files'].items()}
                    for rel,item in files.items():
                        if (scratch/rel).is_file():pool.ingest(confined(scratch,rel),item,check)
                    before=prior.get('files') if prior else pool.tree(code,check=check)
                    added=sorted(set(files)-set(before));deleted=sorted(set(before)-set(files));modified=sorted(p for p in files.keys()&before.keys() if files[p]!=before[p])
                    changes=[{'path':p,'action':action,'before':before.get(p),'after':files.get(p)} for action,paths in
                             [('added',added),('modified',modified),('deleted',deleted)] for p in paths]
                    revision=uid();record={'id':revision,'project_id':pid,'source':str(source),'at':time.time(),'files':files,
                                          'source_stamps':captured['stamps'],'changes':changes}
                    check()
                    # Verify the editor's batch marker again immediately before publishing.
                    if (source/'.compute-sync.lock').exists():raise ValueError('源码正在批量修改，未发布')
                    touched=added+modified+deleted
                    e.progress(job or automatic,phase='publishing',label='发布完整代码版本；运行中的实验保持原版本')
                    atomic_json(pending,{'before':before,'touched':touched,'target_revision':revision})
                    committed=False
                    original_value=dict(value)
                    try:
                        for rel in sorted(deleted+modified,key=lambda p:p.count('/'),reverse=True):
                            target=confined(code,rel)
                            if target.is_file():target.unlink()
                        # Remove empty parents so file <-> directory changes are safe.
                        for current,dirs,names in os.walk(code,topdown=False):
                            p=Path(current)
                            if p!=code and not any(p.iterdir()):p.rmdir()
                        for rel in added+modified:self.copy_object(pool,confined(code,rel),files[rel])
                        record['mirror_stamps']=scan(code,[],check)
                        atomic_json(folder/'mirror.json',{'revision':revision,'stamps':record['mirror_stamps']})
                        atomic_json(folder/('versions/'+revision+'.json'),record)
                        atomic_json(folder/('changes/'+revision+'.json'),{'id':revision,'at':record['at'],'source':str(source),'changes':changes})
                        atomic_json(folder/('history/'+revision+'.json'),{'id':revision,'at':record['at'],'source':str(source),
                            'added':len(added),'modified':len(modified),'deleted':len(deleted),'total':len(changes)})
                        value.update(status='ready' if value['enabled'] else 'paused',revision=revision,manifest='versions/'+revision+'.json',
                                     root_identity=captured['root_identity'],synced_configuration=value['configuration_id'],checked_at=time.time(),
                                     synced_at=time.time(),files=len(files),added=len(added),modified=len(modified),deleted=len(deleted),error='')
                        project=e.store.get('projects',pid);project.update(public_files_revision=revision,public_files_updated_at=time.time())
                        e.store.put_many([('projects',project),('code_sources',value)]);committed=True;pending.unlink()
                    except Exception:
                        if not committed:
                            value=original_value;self.restore(pool,before,touched)
                            atomic_json(folder/'mirror.json',{'revision':value.get('revision'),'stamps':scan(code,[],lambda:None)})
                            for group in ('versions','history','changes'):confined(folder,group+'/'+revision+'.json').unlink(missing_ok=True)
                            pending.unlink(missing_ok=True)
                        raise
                    result={'revision':revision,'changed':True,'added':len(added),'modified':len(modified),'deleted':len(deleted),
                            'changes':changes[:20],'record_id':revision,'source':str(source)}
                    e.store.event('代码同步 · '+project['name']+' · '+revision[:8]+' · 新增 '+str(len(added))+'、修改 '+str(len(modified))+'、删除 '+str(len(deleted)))
                    if automatic:
                        automatic.update(status='success',finished_at=time.time(),result=result,progress={'phase':'complete'});e.store.put('jobs',automatic)
                    return result
                finally:shutil.rmtree(scratch,ignore_errors=True)
            except Exception as exc:
                canceled=isinstance(exc,OperationCanceled)
                value.update(status='error',error=str(exc),checked_at=time.time())
                if canceled and not e.stop_event.is_set():value.update(enabled=False,status='paused')
                e.store.put('code_sources',value)
                if automatic:
                    automatic.update(status='canceled' if canceled else 'failed',error=str(exc),finished_at=time.time());e.store.put('jobs',automatic)
                exc.code_sync_recorded=bool(automatic or job)
                raise
            finally:
                if automatic:e.cancel_signals.pop(automatic['id'],None);e.cancel_checks.pop(automatic['id'],None)

    def snapshot(self,pid,check):
        with self.lock(pid):
            value=self.get(pid)
            if not value.get('path'):return None
            self.synchronize(pid,check=check)
            value=self.get(pid);version=self.read_version(pid,value)
            if not version:raise ValueError('配置的源码目录尚未成功同步，不能使用旧代码启动')
            return version

    def schedule(self):
        if self.engine.storage_moving:return
        for value in self.engine.store.all('code_sources'):
            pid=value['id']
            if not value.get('path') or not value.get('enabled') or time.monotonic()-self.last.get(pid,0)<value.get('interval_seconds',10):continue
            with self.guard:
                future=self.pending.get(pid)
                if future is not None and not future.done():continue
                self.last[pid]=time.monotonic()
                def poll(project_id=pid):
                    try:
                        self.synchronize(project_id)
                        getattr(self,'last_errors',{}).pop(project_id,None)
                    except Exception as exc:
                        if not self.engine.stop_event.is_set():
                            error=str(exc);previous=getattr(self,'last_errors',{}).get(project_id)
                            if error!=previous:
                                self.last_errors=getattr(self,'last_errors',{});self.last_errors[project_id]=error
                                self.engine.store.event('代码自动同步暂缓：'+error,'warning')
                                if not getattr(exc,'code_sync_recorded',False):
                                    now=time.time();self.engine.store.put('jobs',{'id':uid(),'label':'代码同步暂缓','operation':'source_error:'+json.dumps([project_id]),'status':'failed','error':error,'created_at':now,'finished_at':now,'cancelable':False})
                self.pending[pid]=self.worker.submit(poll)
