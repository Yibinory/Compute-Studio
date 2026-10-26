import json
import threading
import time
from pathlib import Path

import pytest

from app import create_app
from conftest import FakeRemote
from scheduler.files import manifest
from scheduler.operations import measured
from scheduler.operations import OperationCanceled
from scheduler.remote import Remote
from test_api import archive, client, login


def wait_job(engine,key):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        value=engine.store.get('jobs',key)
        if value['status'] not in ('queued','running'):return value
        time.sleep(.01)
    raise AssertionError('后台操作未结束：'+str(value))


def begin(c,h,**kwargs):
    result=c.post('/api/uploads',json=kwargs,headers=h)
    assert result.status_code==201,result.get_json()
    return result.get_json()


def chunk(c,h,u,block,offset=0,index=0):
    return c.put('/api/uploads/'+u['id']+'/files/'+str(index)+'?offset='+str(offset),data=block,headers=h)


def test_chunk_interrupt_resume_and_publish(client):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    p=e.create_project({'name':'ChunkResume'})
    files=[{'path':'nested/a.txt','size':6,'last_modified':123},{'path':'empty.txt','size':0}]
    u=begin(c,h,kind='project',project_id=p['id'],files=files)
    assert chunk(c,h,u,b'abc').get_json()['received_bytes']==3
    assert chunk(c,h,u,b'abc').get_json()['received_bytes']==3
    assert chunk(c,h,u,b'bad').status_code==400
    assert chunk(c,h,u,b'x',offset=4).status_code==400
    assert c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h).status_code==400
    assert c.post('/api/uploads/'+u['id']+'/interrupt',json={},headers=h).get_json()['status']=='interrupted'
    assert (e.root/'incoming'/u['id']/'payload'/'0').read_bytes()==b'abc'
    assert chunk(c,h,u,b'def',offset=3).status_code==400
    bad=[{**files[0],'last_modified':124},files[1]]
    assert c.post('/api/uploads/'+u['id']+'/resume',json={'files':bad},headers=h).status_code==400
    resumed=c.post('/api/uploads/'+u['id']+'/resume',json={'files':files},headers=h).get_json()
    assert resumed['files'][0]['received']==3
    assert chunk(c,h,u,b'def',offset=3).status_code==200
    r=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h)
    assert r.status_code==202
    assert wait_job(e,r.get_json()['job_id'])['status']=='success'
    code=e.local_project(p['id'])/'code'
    assert (code/'nested'/'a.txt').read_bytes()==b'abcdef'
    assert (code/'empty.txt').exists()
    assert not (e.root/'incoming'/u['id']).exists()
    assert e.store.get('uploads',u['id'])['progress']['phase']=='complete'


def test_background_hash_cancel_blocks_experiment_then_retries(client,monkeypatch):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'AsyncHash'})
    t=e.create_task(p['id'],{'name':'train','command':'echo fixture'});t['status']='queued';e.store.put('tasks',t)
    started=threading.Event();release=threading.Event();original=e.uploads.file_work
    def slow(*args,**kwargs):
        started.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(e.uploads,'file_work',slow)
    u=begin(c,h,kind='project',project_id=p['id'],files=[{'path':'train.py','size':4}])
    assert chunk(c,h,u,b'code').status_code==200
    then=time.monotonic();r=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h)
    assert time.monotonic()-then<1 and r.status_code==202
    assert started.wait(2)
    try:
        assert c.get('/api/state').status_code==200
        e.tick();assert e.store.get('tasks',t['id'])['status']=='blocked'
        then=time.monotonic();r=c.post('/api/uploads/'+u['id']+'/interrupt',json={},headers=h)
        assert time.monotonic()-then<1 and r.get_json()['status']=='interrupting'
    finally:release.set()
    assert wait_job(e,e.store.get('uploads',u['id'])['job_id'])['status']=='canceled'
    assert e.store.get('uploads',u['id'])['status']=='interrupted'
    assert not (e.local_project(p['id'])/'code'/'train.py').exists()
    assert (e.root/'incoming'/u['id']/'payload'/'0').read_bytes()==b'code'
    r=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h).get_json()
    assert wait_job(e,r['job_id'])['status']=='success'
    assert not e.uploads.pending(p['id'])


def test_cancel_queued_import_never_executes(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];release=threading.Event();started=threading.Barrier(3)
    def occupy(job=None):started.wait();release.wait(5)
    e.enqueue('occupy1',occupy,);e.enqueue('occupy2',lambda job=None:occupy(job));started.wait(timeout=2)
    try:
        u=begin(c,h,kind='dataset',name='QueuedCancel',files=[{'path':'a','size':1}]);chunk(c,h,u,b'a')
        u=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h).get_json()
        assert c.post('/api/jobs/'+u['job_id']+'/interrupt',json={},headers=h).status_code==200
        assert e.store.get('uploads',u['id'])['status']=='interrupted'
    finally:release.set()
    assert wait_job(e,u['job_id'])['status']=='canceled'
    assert not (e.root/'datasets'/u['dataset_id']/'files').exists()


def test_remote_transfer_cancel_preserves_partial_and_retry(client):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    s=e.save_server({'name':'cancel-test','host':'fixture','username':'test','work_root':'/work/test','data_root':'/data/test','enabled':False})
    dataset={'id':'cancel-dataset','name':'CancelData','servers':{},'import_status':'ready'}
    local=e.root/'datasets'/dataset['id']/'files';local.mkdir(parents=True);(local/'weights.bin').write_bytes(b'x'*1024**2)
    dataset['manifest']=manifest(local);e.store.put('datasets',dataset)
    remote=e.remote(s);started=threading.Event();release=threading.Event();original=remote.sftp.put
    def slow(source,target,callback=None):
        Path(target).parent.mkdir(parents=True,exist_ok=True)
        with Path(target).open('wb') as out:out.write(b'x'*32)
        started.set();assert release.wait(5)
        callback(32,Path(source).stat().st_size)
        original(source,target,callback)
    remote.sftp.put=slow
    job=e.enqueue('Cancel server transfer',e.upload_dataset,dataset['id'])
    assert started.wait(3)
    try:
        assert c.post('/api/jobs/'+job['id']+'/interrupt',json={},headers=h).status_code==200
    finally:release.set()
    assert wait_job(e,job['id'])['status']=='canceled'
    assert e.store.get('datasets',dataset['id'])['servers'][s['id']]['status']=='interrupted'
    assert not (remote.data_base/dataset['name']/'weights.bin').exists()
    assert list((remote.data_base/dataset['name']).glob('*.upload-*'))
    remote.sftp.put=original
    retry=e.enqueue('Retry',e.upload_dataset,dataset['id'])
    assert wait_job(e,retry['id'])['status']=='success'
    assert manifest(remote.data_base/dataset['name'])==dataset['manifest']


@pytest.mark.parametrize('path',['../evil','C:/evil','a\\b','CON','deps/file.py'])
def test_chunk_path_safety(client,path):
    c,app=client;h=login(c,app);p=app.extensions['engine'].create_project({'name':'Paths'})
    assert c.post('/api/uploads',json={'kind':'project','project_id':p['id'],'files':[{'path':path,'size':1}]},headers=h).status_code==400


def test_bad_zip_never_replaces_existing_code(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'SafeZip'})
    target=e.local_project(p['id'])/'code'/'model.py';target.write_text('original')
    raw=archive({'model.py':'bad','../escape':'bad'}).getvalue()
    u=begin(c,h,kind='project',project_id=p['id'],mode='zip',files=[{'path':'bad.zip','size':len(raw)}]);chunk(c,h,u,raw)
    r=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=h).get_json()
    assert wait_job(e,r['job_id'])['status']=='failed'
    assert target.read_text()=='original'
    assert e.uploads.pending(p['id'])
    assert c.post('/api/uploads/'+u['id']+'/cancel',json={},headers=h).status_code==200
    assert not e.uploads.pending(p['id'])


def test_storage_migration_preserves_data_auth_and_restarts(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];storage=app.extensions['local_storage'];old=e.root;destination=old.parent/(old.name+'-moved')
    p=e.create_project({'name':'MoveMe'});code=e.local_project(p['id'])/'code'/'a.py';code.write_text('print(42)')
    token=(old/'access-token.txt').read_text();cookie=app.config['SESSION_COOKIE_NAME']
    job=e.enqueue('Migrate',storage.migrate,str(destination))
    value=wait_job(e,job['id']);assert value['status']=='success',value
    assert e.root==destination and code.read_text()=='print(42)'
    assert (destination/'projects'/p['id']/'code'/'a.py').read_text()=='print(42)'
    assert c.get('/api/state').status_code==200
    assert (destination/'access-token.txt').read_text()==token
    assert json.loads(storage.config.read_text())['root']==str(destination)
    e.store.event('new-root event')
    assert 'new-root event' in (destination/'logs'/'events.log').read_text(encoding='utf-8')
    e.close()
    replacement=create_app(config_path=storage.config,remote_factory=FakeRemote)
    try:
        assert replacement.extensions['engine'].root==destination
        assert replacement.config['SESSION_COOKIE_NAME']==cookie
        assert replacement.test_client().post('/login',json={'token':token}).status_code==200
        assert replacement.extensions['engine'].store.get('projects',p['id'])['name']=='MoveMe'
    finally:replacement.extensions['engine'].close()


def test_storage_refuses_occupied_target_and_active_upload(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];old=e.root
    occupied=old.parent/(old.name+'-occupied');occupied.mkdir();(occupied/'keep').write_text('keep')
    assert c.post('/api/local-storage/validate',json={'path':str(occupied)},headers=h).status_code==400
    assert c.post('/api/local-storage/validate',json={'path':str(old/'child')},headers=h).status_code==400
    u=begin(c,h,kind='dataset',name='BusyMigration',files=[{'path':'a','size':1}])
    r=c.post('/api/local-storage/migrate',json={'path':str(old.parent/(old.name+'-target'))},headers=h)
    assert wait_job(e,r.get_json()['id'])['status']=='failed'
    assert e.root==old and (occupied/'keep').read_text()=='keep'
    assert c.post('/api/uploads/'+u['id']+'/interrupt',json={},headers=h).status_code==200


def test_storage_verification_failure_keeps_original(client,monkeypatch):
    c,app=client;login(c,app);e=app.extensions['engine'];storage=app.extensions['local_storage'];old=e.root
    p=e.create_project({'name':'Rollback'});(e.local_project(p['id'])/'code'/'keep').write_text('source')
    monkeypatch.setattr('scheduler.local_storage.digest',lambda *args:'bad-hash')
    job=e.enqueue('Bad migration',storage.migrate,str(old.parent/(old.name+'-bad')))
    assert wait_job(e,job['id'])['status']=='failed'
    assert e.root==old and not storage.config.exists() and not e.storage_moving
    assert e.store.get('projects',p['id'])['name']=='Rollback'
    assert (e.local_project(p['id'])/'code'/'keep').read_text()=='source'


def test_phase_estimates_reset_between_servers():
    p=measured({'phase':'syncing','server':'a','bytes':100,'total':1000,'updated_at':time.time()-1},{'phase':'syncing','server':'a','bytes':200,'total':1000})
    assert p['bytes_per_second']>0 and p['eta_seconds']>0
    changed=measured(p,{'phase':'syncing','server':'b','bytes':0,'total':1000})
    assert changed['bytes_per_second']==0 and changed['eta_seconds'] is None


def test_stalled_sftp_uses_own_channel_and_interrupts_promptly(tmp_path):
    from types import SimpleNamespace
    local=tmp_path/'local';local.mkdir();(local/'a').write_bytes(b'a')
    canceled=threading.Event();started=threading.Event();closed=threading.Event()
    class Channel:
        def get_channel(self):return self
        def settimeout(self,value):pass
        def close(self):closed.set()
        def put(self,*args,**kwargs):
            started.set();assert closed.wait(2)
            raise EOFError('canceled channel')
    remote=Remote({'id':'test'},tmp_path);remote.client=SimpleNamespace(open_sftp=lambda:Channel())
    remote.call=lambda action,*args,**kwargs: {'files':{}} if action.endswith('manifest') else {'path':'unused'}
    def check():
        if canceled.is_set():raise OperationCanceled('stop')
    failures=[]
    def work():
        try:remote.upload(local,{'name':'test'},'',dataset=True,cancel_check=check)
        except Exception as exc:failures.append(exc)
    worker=threading.Thread(target=work);worker.start();assert started.wait(1)
    canceled.set();worker.join(timeout=1)
    assert not worker.is_alive() and closed.is_set()
    assert isinstance(failures[0],OperationCanceled)
    assert remote.client is not None


def test_remote_hash_request_cancel_closes_only_its_channel(tmp_path):
    from types import SimpleNamespace
    class Channel:
        closed=False
        def shutdown_write(self):pass
        def recv_ready(self):return False
        def recv_stderr_ready(self):return False
        def exit_status_ready(self):return False
        def close(self):self.closed=True
    channel=Channel()
    stdin=SimpleNamespace(channel=channel,write=lambda x:None,flush=lambda:None)
    stream=SimpleNamespace(channel=channel)
    remote=Remote({'id':'test'},tmp_path);remote.agent_root='/test'
    remote.client=SimpleNamespace(get_transport=lambda:SimpleNamespace(is_active=lambda:True),exec_command=lambda *args,**kwargs:(stdin,stream,stream))
    count=[0]
    def check():
        count[0]+=1
        if count[0]>3:raise OperationCanceled('stop')
    with pytest.raises(OperationCanceled):remote.cancellable_call('dataset_manifest',{'name':'test'},check=check)
    assert channel.closed and remote.client is not None


def test_migration_interrupt_keeps_source_and_auth(client,monkeypatch):
    c,app=client;h=login(c,app);e=app.extensions['engine'];old=e.root;storage=app.extensions['local_storage']
    started=threading.Event();release=threading.Event()
    original=storage.target
    def target(raw):
        result=original(raw);started.set();assert release.wait(3)
        return result
    monkeypatch.setattr(storage,'target',target)
    job=e.enqueue('Interrupt migration',storage.migrate,str(old.parent/(old.name+'-canceled')))
    assert started.wait(2)
    try:assert c.post('/api/jobs/'+job['id']+'/interrupt',json={},headers=h).status_code==200
    finally:release.set()
    assert wait_job(e,job['id'])['status']=='canceled'
    assert e.root==old and not e.storage_moving and not storage.config.exists()
    assert c.get('/api/state').status_code==200


def test_restart_marks_incomplete_upload_resumable(tmp_path):
    old=tmp_path/'restart';app=create_app(old,remote_factory=FakeRemote);e=app.extensions['engine'];uploads=app.extensions['uploads']
    files=[{'path':'a','size':6}]
    u=uploads.begin({'kind':'dataset','name':'RestartData','files':files});uploads.chunk(u['id'],0,0,b'abc');e.close()
    replacement=create_app(old,remote_factory=FakeRemote)
    try:
        re=replacement.extensions['engine'];ru=replacement.extensions['uploads']
        assert re.store.get('uploads',u['id'])['status']=='interrupted'
        assert ru.resume(u['id'],files)['files'][0]['received']==3
        ru.chunk(u['id'],0,3,b'def');finished=ru.finish(u['id'])
        assert wait_job(re,finished['job_id'])['status']=='success'
        assert (old/'datasets'/u['dataset_id']/'files'/'a').read_bytes()==b'abcdef'
    finally:replacement.extensions['engine'].close()
