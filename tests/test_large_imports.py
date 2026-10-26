import json
import threading
import time
from pathlib import Path

import pytest

from test_api import client,login,archive
from test_transfers import wait_job


def test_many_file_receipts_are_not_rewritten_in_each_chunk(client,monkeypatch):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    files=[{'path':f'folder/{n}.txt','size':1} for n in range(5000)]
    u=c.post('/api/uploads',json={'kind':'dataset','name':'many','files':files},headers=h).get_json()
    raw=e.store.db.execute("SELECT data FROM records WHERE kind='uploads' AND id=?",(u['id'],)).fetchone()[0]
    assert len(raw)<1500 and 'files' not in json.loads(raw)
    monkeypatch.setattr(e.uploads.files,'all',lambda *args:(_ for _ in ()).throw(AssertionError('Do not load full receipt list')))
    parts=[{'index':n,'offset':0,'length':1} for n in range(64)]
    headers={**h,'X-Upload-Batch':json.dumps(parts),'Content-Type':'application/octet-stream'}
    response=c.post('/api/uploads/'+u['id']+'/batch',data=b'x'*64,headers=headers)
    assert response.status_code==200 and response.get_json()['received_bytes']==64
    assert c.post('/api/uploads/'+u['id']+'/batch',data=b'x'*64,headers=headers).get_json()['received_bytes']==64
    assert c.get('/api/state').status_code==200
    detail=c.get('/api/uploads/'+u['id']+'/details?receipt_offset=60&limit=10').get_json()
    assert len(detail['receipts'])==10 and detail['receipt_total']==5000 and detail['receipts'][0]['received']==1
    bad={**h,'X-Upload-Batch':json.dumps([parts[0],parts[0]])}
    assert c.post('/api/uploads/'+u['id']+'/batch',data=b'xx',headers=bad).status_code==400


def test_local_folder_copy_is_background_interruptible_and_can_retry(client,tmp_path,monkeypatch):
    c,app=client;h=login(c,app);e=app.extensions['engine'];source=tmp_path.with_name(tmp_path.name+'-source');source.mkdir()
    (source/'a.txt').write_text('original');(source/'empty.txt').touch()
    started=threading.Event();release=threading.Event();original=e.uploads.copy_local
    def slow(*args):started.set();assert release.wait(4);return original(*args)
    monkeypatch.setattr(e.uploads,'copy_local',slow)
    then=time.monotonic();response=c.post('/api/uploads/local',json={'kind':'dataset','name':'local','source_path':str(source)},headers=h)
    assert response.status_code==202 and time.monotonic()-then<1
    u=response.get_json();assert started.wait(2)
    try:
        assert c.get('/api/state').status_code==200
        assert c.post('/api/uploads/'+u['id']+'/interrupt',json={},headers=h).status_code==200
    finally:release.set()
    assert wait_job(e,u['job_id'])['status']=='canceled'
    assert (source/'a.txt').read_text()=='original'
    monkeypatch.setattr(e.uploads,'copy_local',original)
    resumed=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h).get_json()
    assert wait_job(e,resumed['job_id'])['status']=='success'
    target=e.root/'datasets'/u['dataset_id']/'files'
    assert (target/'a.txt').read_text()=='original' and (target/'empty.txt').exists()
    assert c.get('/api/uploads/'+u['id']+'/details').get_json()['receipt_total']==2


def test_local_zip_keeps_source_and_overlap_is_rejected(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine'];source=tmp_path.with_name(tmp_path.name+'-source.zip');source.write_bytes(archive({'nested/a.txt':'abc'}).getvalue())
    response=c.post('/api/uploads/local',json={'kind':'dataset','name':'localzip','source_path':str(source)},headers=h)
    u=response.get_json();assert response.status_code==202 and wait_job(e,u['job_id'])['status']=='success'
    assert (e.root/'datasets'/u['dataset_id']/'files'/'nested'/'a.txt').read_text()=='abc' and source.is_file()
    assert c.post('/api/uploads/local',json={'kind':'dataset','name':'unsafe','source_path':str(e.root)},headers=h).status_code==400
    assert c.post('/api/uploads/local',json={'kind':'dataset','name':'relative','source_path':'relative'},headers=h).status_code==400


def test_delete_abandoned_dataset_releases_name_and_retains_upload_audit(client):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    u=c.post('/api/uploads',json={'kind':'dataset','name':'BraTS2018_cl','files':[{'path':'a','size':100}]},headers=h).get_json()
    c.post('/api/uploads/'+u['id']+'/cancel',json={},headers=h)
    response=c.post('/api/datasets/'+u['dataset_id']+'/delete',json={},headers=h)
    assert response.status_code==202 and wait_job(e,response.get_json()['id'])['status']=='success'
    assert not e.store.all('datasets') and e.store.get('uploads',u['id'])['dataset_deleted']
    assert e.store.get('deleted_datasets',u['dataset_id'])['name']=='BraTS2018_cl'
    assert not (e.root/'incoming'/u['id']).exists()
    assert c.post('/api/uploads',json={'kind':'dataset','name':'BraTS2018_cl','files':[{'path':'a','size':1}]},headers=h).status_code==201


def test_delete_local_copy_preserves_original_and_remote_files(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine'];source=tmp_path.with_name(tmp_path.name+'-source');source.mkdir();(source/'a').write_text('keep')
    u=c.post('/api/uploads/local',json={'kind':'dataset','name':'remove-local','source_path':str(source)},headers=h).get_json();assert wait_job(e,u['job_id'])['status']=='success'
    s=e.save_server({'name':'remote','host':'fixture','username':'test','work_root':'/work','data_root':'/data'})
    remote=e.remote(s);target=remote.data_base/'remove-local';target.mkdir();(target/'a').write_text('keep')
    r=c.post('/api/datasets/'+u['dataset_id']+'/delete',json={},headers=h).get_json();assert wait_job(e,r['id'])['status']=='success'
    assert (source/'a').read_text()=='keep' and (target/'a').read_text()=='keep'
    assert not (e.root/'datasets'/u['dataset_id']).exists()


def test_dataset_in_use_and_active_import_cannot_be_deleted(client):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    u=c.post('/api/uploads',json={'kind':'dataset','name':'in-use','files':[{'path':'a','size':1}]},headers=h).get_json()
    p=e.create_project({'name':'use'});t=e.create_task(p['id'],{'name':'train','command':'echo test','datasets':['in-use']});t['status']='queued';e.store.put('tasks',t)
    assert c.post('/api/datasets/'+u['dataset_id']+'/delete',json={},headers=h).status_code==400
    t['status']='draft';e.store.put('tasks',t);value=e.store.get('uploads',u['id']);value['status']='copying';e.store.put('uploads',value)
    assert c.post('/api/datasets/'+u['dataset_id']+'/delete',json={},headers=h).status_code==400
