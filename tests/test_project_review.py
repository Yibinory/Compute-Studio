import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import LocalSFTP
from scheduler.engine import Engine
from scheduler.files import copy_tree, manifest
from scheduler.operations import OperationCanceled
from test_api import client, login
from test_experiment_controls import running


def test_old_attempt_cannot_overwrite_new_reservation(engine,setup_project):
    task,project,servers=running(engine,setup_project);old=copy.deepcopy(task)
    replacement={**task,'attempt_id':'new-attempt','status':'staging','reason':'new run'};engine.store.put('tasks',replacement)
    with pytest.raises(OperationCanceled):engine.put_runtime_task(old)
    assert engine.store.get('tasks',task['id'])==replacement


def test_stale_paused_status_does_not_undo_confirmed_resume(engine,setup_project):
    task,project,servers=running(engine,setup_project)
    paused=engine.task_action(task['id'],'pause');engine.task_action(task['id'],'resume')
    engine.put_runtime_task(paused)
    assert engine.store.get('tasks',task['id'])['status']=='running'


def test_control_requests_are_ordered_per_experiment(engine,setup_project,monkeypatch):
    task,project,servers=running(engine,setup_project);remote=engine.remote(servers[0]);original=remote.call
    entered=threading.Event();release=threading.Event();order=[]
    def slow(action,*args,**kwargs):
        if action=='pause':entered.set();assert release.wait(3)
        if action in ('pause','resume'):order.append(action)
        return original(action,*args,**kwargs)
    monkeypatch.setattr(remote,'call',slow)
    first=engine.prepare_control(task['id'],'pause')
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(engine.perform_control,first,'pause');assert entered.wait(2)
        # Accept a new desired state while the first SSH request is in flight.
        pending=engine.store.get('tasks',task['id']);pending['status']='suspended';engine.store.put('tasks',pending)
        second=engine.prepare_control(task['id'],'resume');b=pool.submit(engine.perform_control,second,'resume')
        release.set();a.result(3);b.result(3)
    assert order==['pause','resume'] and engine.store.get('tasks',task['id'])['status']=='running'


def test_canceled_copy_keeps_existing_target_and_removes_partial_file(tmp_path):
    source=tmp_path/'source';target=tmp_path/'target';source.mkdir();target.mkdir()
    (source/'weights.bin').write_bytes(b'x'*(4*1024*1024));(target/'weights.bin').write_bytes(b'original')
    def stop():
        if any(p.stat().st_size>=1024*1024 for p in target.glob('.copy-*')):raise OperationCanceled('stop')
    with pytest.raises(OperationCanceled):copy_tree(source,target,check=stop)
    assert (target/'weights.bin').read_bytes()==b'original' and not list(target.glob('.copy-*'))
    copy_tree(source,target);assert (target/'weights.bin').stat().st_size==4*1024*1024


def test_hash_can_be_interrupted_inside_large_file(tmp_path):
    (tmp_path/'large.bin').write_bytes(b'x'*(3*1024*1024));calls=[]
    def stop():
        calls.append(True)
        if len(calls)>2:raise OperationCanceled('stop')
    with pytest.raises(OperationCanceled):manifest(tmp_path,check=stop)


def test_result_download_uses_independent_channel(engine,setup_project,monkeypatch,tmp_path):
    task,project,servers=running(engine,setup_project);remote=engine.remote(servers[0]);remote.complete(task)
    shared=remote.sftp
    monkeypatch.setattr(shared,'get',lambda *a,**k:(_ for _ in ()).throw(AssertionError('shared channel used')))
    independent=LocalSFTP();independent.get_channel=lambda:SimpleNamespace(settimeout=lambda n:None)
    remote.client=object();monkeypatch.setattr(remote,'log_channel',lambda:nullcontext(independent))
    entered=threading.Event();release=threading.Event()
    def occupy():
        with remote.lock:entered.set();release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker=pool.submit(occupy);assert entered.wait(1)
        try:
            start=time.monotonic();result=remote.download(tmp_path/'download',task,'runs/'+task['attempt_id']+'/workspace')
            assert time.monotonic()-start<.5 and 'output.txt' in result['files']
        finally:release.set();worker.result(3)


def test_settings_rejection_preserves_memory_and_disk(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];before=copy.deepcopy(e.settings)
    for data in ({'poll_seconds':2,'history_samples':0},{'poll_seconds':None},{'paused':'false'},{'poll_seconds':2.5}):
        assert c.post('/api/settings',json=data,headers=h).status_code==400
        assert e.settings==before and e.store.get('settings','global')==before


@pytest.mark.parametrize('value',[True,None,'NaN',1.25])
def test_integer_fields_reject_invalid_values(value):
    with pytest.raises(ValueError):Engine.numeric(value,1,100,True)


def test_async_probe_from_old_configuration_is_discarded(engine,setup_project,monkeypatch):
    project,servers=setup_project;server=servers[0];engine.background_runtime=True;entered=threading.Event();release=threading.Event();original=engine.poll_one
    def slow(s):
        if s['id']==server['id']:entered.set();assert release.wait(3)
        return original(s)
    monkeypatch.setattr(engine,'poll_one',slow)
    try:
        engine.tick();assert entered.wait(2)
        engine.save_server({'id':server['id'],'work_root':'/new/work'})
        release.set()
        for f in engine.probe_pending.values():f.result(3)
        engine.tick()
        assert not engine.store.get('servers',server['id']).get('connected')
    finally:release.set()


def test_control_jobs_and_replies_do_not_duplicate_large_manifests(client):
    c,app=client;h=login(c,app);e=app.extensions['engine']
    server=e.save_server({'name':'control','host':'fake','username':'tester','work_root':'/work','data_root':'/data'})
    p=e.create_project({'name':'compact'});t=e.create_task(p['id'],{'name':'run','command':'echo run','server_ids':[server['id']]});e.task_action(t['id'],'queue');e.tick()
    task=e.store.get('tasks',t['id']);task['input_manifest']={f'file-{i}':{'size':1,'sha256':'a'*64} for i in range(2000)};e.store.put('tasks',task)
    response=c.post('/api/tasks/'+t['id']+'/action',json={'action':'pause'},headers=h)
    assert response.status_code==200 and len(response.data)<12000
    job=e.store.get('jobs',response.json['id']);assert len(job['operation'])<300 and 'input_manifest' not in response.json['task']
    until=time.monotonic()+2
    while e.store.get('jobs',job['id'])['status'] in ('queued','running') and time.monotonic()<until:time.sleep(.01)
    job=e.store.get('jobs',job['id']);assert 'input_manifest' not in job['result'] and job['status']=='success'


def test_controller_shutdown_preserves_preparation_for_restart(engine,setup_project,monkeypatch):
    p,servers=setup_project;t=engine.create_task(p['id'],{'name':'shutdown','command':'echo hi','server_ids':[servers[0]['id']]})
    remote=engine.remote(servers[0]);original=remote.upload
    def stop(*args,**kwargs):
        engine.stop_event.set();return original(*args,**kwargs)
    monkeypatch.setattr(remote,'upload',stop);engine.task_action(t['id'],'queue');engine.tick()
    saved=engine.store.get('tasks',t['id']);assert saved['status']=='staging' and saved['attempt_id'] and not saved.get('cancel_requested')
    other=Engine(engine.root,remote_factory=engine.remote_factory);other.settings['paused']=True
    try:other.tick();assert other.store.get('tasks',t['id'])['status']=='staging' and other.store.get('tasks',t['id'])['attempt_id']==saved['attempt_id']
    finally:other.close()


def test_preparation_failure_backs_off_but_manual_retry_is_immediate(engine,setup_project,monkeypatch):
    p,servers=setup_project;t=engine.create_task(p['id'],{'name':'missing-data','command':'echo hi','datasets':['missing'],'server_ids':[servers[0]['id']]});remote=engine.remote(servers[0]);calls=[];original=remote.call
    def call(action,*args,**kwargs):
        if action=='dataset_exists':calls.append(action)
        return original(action,*args,**kwargs)
    monkeypatch.setattr(remote,'call',call);engine.task_action(t['id'],'queue');engine.tick()
    saved=engine.store.get('tasks',t['id']);assert saved['preparation_retry_at']>time.time() and saved['status']=='waiting'
    count=len(calls);engine.tick();assert len(calls)==count
    engine.task_action(t['id'],'retry');assert 'preparation_retry_at' not in engine.store.get('tasks',t['id'])
    engine.tick();assert len(calls)>count


def test_reexecution_clears_current_outputs_but_retains_old_run(engine,setup_project):
    task,p,servers=running(engine,setup_project);engine.remote(servers[0]).complete(task);engine.tick();finished=engine.store.get('tasks',task['id']);assert finished['result_manifest']
    old=finished['attempt_id'];engine.task_action(task['id'],'retry');new=engine.store.get('tasks',task['id'])
    assert engine.store.summary('tasks',new)['output_count']==0 and new['previous_attempts'][-1]['attempt_id']==old
    assert (engine.local_project(p['id'])/'runs'/old/'result'/'output.txt').is_file()


def test_restart_continues_same_snapshot_and_skips_completed_input_files(engine,setup_project,monkeypatch):
    p,servers=setup_project;t=engine.create_task(p['id'],{'name':'continue-upload','command':'echo hi','server_ids':[servers[0]['id']]})
    (engine.local_project(p['id'])/'code'/'second.py').write_text('SECOND=1')
    remote=engine.remote(servers[0]);original=remote.upload;completed=[]
    def partial(local,spec,prefix,**kwargs):
        if prefix.endswith('/workspace'):
            inventory=manifest(local);first=next(iter(inventory));completed.append(first)
            remote._upload(local,spec,prefix,inventory={first:inventory[first]},verify_after=False)
            engine.stop_event.set();raise OperationCanceled('controller stopped')
        return original(local,spec,prefix,**kwargs)
    monkeypatch.setattr(remote,'upload',partial);engine.task_action(t['id'],'queue');engine.tick()
    saved=engine.store.get('tasks',t['id']);attempt=saved['attempt_id'];assert saved['status']=='staging' and saved['attempts']==0
    # The retained input must remain immutable even if someone edits source files outside the tool.
    (engine.local_project(p['id'])/'code'/'model.py').write_text('VERSION=99')
    other=Engine(engine.root,remote_factory=engine.remote_factory);other.settings['paused']=True
    try:
        other.tick();assert other.store.get('tasks',t['id'])['attempt_id']==attempt
        remote2=other.remote(servers[0]);sent=[];put=remote2.sftp.put
        monkeypatch.setattr(remote2.sftp,'put',lambda source,target,**kwargs:(sent.append(target),put(source,target,**kwargs))[1])
        other.settings['paused']=False;other.tick();value=other.store.get('tasks',t['id'])
        assert value['status']=='running' and value['attempt_id']==attempt and value['attempts']==1
        assert not any('/workspace/'+completed[0]+'.upload-' in target for target in sent)
        assert (remote2.project(value)/'runs'/attempt/'workspace'/'model.py').read_text()=='VERSION=1\n'
    finally:other.close()


@pytest.mark.parametrize('second_staged',[False,True])
def test_recovery_reserves_project_preparation_slot_before_remote_status_returns(engine,setup_project,monkeypatch,second_staged):
    p,servers=setup_project;engine.settings['paused']=True;engine.tick()
    first=engine.create_task(p['id'],{'name':'recover-first','command':'echo hi','server_ids':[servers[0]['id']]})
    second=engine.create_task(p['id'],{'name':'queued-second','command':'echo hi','server_ids':[servers[1]['id']]})
    engine.task_action(first['id'],'queue');engine.task_action(second['id'],'queue')
    gpu=engine.store.get('servers',servers[0]['id'])['snapshot']['gpus'][0];saved=engine.reserve_task(first,servers[0],gpu)
    if second_staged:engine.reserve_task(second,servers[1],engine.store.get('servers',servers[1]['id'])['snapshot']['gpus'][0])
    remote=engine.remote(servers[0]);original=remote.call;entered=threading.Event();release=threading.Event()
    def slow(action,*args,**kwargs):
        if action=='status':entered.set();assert release.wait(3)
        return original(action,*args,**kwargs)
    monkeypatch.setattr(remote,'call',slow);engine.background_runtime=True;engine.settings['paused']=False
    try:
        engine.tick();assert entered.wait(1)
        assert engine.runtime_pending[first['id']]['kind']=='recover_preparation'
        value=engine.store.get('tasks',second['id']);assert value['status']==('staging' if second_staged else 'waiting') and value['attempts']==0
        if not second_staged:assert not value.get('attempt_id')
    finally:release.set()
    until=time.monotonic()+3
    while first['id'] in engine.runtime_pending and time.monotonic()<until:time.sleep(.01)
    value=engine.store.get('tasks',first['id']);assert value['status']=='running' and value['attempt_id']==saved['attempt_id']
    if second_staged:
        while second['id'] in engine.runtime_pending and time.monotonic()<until:time.sleep(.01)
        engine.tick()
        until=time.monotonic()+3
        while second['id'] in engine.runtime_pending and time.monotonic()<until:time.sleep(.01)
        assert engine.store.get('tasks',second['id'])['status']=='running'


def test_legacy_job_summaries_drop_inventories_but_preserve_full_history(client):
    import json
    from scheduler.storage import Store
    c,app=client;h=login(c,app);e=app.extensions['engine']
    inventory={f'file-{i}':{'size':1,'sha256':'a'*64} for i in range(2000)}
    job={'id':'legacy-inventory','label':'刷新副本清单','operation':'refresh_replica:[]','created_at':time.time(),'status':'success','result':{'files':inventory,'exists':True}}
    e.store.put('jobs',job)
    with e.store.lock,e.store.db:e.store.db.execute("UPDATE record_summaries SET data=? WHERE kind='jobs' AND id=?",(json.dumps(job),'legacy-inventory'))
    upgraded=Store(e.root)
    try:
        compact=upgraded.summaries('jobs')[0];assert 'files' not in compact['result'] and compact['result']['file_count']==2000
        assert upgraded.get('jobs',job['id'])['result']['files']==inventory
    finally:upgraded.close()
    response=c.get('/api/state');assert response.status_code==200 and len(response.data)<20000


def test_transfer_cancel_checks_use_compact_records_and_reject_old_attempts(engine,setup_project,monkeypatch):
    p,servers=setup_project;t=engine.create_task(p['id'],{'name':'compact-cancel','command':'echo hi'})
    t.update(status='staging',attempt_id='original',input_manifest={f'file-{i}':{'size':1,'sha256':'a'*64} for i in range(1000)})
    engine.store.put('tasks',t);original=engine.store.get
    def get(kind,key):
        assert kind!='tasks','A transfer callback must not decode the full file inventory'
        return original(kind,key)
    monkeypatch.setattr(engine.store,'get',get)
    for _ in range(10):engine.check_task_cancel(t)
    engine.store.put('tasks',{**t,'attempt_id':'new'})
    with pytest.raises(OperationCanceled):engine.check_task_cancel(t)
    engine.store.put('tasks',{**t,'cancel_requested':True})
    with pytest.raises(OperationCanceled):engine.check_task_cancel(t)
