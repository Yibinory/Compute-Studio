import hashlib
import os
import time
import threading
from types import SimpleNamespace

import pytest

from test_api import client,login


def wait(engine,key,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=engine.store.get('jobs',key)
        if value['status'] not in ('queued','running'):return value
        time.sleep(.01)
    raise AssertionError(str(value))


def source_folder(tmp_path,count):
    source=tmp_path.with_name(tmp_path.name+'-source');source.mkdir()
    for index in range(count):(source/f'{index:05}.txt').write_bytes(b'fixture')
    with (source/'large.bin').open('wb') as stream:stream.truncate(64*1024**2)
    return source


def test_real_process_copy_and_publish_leave_api_and_scheduler_responsive(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    source=source_folder(tmp_path,1800);project=e.create_project({'name':'ProcessFiles'})
    start=time.monotonic()
    u=c.post('/api/uploads/local',json={'kind':'project','project_id':project['id'],'source_path':str(source)},headers=h).get_json()
    assert time.monotonic()-start<1
    latencies=[];pids=set();phases=set();end=time.monotonic()+30
    while time.monotonic()<end:
        start=time.monotonic();response=c.get('/api/state');latencies.append(time.monotonic()-start)
        assert response.status_code==200
        upload=e.store.get('uploads',u['id']);progress=upload['progress'];phases.add(progress['phase'])
        if progress.get('worker_pid'):pids.add(progress['worker_pid'])
        assert e.lock.acquire(timeout=.25),'Filesystem work held the scheduler lock'
        e.lock.release()
        if upload['status'] in ('success','failed','interrupted'):break
        time.sleep(.01)
    assert upload['status']=='success',upload
    assert pids and os.getpid() not in pids and 'copying' in phases and 'publishing' in phases
    assert len(latencies)>5 and max(latencies)<1
    target=e.local_project(project['id'])/'code'
    assert (target/'00000.txt').read_bytes()==b'fixture' and (target/'large.bin').stat().st_size==64*1024**2
    assert e.store.get('jobs',u['job_id'])['status']=='success'


def test_interrupt_reaps_actual_child_and_preserves_source(client,tmp_path,monkeypatch):
    import scheduler.file_process as worker
    c,app=client;h=login(c,app);e=app.extensions['engine'];source=source_folder(tmp_path,1400)
    processes=[];original=worker.subprocess.Popen
    def popen(*args,**kwargs):
        process=original(*args,**kwargs);processes.append(process);return process
    monkeypatch.setattr(worker.subprocess,'Popen',popen)
    u=c.post('/api/uploads/local',json={'kind':'dataset','name':'ProcessCancel','source_path':str(source)},headers=h).get_json()
    end=time.monotonic()+5
    while time.monotonic()<end:
        if e.store.get('uploads',u['id']).get('progress',{}).get('worker_pid'):break
        time.sleep(.005)
    assert processes and processes[0].poll() is None
    start=time.monotonic();response=c.post('/api/uploads/'+u['id']+'/interrupt',json={},headers=h)
    assert response.status_code==200 and time.monotonic()-start<1
    assert wait(e,u['job_id'])['status']=='canceled' and processes[0].poll() is not None
    assert e.store.get('uploads',u['id'])['status']=='interrupted'
    assert (source/'00000.txt').read_bytes()==b'fixture' and (source/'large.bin').stat().st_size==64*1024**2
    assert not (e.root/'datasets'/u['dataset_id']/'files').exists()
    assert not list((e.root/'workers').iterdir())


def test_state_does_not_load_large_inventories_and_summaries_survive_restart(client,monkeypatch):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    inventory={f'f/{i}':{'size':7,'sha256':'a'*64} for i in range(12000)}
    e.store.put('datasets',{'id':'big','name':'Big','manifest':inventory,'servers':{}})
    e.store.put('jobs',{'id':'old','label':'Old','status':'success','result':{'manifest':inventory,'name':'Big'},'created_at':1})
    original=e.store.all
    def all_records(kind):
        assert kind not in ('datasets','jobs','replicas'),'State must use compact summaries'
        return original(kind)
    monkeypatch.setattr(e.store,'all',all_records)
    response=c.get('/api/state');assert response.status_code==200
    data=response.get_json();assert data['datasets'][0]['file_count']==12000 and data['datasets'][0]['total_bytes']==84000
    assert 'manifest' not in data['jobs'][0]['result'] and len(response.data)<20000
    from scheduler.storage import Store
    reopened=Store(e.root)
    try:assert reopened.summaries('datasets')[0]['file_count']==12000
    finally:reopened.close()


def test_slow_remote_hash_does_not_block_a_resource_probe(tmp_path):
    from scheduler.remote import Remote
    release=threading.Event();started=threading.Event();result=[]
    class Channel:
        sent=False
        def shutdown_write(self):pass
        def recv_ready(self):return release.is_set() and not self.sent
        def recv_stderr_ready(self):return False
        def recv(self,size):self.sent=True;return b'{"ok":true,"result":{"files":{}}}'
        def exit_status_ready(self):return self.sent
        def recv_exit_status(self):return 0
        def close(self):pass
    def command(value,**kwargs):
        channel=Channel();stdin=SimpleNamespace(channel=channel,write=lambda x:None,flush=lambda:None)
        if value.endswith('dataset_manifest'):
            started.set();stream=SimpleNamespace(channel=channel);return stdin,stream,stream
        return stdin,SimpleNamespace(channel=channel,read=lambda:b'{"ok":true,"result":{"gpus":[]}}'),SimpleNamespace(read=lambda:b'')
    remote=Remote({'id':'fixture'},tmp_path);remote.agent_root='/fixture'
    remote.client=SimpleNamespace(get_transport=lambda:SimpleNamespace(is_active=lambda:True),exec_command=command)
    thread=threading.Thread(target=lambda:result.append(remote.cancellable_call('dataset_manifest',check=lambda:None)));thread.start()
    assert started.wait(2)
    try:
        start=time.monotonic();assert remote.call('probe')=={'gpus':[]}
        assert time.monotonic()-start<.25 and thread.is_alive()
    finally:release.set();thread.join(timeout=2)
    assert result==[{'files':{}}]


def test_project_publication_failure_rolls_back_in_child(tmp_path):
    from scheduler.file_process import run
    target=tmp_path/'target';stage=tmp_path/'stage';backup=tmp_path/'backup'
    target.mkdir();stage.mkdir();(target/'a').write_bytes(b'original');(target/'b').mkdir()
    (stage/'a').write_bytes(b'new');(stage/'b').write_bytes(b'new')
    inventory={key:{'sha256':hashlib.sha256(b'new').hexdigest(),'size':3} for key in ('a','b')}
    before={'a':{'sha256':hashlib.sha256(b'original').hexdigest(),'size':8}}
    with pytest.raises(ValueError):run(tmp_path,'publish',stage=str(stage),target=str(target),backup=str(backup),inventory=inventory,before=before)
    assert (target/'a').read_bytes()==b'original' and (target/'b').is_dir()
