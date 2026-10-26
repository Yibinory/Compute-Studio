"""Delete a managed local dataset; never delete its source or remote copies."""
import json
import time

from .files import confined
from .policy import ACTIVE
from .file_process import run as file_process


def guard(engine,key,job=None):
    dataset=engine.store.get('datasets',key)
    if any(t['status'] in ACTIVE|{'queued','waiting','blocked'} and dataset['name'] in t.get('datasets',[]) for t in engine.store.all('tasks')):
        raise ValueError('有执行中或等待执行的实验使用此数据集，不能删除')
    for value in engine.store.summaries('jobs'):
        if value['id']==(job or {}).get('id') or value['status'] not in ('queued','running'):continue
        op=value.get('operation','')
        if op in ('upload_dataset:'+json.dumps([key]),'verify_dataset:'+json.dumps([key]),'delete_dataset:'+json.dumps([key])):
            raise ValueError('此数据集正在同步、核验或删除，请先中断或等待完成')
    for upload in engine.store.all('uploads'):
        if upload.get('dataset_id')==key and upload['status'] in {'queued','scanning','copying','extracting','hashing','publishing','syncing','interrupting'}:
            raise ValueError('此数据集正在后台处理，请先中断或等待完成')
    return dataset


def delete(engine,key,job=None):
    with engine.lock,engine.uploads.lock:
        dataset=guard(engine,key,job)
        linked=[u for u in engine.store.all('uploads') if u.get('dataset_id')==key]
        targets=[confined(engine.root/'datasets',key)]+[engine.uploads.base(u) for u in linked]
        # Resolve and check every absolute target before any recursive removal.
        for target in targets:
            if target.is_symlink() or engine.root.resolve() not in target.resolve().parents:raise ValueError('清理目录越界或为符号链接，拒绝删除')
        for upload in linked:
            upload.update(status='canceled',dataset_deleted=True,finished_at=time.time());engine.store.put('uploads',upload)
        dataset['import_status']='deleting';engine.store.put('datasets',dataset)
    for index,target in enumerate(targets):
        engine.progress(job,phase='deleting',path=str(target),bytes=0,total=0,files_done=index,files_total=len(targets))
        if target.exists():file_process(engine.root,'remove',path=str(target))
    with engine.lock,engine.store.lock,engine.store.db:
        dataset.update(deleted_at=time.time(),import_status='deleted')
        engine.store.put('deleted_datasets',dataset);engine.store.delete('datasets',key);engine.store.delete('dataset_notes',key)
    engine.progress(job,phase='complete',bytes=0,total=0,files_done=len(targets),files_total=len(targets))
    return {'dataset_id':key,'name':dataset['name'],'remote_files_preserved':True}
