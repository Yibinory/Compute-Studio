import threading
import time

import pytest

from scheduler.environments import resolve
from scheduler.scheduling_edits import update_policies
from test_api import client, login


def wait_for(predicate,timeout=3):
    end=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>end:raise AssertionError('timed out')
        time.sleep(.01)


def test_slow_upload_does_not_block_retry_edit_state_or_cancel(client,monkeypatch):
    c,app=client;headers=login(c,app);e=app.extensions['engine'];e.background_runtime=True
    server=e.save_server({'name':'slow','host':'fake','username':'tester','work_root':'/work','data_root':'/data'})
    p=e.create_project({'name':'slow-project'});t=e.create_task(p['id'],{'name':'slow-task','command':'echo run'})
    old=e.create_task(p['id'],{'name':'retry-task','command':'echo retry','conda_env':'/wrong/server/env'})
    old.update(status='failed',attempt_id='old-run',server_id=server['id'],resolved_conda_env='old-env',conda_env_source='task')
    e.store.put('tasks',old);remote=e.remote(server);entered=threading.Event();release=threading.Event();original=remote.upload
    def slow(*args,**kwargs):
        kwargs['progress'](100,100);entered.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(remote,'upload',slow)
    e.task_action(t['id'],'queue')
    try:
        e.tick();wait_for(lambda:all(f.done() for f in e.probe_pending.values()));e.tick();assert entered.wait(2)
        staged=e.store.get('tasks',t['id']);assert staged['status']=='staging' and staged['runtime_progress']['phase']=='verifying' and staged['runtime_progress']['percent']==100
        # A metadata operation may still own the old broad engine lock.
        with e.lock:
            started=time.monotonic();reply=c.post('/api/tasks/scheduling',headers=headers,json={'task_ids':[old['id']],'changes':{'conda_env':'','server_conda_envs':{server['id']:'pytorch'}}})
            assert reply.status_code==200 and time.monotonic()-started<.25
            started=time.monotonic();reply=c.post('/api/tasks/'+old['id']+'/action',headers=headers,json={'action':'retry'})
            assert reply.status_code==200 and reply.json['status']=='queued' and time.monotonic()-started<.25
            started=time.monotonic();assert c.get('/api/state').status_code==200 and time.monotonic()-started<.25
            started=time.monotonic();reply=c.post('/api/tasks/'+t['id']+'/action',headers=headers,json={'action':'cancel'})
            assert reply.status_code==200 and time.monotonic()-started<.25
            assert e.store.get('tasks',t['id'])['cancel_requested']
        e.tick();assert e.store.get('tasks',t['id'])['attempt_id']==staged['attempt_id']
    finally:release.set()
    wait_for(lambda:t['id'] not in e.runtime_pending)
    assert e.store.get('tasks',t['id'])['status']=='canceled'
    assert not any(sid==server['id'] and attempt==staged['attempt_id'] for sid,attempt in remote.launches)


def test_metadata_scan_does_not_delay_running_state(engine,setup_project,monkeypatch):
    p,servers=setup_project;engine.background_runtime=True;entered=threading.Event();release=threading.Event()
    def scan(*args,**kw):entered.set();assert release.wait(3)
    monkeypatch.setattr(engine,'refresh_replica',scan)
    t=engine.create_task(p['id'],{'name':'metadata','command':'echo hi','server_ids':[servers[0]['id']]});engine.task_action(t['id'],'queue')
    try:
        engine.tick();wait_for(lambda:all(f.done() for f in engine.probe_pending.values()));engine.tick();assert entered.wait(2)
        wait_for(lambda:t['id'] not in engine.runtime_pending)
        assert engine.store.get('tasks',t['id'])['status']=='running'
    finally:release.set()


def test_per_server_environment_and_inherit_clear_imported_absolute_path(engine,setup_project):
    p,servers=setup_project
    task=engine.create_task(p['id'],{'name':'env','command':'echo hi','conda_env':'/foreign/env',
          'server_conda_envs':{servers[0]['id']:'torch6',servers[1]['id']:'torch8'}})
    for server,expected in zip(servers,['torch6','torch8']):assert resolve(task,p,server)=={'conda_env':expected,'source':'task_server'}
    task.update(status='failed',resolved_conda_env='/foreign/env',previous_attempts=[{'attempt_id':'prior'}]);engine.store.put('tasks',task)
    update_policies(engine,[task['id']],{'conda_env':'','server_conda_envs':{}})
    saved=engine.store.get('tasks',task['id']);assert saved['status']=='failed' and saved['previous_attempts']==task['previous_attempts'] and saved['resolved_conda_env']=='/foreign/env'
    project={**p,'server_conda_envs':{servers[0]['id']:'project-env'}}
    assert resolve(saved,project,servers[0])=={'conda_env':'project-env','source':'project'}
    with pytest.raises(ValueError):update_policies(engine,[task['id']],{'priority':1,'conda_env':'pytorch'})


def test_invalid_server_mapping_is_atomic(engine,setup_project):
    p,servers=setup_project;task=engine.create_task(p['id'],{'name':'env','command':'echo hi'})
    for changes in ({'server_conda_envs':[]},{'server_conda_envs':{'missing':'torch'}},{'server_conda_envs':{servers[0]['id']:'../bad'}}):
        with pytest.raises(ValueError):update_policies(engine,[task['id']],changes)
        assert engine.store.get('tasks',task['id'])==task


def test_state_reads_compact_tasks_instead_of_full_file_manifests(client,monkeypatch):
    c,app=client;login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'large-manifest'})
    t=e.create_task(p['id'],{'name':'large','command':'echo hi'})
    t['result_manifest']={f'file-{i}':{'size':1,'sha256':str(i)} for i in range(1000)};e.store.put('tasks',t)
    original=e.store.all
    def all_records(kind):
        assert kind!='tasks','State should never decode full task file inventories'
        return original(kind)
    monkeypatch.setattr(e.store,'all',all_records)
    response=c.get('/api/state');assert response.status_code==200
    summary=response.json['tasks'][0];assert summary['output_count']==1000 and len(summary['outputs'])==3
    assert 'result_manifest' not in summary and len(response.data)<20000


@pytest.mark.parametrize('remote_started',[False,True])
def test_controller_restart_recovers_preparation_without_duplicate_launch(engine,setup_project,remote_started):
    from scheduler.engine import Engine
    from conftest import FakeRemote
    p,servers=setup_project;t=engine.create_task(p['id'],{'name':'restart-stage','command':'echo hi','server_ids':[servers[0]['id']]})
    engine.task_action(t['id'],'queue');engine.tick();saved=engine.store.get('tasks',t['id']);attempt=saved['attempt_id'];launches=len(FakeRemote.launches)
    if not remote_started:
        remote=engine.remote(servers[0]);control=remote.project(saved)/'runs'/attempt/'_control'/'status.json';control.unlink()
    saved['status']='staging';engine.store.put('tasks',saved)
    other=Engine(engine.root,remote_factory=FakeRemote);other.settings['paused']=True
    try:
        other.tick();value=other.store.get('tasks',t['id'])
        assert value['status']==('running' if remote_started else 'staging')
        assert value['attempt_id']==attempt
        assert len(FakeRemote.launches)==launches and value['command']==t['command']
    finally:other.close()
