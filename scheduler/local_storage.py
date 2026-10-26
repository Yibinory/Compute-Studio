"""Configurable storage with verified, non-destructive live migration."""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .files import confined, digest
from .policy import ACTIVE
from .storage import uid
from .uploads import BUSY


def saved_root(config,default):
    path=Path(config)
    return Path(json.loads(path.read_text(encoding='utf-8'))['root']) if path.exists() else Path(default)


def root_lock(root):
    path=Path(root);path.mkdir(parents=True,exist_ok=True)
    lock=(path/'controller.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close();raise ValueError('此保存目录已经有控制器运行')
    return lock


class LogWriter:
    def __init__(self,logger,level):self.logger,self.level=logger,level
    def write(self,text):
        if text.strip():self.logger.log(self.level,text.rstrip())
        return len(text)
    def flush(self):
        for handler in self.logger.handlers:handler.flush()
    def isatty(self):return False


class LocalStorage:
    def __init__(self,engine,config,install,runtime=False,initial_lock=None):
        self.engine,self.config,self.install,self.runtime=engine,Path(config),Path(install).resolve(),runtime
        self.locks=[initial_lock] if initial_lock else []
        self.logger=logging.getLogger('compute.controller.'+uid());self.logger.setLevel(logging.INFO);self.logger.propagate=False
        self.old_streams=None
        self.configure_logs()
        instance=engine.root/'controller-instance.json'
        if not instance.exists():
            instance.write_text(json.dumps({'cookie_id':hashlib.sha256(os.path.normcase(str(engine.root)).encode()).hexdigest()[:12]}),encoding='utf-8')
        self.cookie_id=json.loads(instance.read_text(encoding='utf-8'))['cookie_id']
        if runtime:
            self.old_streams=(sys.stdout,sys.stderr)
            sys.stdout=LogWriter(self.logger,logging.INFO);sys.stderr=LogWriter(self.logger,logging.ERROR)
            tempfile.tempdir=str(self.engine.root/'tmp')
        engine.close_handlers.append(self.close)

    def configure_logs(self):
        from logging.handlers import RotatingFileHandler
        for handler in self.logger.handlers[:]:handler.close();self.logger.removeHandler(handler)
        (self.engine.root/'logs').mkdir(exist_ok=True);(self.engine.root/'tmp').mkdir(exist_ok=True)
        handler=RotatingFileHandler(self.engine.root/'logs'/'controller.log',maxBytes=10*1024**2,backupCount=5,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'));self.logger.addHandler(handler)

    def close(self):
        if self.old_streams:
            sys.stdout,sys.stderr=self.old_streams;self.old_streams=None
        for handler in self.logger.handlers[:]:handler.close();self.logger.removeHandler(handler)
        for lock in self.locks:lock.close()
        self.locks=[]

    def info(self):
        disk=shutil.disk_usage(self.engine.root)
        try:previous=self.engine.store.get('local_storage','global').get('previous_roots',[])
        except ValueError:previous=[]
        return {'root':str(self.engine.root),'logs_root':str(self.engine.root/'logs'),'temp_root':str(self.engine.root/'tmp'),'free_bytes':disk.free,'total_bytes':disk.total,'migrating':self.engine.storage_moving,'previous_roots':previous}

    def target(self,raw):
        value=Path(str(raw).strip()).expanduser()
        if not value.is_absolute() or str(value).startswith('\\\\'):
            raise ValueError('请填写本机磁盘上的绝对目录')
        path=value.resolve();old=self.engine.root
        if path==Path(path.anchor) or path==old or path in old.parents or old in path.parents or path==self.install or path in self.install.parents or path==Path.home().resolve():
            raise ValueError('新目录不能是当前目录、它的上级／子目录、程序目录、用户主目录或磁盘根目录')
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError('请使用空目录，防止覆盖已有文件')
        parent=path
        while not parent.exists():
            if parent==parent.parent:
                raise ValueError('目标磁盘不存在或不可访问')
            parent=parent.parent
        disk=shutil.disk_usage(parent)
        return path,disk

    def validate(self,raw):
        path,disk=self.target(raw)
        return {'root':str(path),'free_bytes':disk.free,'total_bytes':disk.total,'source':str(self.engine.root),'keeps_source':True}

    def migrate(self,raw,job=None):
        engine=self.engine;old=engine.root;destination=None;new_lock=None;switched=False
        with engine.lock:
            if any(not future.done() for future in engine.code_sources.pending.values()):raise ValueError('源码目录正在检查，请稍后迁移')
            if engine.storage_moving or any(t['status'] in ACTIVE for t in engine.store.all('tasks')):
                raise ValueError('存在执行、回传或状态未知的实验，请完成后迁移')
            if any(j['status'] in ('running','queued') and j['id']!=(job or {}).get('id') for j in engine.store.all('jobs')) or any(u['status'] in BUSY for u in engine.store.all('uploads')):
                raise ValueError('有上传或后台操作进行中，请完成后迁移')
            destination,disk=self.target(raw)
            engine.storage_moving=True
        try:
            engine.progress(job,phase='scanning',bytes=0,total=0)
            entries=[]
            for base,dirs,files in os.walk(old,followlinks=False):
                engine.check_cancel(job)
                dirs[:]=[d for d in dirs if not (Path(base)==old and d=='tmp')]
                for item in dirs+files:
                    confined(old,(Path(base)/item).relative_to(old).as_posix())
                for item in files:
                    rel=(Path(base)/item).relative_to(old).as_posix()
                    if rel in ('controller.lock','compute.sqlite3','compute.sqlite3-wal','compute.sqlite3-shm','migration-source.json'):
                        continue
                    path=confined(old,rel);entries.append((rel,path,path.stat().st_size,path.stat().st_mtime_ns))
            with engine.store.lock:
                db_size=engine.store.db.execute('PRAGMA page_count').fetchone()[0]*engine.store.db.execute('PRAGMA page_size').fetchone()[0]
            total=sum(n for _,_,n,_ in entries)+db_size
            if disk.free<total+64*1024**2:
                raise ValueError('目标磁盘空间不足：需要存储副本并预留至少 64 MB')
            destination.mkdir(parents=True,exist_ok=True)
            new_lock=root_lock(destination)
            if any(p.name!='controller.lock' for p in destination.iterdir()):
                raise ValueError('目标目录在验证后出现文件，已停止迁移以避免覆盖')
            (destination/'migration-source.json').write_text(json.dumps({'source':str(old),'started_at':time.time(),'instance':self.cookie_id}),encoding='utf-8')
            done=0;last=0;checks=[]
            for index,(rel,source,length,mtime) in enumerate(entries):
                target=confined(destination,rel);target.parent.mkdir(parents=True,exist_ok=True)
                value=hashlib.sha256();remaining=length
                with source.open('rb') as input_stream,target.open('xb') as output:
                    while remaining:
                        engine.check_cancel(job)
                        block=input_stream.read(min(1024**2,remaining))
                        if not block:raise ValueError('迁移期间文件发生变化：'+rel)
                        output.write(block);value.update(block);remaining-=len(block);done+=len(block)
                        if time.monotonic()-last>.25:
                            last=time.monotonic();engine.progress(job,phase='copying',bytes=done,total=total,path=rel,files_done=index,files_total=len(entries))
                if not rel.startswith('logs/') and (source.stat().st_size!=length or source.stat().st_mtime_ns!=mtime):
                    raise ValueError('迁移期间文件发生变化：'+rel)
                checks.append((rel,value.hexdigest(),length))
            done=0;verify_total=sum(n for _,_,n in checks)
            for index,(rel,expected,length) in enumerate(checks):
                def progress(count):
                    nonlocal last
                    engine.check_cancel(job)
                    if time.monotonic()-last>.25:
                        last=time.monotonic();engine.progress(job,phase='verifying',bytes=done+count,total=verify_total,path=rel,files_done=index,files_total=len(checks))
                if digest(confined(destination,rel),progress)!=expected:
                    raise ValueError('迁移副本 hash 不一致：'+rel)
                done+=length
            engine.progress(job,phase='switching',bytes=verify_total,total=verify_total)
            with engine.lock,engine.store.lock:
                if (destination/'compute.sqlite3').exists():
                    raise ValueError('目标目录出现其他数据库，已拒绝覆盖')
                with sqlite3.connect(str(destination/'compute.sqlite3')) as target_db:
                    engine.store.db.backup(target_db)
                for remote in engine.remotes.values():remote.close()
                engine.remotes={}
                engine.store.relocate(destination);engine.root=destination;switched=True
                self.configure_logs()
                if self.runtime:tempfile.tempdir=str(destination/'tmp')
                try:previous=engine.store.get('local_storage','global').get('previous_roots',[])
                except ValueError:previous=[]
                engine.store.put('local_storage',{'id':'global','previous_roots':previous+[str(old)],'changed_at':time.time()})
                engine.store.event('本机保存目录已迁移：'+str(destination)+'；原目录保留作备份')
                engine.progress(job,phase='complete',bytes=total,total=total)
                self.config.parent.mkdir(parents=True,exist_ok=True)
                temporary=self.config.with_name(self.config.name+'.'+uid()+'.tmp')
                temporary.write_text(json.dumps({'root':str(destination)},ensure_ascii=False),encoding='utf-8')
                os.replace(temporary,self.config)
                self.locks.append(new_lock);new_lock=None
            return {'root':str(destination),'previous_root':str(old),'verified_files':len(checks),'kept_previous':True}
        except Exception:
            if switched:
                with engine.lock:
                    engine.store.relocate(old);engine.root=old;self.configure_logs()
                    if self.runtime:tempfile.tempdir=str(old/'tmp')
            # A failed destination is retained for review; no unrelated files are deleted.
            raise
        finally:
            if new_lock:new_lock.close()
            engine.storage_moving=False
