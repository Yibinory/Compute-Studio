"""Durable chunk reception and background import into managed local storage."""
import os
import json
import shutil
import threading
import time
from pathlib import Path

from .files import confined, name, relative
from .policy import ACTIVE
from .storage import uid
from .operations import OperationCanceled, measured
from .upload_files import UploadFiles
from .file_process import run as file_process

CHUNK_SIZE=8*1024**2
BUSY={'receiving','queued','scanning','copying','extracting','hashing','publishing','syncing','interrupting'}


class Uploads:
    def __init__(self,engine,limit=200*1024**3):
        self.engine,self.limit=engine,limit
        self.lock=threading.RLock()
        self.files=UploadFiles(engine.store)
        for upload in engine.store.all('uploads'):
            if upload['status'] in BUSY:
                upload.update(status='interrupted',error='控制器重启中断了上传；完整接收的文件可继续后台处理，否则请重新选择文件上传。')
                engine.store.put('uploads',upload)

    def scope(self,project_id,task_id=None):
        project=self.engine.local_project(project_id)
        if task_id:
            task=self.engine.store.get('tasks',task_id)
            if task['project_id']!=project_id:
                raise ValueError('任务不属于此项目')
            if task['status'] in ACTIVE:
                raise ValueError('任务执行或回传中，不能导入输入文件')
            return project/'tasks'/task_id/'files'
        if self.engine.code_sources.bound(project_id):
            raise ValueError('项目已绑定源码目录，请在原目录修改；如需上传到工具 code 副本，请先解绑自动同步')
        return project/'code'

    def pending(self,project_id,task_id=None,exclude_job=None):
        clearing=any(j['status'] in ('queued','running') and j['id']!=exclude_job and j.get('operation','').startswith('clear_public_files:')
                     and json.loads(j['operation'].split(':',1)[1])[0]==project_id for j in self.engine.store.active_summaries('jobs'))
        return clearing or any(u['kind']=='project' and u['project_id']==project_id and (not task_id or u.get('task_id') in (None,task_id)) and u['status'] in BUSY|{'failed','interrupted'} for u in self.engine.store.all('uploads'))

    def begin(self,data):
        with self.engine.lock,self.lock:
            if self.engine.storage_moving:
                raise ValueError('本机存储正在迁移，请稍后上传')
            kind=data.get('kind')
            if kind not in ('dataset','project'):
                raise ValueError('未知上传类型')
            mode=data.get('mode','files')
            if mode not in ('files','zip'):
                raise ValueError('未知上传模式')
            raw=data.get('files',[])
            if not isinstance(raw,list) or not raw or len(raw)>200000:
                raise ValueError('请选择文件，最多 200000 个')
            if mode=='zip' and len(raw)!=1:
                raise ValueError('ZIP 模式一次导入一个压缩包')
            files=[];seen=set()
            for item in raw:
                path=relative(item['path']);length=int(item['size'])
                if length<0 or length!=item['size'] or path.casefold() in seen:
                    raise ValueError('文件大小或重名路径无效')
                if kind=='project' and mode=='files' and (path=='deps' or path.startswith('deps/')):
                    raise ValueError('deps 是保留目录')
                seen.add(path.casefold());files.append({'path':path,'size':length,'received':0,'last_modified':item.get('last_modified')})
            total=sum(f['size'] for f in files)
            if total>self.limit:
                raise ValueError('选择的文件超过上传大小限制')
            if shutil.disk_usage(self.engine.root).free<total+64*1024**2:raise ValueError('本机保存目录可用空间不足')
            upload={'id':uid(),'kind':kind,'mode':mode,'file_count':len(files),'files_done':sum(f['size']==0 for f in files),'total_bytes':total,'received_bytes':0,'status':'receiving','created_at':time.time(),'progress':{'phase':'receiving','bytes':0,'total':total}}
            if kind=='project':
                pid=data['project_id'];tid=data.get('task_id') or None
                self.scope(pid,tid)
                if self.pending(pid,tid):
                    raise ValueError('此项目或任务已有导入，请等待完成')
                upload.update(project_id=pid,task_id=tid,label='导入文件 · '+self.engine.store.get('projects',pid)['name'])
            else:
                title=name(data['name'])
                if any(d['name']==title and d.get('import_status')!='canceled' for d in self.engine.store.all('datasets')):
                    raise ValueError('数据集名称已存在；已完成导入的条目可重试服务器同步')
                dataset={'id':uid(),'name':title,'created_at':time.time(),'servers':{},'manifest':{},'import_status':'receiving','upload_id':upload['id']}
                upload.update(dataset_id=dataset['id'],name=title,label='导入数据集 · '+title)
                self.engine.store.put('datasets',dataset)
            base=self.base(upload)
            (base/'payload').mkdir(parents=True)
            self.files.replace(upload['id'],files)
            self.engine.store.put('uploads',upload)
            return self.public(upload)

    def begin_local(self,data):
        source=Path(str(data.get('source_path',''))).expanduser()
        if not source.is_absolute() or source.is_symlink():raise ValueError('请填写本机文件夹或 ZIP 的绝对路径，不支持符号链接')
        source=source.resolve();root=self.engine.root.resolve()
        if source==root or source in root.parents or root in source.parents:raise ValueError('导入来源不能与工具保存目录重叠')
        if not source.is_dir() and not (source.is_file() and source.suffix.lower()=='.zip'):raise ValueError('来源必须是已存在的本机文件夹或 ZIP 压缩包')
        is_zip=source.is_file();length=source.stat().st_size if is_zip else 0
        value=self.begin({**data,'mode':'zip' if is_zip else 'files','files':[{'path':source.name,'size':length}]})
        with self.lock:
            upload=self.engine.store.get('uploads',value['id']);upload.update(local_source=True,source_path=str(source))
            if not is_zip:
                self.files.replace(upload['id'],[]);upload.update(file_count=0,files_done=0)
            self.engine.store.put('uploads',upload)
        return self.finish(value['id'])

    def file_work(self,action,progress,job,upload=None,**options):
        completed=[0]
        def update(kind,event,scratch,pid):
            if kind=='receipts':
                self.files.replace(upload['id'],json.loads((scratch/'receipts.json').read_text(encoding='utf-8')))
                value=self.engine.store.get('uploads',upload['id'])
                value.update(file_count=event['count'],files_done=0,total_bytes=event['total'],received_bytes=0)
                self.engine.store.put('uploads',value)
            elif kind=='progress':
                if upload and event['phase']=='copying':
                    count=event['files_done']
                    if count>completed[0]:
                        with self.engine.store.lock,self.engine.store.db:
                            self.engine.store.db.execute('UPDATE upload_files SET received=size WHERE upload_id=? AND idx>=? AND idx<?',(upload['id'],completed[0],count))
                        completed[0]=count
                    value=self.engine.store.get('uploads',upload['id'])
                    value.update(files_done=count,received_bytes=event['bytes']);self.engine.store.put('uploads',value)
                phase=event.pop('phase');event.pop('type',None)
                done=event.pop('bytes');total=event.pop('total');path=event.pop('path','')
                count=event.pop('files_done',0);files=event.pop('files_total',0)
                progress(phase)(done,total,path,count,files,worker_pid=pid,**event)
        return file_process(self.engine.root,action,update=update,check=lambda:self.engine.check_cancel(job),**options)

    def copy_local(self,upload,stage,progress,job):
        return self.file_work('copy',progress,job,upload=upload,stage=str(stage),
            source=upload['source_path'],mode=upload['mode'],payload=str(self.base(upload)/'payload'),limit=self.limit)

    def base(self,upload):
        return confined(self.engine.root/'incoming',upload['id'])

    @staticmethod
    def public(upload):
        return {**{k:v for k,v in upload.items() if k!='files'},'file_count':upload.get('file_count',len(upload.get('files',[]))),'chunk_size':CHUNK_SIZE}

    def chunk(self,upload_id,index,offset,block):
        return self.batch(upload_id,[(index,offset,block)])

    def batch(self,upload_id,blocks):
        with self.lock:
            upload=self.engine.store.get('uploads',upload_id)
            if upload['status']!='receiving':
                raise ValueError('此上传当前不能接收文件')
            if not blocks or len(blocks)>128 or sum(len(b) for _,_,b in blocks)>CHUNK_SIZE or len({i for i,_,_ in blocks})!=len(blocks):raise ValueError('上传批次无效')
            selected=[]
            for index,offset,block in blocks:
                if not 0<=index<upload['file_count'] or not block or offset<0:raise ValueError('上传分片无效')
                item=self.files.get(upload_id,index);path=self.base(upload)/'payload'/str(index)
                if offset<item['received']:
                    with path.open('rb') as stream:
                        stream.seek(offset)
                        if offset+len(block)<=item['received'] and stream.read(len(block))==block:continue
                    raise ValueError('重试分片与已接收内容不同')
                if offset!=item['received'] or offset+len(block)>item['size']:raise ValueError('分片位置或大小不匹配')
                selected.append((index,offset,block,item,path))
            updates=[]
            for index,offset,block,item,path in selected:
                with path.open('r+b' if path.exists() else 'w+b') as stream:
                    if stream.seek(0,2)<offset:raise ValueError('本机暂存文件已损坏，请重新上传')
                    stream.truncate(offset);stream.seek(offset);stream.write(block)
                item['received']+=len(block);upload['received_bytes']+=len(block)
                if item['received']==item['size']:upload['files_done']+=1
                updates.append((item['received'],upload_id,index))
            if selected:
                upload['progress']=measured(upload.get('progress',{}),{'phase':'receiving','bytes':upload['received_bytes'],'total':upload['total_bytes'],'path':selected[-1][3]['path'],'files_done':upload['files_done'],'files_total':upload['file_count']})
                with self.engine.store.lock,self.engine.store.db:
                    self.engine.store.db.executemany('UPDATE upload_files SET received=? WHERE upload_id=? AND idx=?',updates)
                    self.engine.store.put('uploads',upload)
            return self.public(upload)

    def finish(self,upload_id):
        with self.engine.lock,self.lock:
            upload=self.engine.store.get('uploads',upload_id)
            if upload['status'] in ('queued','extracting','hashing','publishing','syncing','success'):
                return self.public(upload)
            if upload['status'] not in ('receiving','failed','interrupted') or not upload.get('local_source') and upload['files_done']!=upload['file_count']:
                raise ValueError('文件未完整接收，请重新选择文件上传')
            upload.update(status='queued',error='',cancel_requested=False,progress={'phase':'queued','bytes':0,'total':0})
            self.engine.store.put('uploads',upload)
            job=self.engine.enqueue(upload['label'],self.process,upload_id)
            latest=self.engine.store.get('uploads',upload_id);latest['job_id']=job['id'];self.engine.store.put('uploads',latest)
            return self.public(latest)

    def cancel(self,upload_id):
        with self.engine.lock,self.lock:
            upload=self.engine.store.get('uploads',upload_id)
            if upload['status'] not in ('receiving','failed','interrupted'):
                raise ValueError('后台正在处理，请等待完成；不能删除使用中的暂存文件')
            base=self.base(upload)
            if base.exists():
                trash_id=uid();trash=confined(self.engine.root/'discarded',trash_id)
                trash.parent.mkdir(parents=True,exist_ok=True)
                os.replace(base,trash)
                cleanup=self.engine.enqueue('清理已放弃的上传暂存文件',self.discard_payload,trash_id)
                upload['cleanup_job_id']=cleanup['id']
            upload.update(status='canceled',finished_at=time.time());self.engine.store.put('uploads',upload)
            if upload['kind']=='dataset':
                dataset=self.engine.store.get('datasets',upload['dataset_id'])
                if not dataset.get('manifest'):
                    dataset['import_status']='canceled';self.engine.store.put('datasets',dataset)
            return self.public(upload)

    def discard_payload(self,trash_id,job=None):
        target=confined(self.engine.root/'discarded',trash_id)
        self.engine.progress(job,phase='deleting',bytes=0,total=0,path=str(target))
        file_process(self.engine.root,'remove',path=str(target))
        return {'temporary_files_removed':True}

    def interrupt(self,upload_id):
        # Do not take the scheduler lock: a transfer may currently be publishing.
        with self.lock,self.engine.store.lock:
            upload=self.engine.store.get('uploads',upload_id)
            if upload['status'] not in BUSY:
                return self.public(upload)
            if upload['status']=='publishing':
                raise ValueError('本机文件正在提交，请等待此步骤完成')
            job_id=upload.get('job_id')
            if job_id:
                job=self.engine.cancel_job(job_id)
                if job['status']=='running':
                    upload['status']='interrupting'
                else:
                    upload['status']='interrupted'
            else:
                upload['status']='interrupted'
            upload.update(cancel_requested=True,error='用户已中断传输，已接收文件保留，可继续传输')
            self.engine.store.put('uploads',upload)
            return self.public(upload)

    def resume(self,upload_id,files):
        with self.engine.lock,self.lock:
            upload=self.engine.store.get('uploads',upload_id)
            if upload['status'] not in ('interrupted','failed') or upload.get('local_ready'):
                raise ValueError('此上传不能继续浏览器传输')
            stored_files=self.files.all(upload_id)
            if len(files)!=len(stored_files) or any(relative(a['path'])!=b['path'] or a['size']!=b['size'] or b.get('last_modified') is not None and a.get('last_modified')!=b['last_modified'] for a,b in zip(files,stored_files)):
                raise ValueError('请选择原来的相同文件（路径、大小和修改时间需一致）')
            if upload['kind']=='project':self.scope(upload['project_id'],upload.get('task_id'))
            upload.update(status='receiving',cancel_requested=False,error='',progress={'phase':'receiving','bytes':upload['received_bytes'],'total':upload['total_bytes']})
            self.engine.store.put('uploads',upload)
            return {**self.public(upload),'files':stored_files}

    def process(self,upload_id,job=None):
        last=[0,None,None]
        def progress(phase):
            def update(done,total,path='',count=0,files=0,**details):
                self.engine.check_cancel(job)
                now=time.monotonic()
                terminal=(phase,done,total,count,files) if files and count==files else None
                if phase==last[1] and now-last[0]<.25 and (terminal is None or terminal==last[2]):return
                last[:]=[now,phase,terminal]
                with self.engine.store.lock:
                    self.engine.check_cancel(job)
                    value=self.engine.store.get('uploads',upload_id)
                    value.update(status=phase,progress=measured(value.get('progress',{}),{'phase':phase,'bytes':done,'total':total,'path':path,'files_done':count,'files_total':files,**details}))
                    self.engine.store.put('uploads',value)
                    self.engine.progress(job,**value['progress'])
            return update
        try:
            with self.lock:
                upload=self.engine.store.get('uploads',upload_id)
            self.engine.check_cancel(job)
            base=self.base(upload);stage=base/'stage'
            if not upload.get('local_ready'):
                if upload.get('local_source'):
                    inventory=self.copy_local(upload,stage,progress,job)
                    if upload['mode']=='zip':
                        inventory=self.file_work('zip',progress,job,stage=str(stage),archive=str(base/'payload'/'0'),limit=self.limit)
                else:
                    inventory=self.file_work('prepare',progress,job,stage=str(stage),payload=str(base/'payload'),
                        mode=upload['mode'],files=self.files.all(upload_id),limit=self.limit)
                if not inventory:
                    raise ValueError('导入内容为空')
                upload=self.engine.store.get('uploads',upload_id)
                target=self.engine.root/'datasets'/upload['dataset_id']/'files' if upload['kind']=='dataset' else self.scope(upload['project_id'],upload.get('task_id'))
                before=self.file_work('existing',progress,job,path=str(target),paths=list(inventory)) if target.exists() else {}
                local_server={'id':'local','name':'\u672c\u673a\u4fdd\u5b58\u76ee\u5f55'}
                self.engine.transfers.prepare(job,[local_server],inventory)
                self.engine.transfers.plan(job,[local_server],inventory,before,local_server)
                # Reserve publication under the scheduler lock. Pending imports
                # keep scheduling disabled for this scope during filesystem work.
                with self.engine.lock:
                    self.engine.check_cancel(job)
                    if upload['kind']=='project':
                        target=self.scope(upload['project_id'],upload.get('task_id'))
                        if any(p=='deps' or p.startswith('deps/') for p in inventory):raise ValueError('deps is reserved')
                    progress('publishing')(0,sum(v['size'] for v in inventory.values()))
                if upload['kind']=='dataset':
                    dataset=self.engine.store.get('datasets',upload['dataset_id'])
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():raise ValueError('Dataset destination already exists')
                    os.replace(stage,target)
                    dataset.update(manifest=inventory,import_status='ready');self.engine.store.put('datasets',dataset)
                else:
                    backup=self.engine.local_project(upload['project_id'])/'history'/uid()
                    self.file_work('publish',progress,job,stage=str(stage),target=str(target),backup=str(backup),inventory=inventory,before=before)
                self.engine.transfers.local_complete(job)
                upload=self.engine.store.get('uploads',upload_id);upload['local_ready']=True;self.engine.store.put('uploads',upload)
                self.file_work('remove',progress,job,path=str(base),ignore_errors=True)
            if upload['kind']=='dataset' and self.engine.store.all('servers') and not self.engine.settings.get('demo'):
                progress('syncing')(0,0)
                self.engine.upload_dataset(upload['dataset_id'],job=job)
            value=self.engine.store.get('uploads',upload_id)
            value.update(status='success',finished_at=time.time(),progress={'phase':'complete','bytes':value['total_bytes'],'total':value['total_bytes']});self.engine.store.put('uploads',value)
            self.engine.progress(job,**value['progress'])
            return {'upload_id':upload_id,'dataset_id':value.get('dataset_id'),'project_id':value.get('project_id')}
        except OperationCanceled as exc:
            value=self.engine.store.get('uploads',upload_id);value.update(status='interrupted',error=str(exc));self.engine.store.put('uploads',value)
            if value['kind']=='dataset' and not value.get('local_ready'):
                dataset=self.engine.store.get('datasets',value['dataset_id']);dataset.update(import_status='interrupted');self.engine.store.put('datasets',dataset)
            raise
        except Exception as exc:
            value=self.engine.store.get('uploads',upload_id);value.update(status='failed',error=str(exc));self.engine.store.put('uploads',value)
            if value['kind']=='dataset' and not value.get('local_ready'):
                dataset=self.engine.store.get('datasets',value['dataset_id']);dataset.update(import_status='failed',import_error=str(exc));self.engine.store.put('datasets',dataset)
            raise
