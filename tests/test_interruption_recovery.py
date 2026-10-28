import json
import time

import pytest

from scheduler.content import Content
from test_content_storage import enable,create
from test_remote_agent import agent
from test_api import client,login


def interrupt(engine,server,task):
    remote=engine.remote(server);run=remote.project(task)/'runs'/task['attempt_id']
    (run/'workspace'/'unfinished.bin').write_bytes(b'incomplete'*100)
    (run/'_control'/'status.json').write_text(json.dumps({'state':'lost','confirmed_stopped':True,'interruption_cause':'server_reboot'}))
    return remote,run


def test_reboot_cleans_before_requeue_preserves_log_and_exact_inputs(engine,setup_project):
    enable(engine);project,servers=setup_project;task=create(engine,project,servers[0],max_retries=0)
    attempt=task['attempt_id'];snapshot=task['input_revision'];remote,run=interrupt(engine,servers[0],task)
    (engine.local_project(project['id'])/'code'/'model.py').write_text('new source must not replace snapshot')
    engine.recover();value=engine.store.get('tasks',task['id'])
    assert value['status']=='queued' and value['attempt_id'] is None and value['interruption_retries']==1
    assert not (run/'workspace').exists()
    assert (run/'_control'/'run.log').is_file()
    pool=Content(engine,project['id']);assert pool.return_destination(attempt,'control').joinpath('run.log').read_text()=='experiment started'
    assert pool.read_catalog(attempt)['outputs']=={} and pool.read_catalog(attempt)['inputs']
    assert value['previous_attempts'][0]['interrupted'] and value['previous_attempts'][0]['synced_at'] is None
    assert value['preparation_retry_at']>time.time()
    engine.tick();assert engine.store.get('tasks',task['id'])['status']=='queued'
    value['preparation_retry_at']=0;engine.store.put('tasks',value);engine.tick();new=engine.store.get('tasks',task['id'])
    assert new['status']=='running' and new['attempt_id']!=attempt and new['input_revision']==snapshot
    assert (remote.project(new)/'runs'/new['attempt_id']/'workspace'/'model.py').read_text()=='VERSION=1\n'


def test_recovery_is_bounded_and_manual_retry_resets_budget(engine,setup_project):
    enable(engine);project,servers=setup_project;task=create(engine,project,servers[0]);task['interruption_retries']=2;engine.store.put('tasks',task)
    _,run=interrupt(engine,servers[0],task);engine.recover();value=engine.store.get('tasks',task['id'])
    assert value['status']=='failed' and '用尽' in value['reason'] and not (run/'workspace').exists()
    engine.task_action(task['id'],'retry');assert engine.store.get('tasks',task['id'])['interruption_retries']==0


@pytest.mark.parametrize('change,expected',[({'cancel_requested':True},'canceled'),({'status':'suspended'},'paused')])
def test_user_cancel_and_pause_are_never_overridden(engine,setup_project,change,expected):
    enable(engine);project,servers=setup_project;task=create(engine,project,servers[0]);task.update(change);engine.store.put('tasks',task)
    interrupt(engine,servers[0],task);engine.recover();value=engine.store.get('tasks',task['id'])
    assert value['status']==expected and value['attempt_id']==task['attempt_id']


def test_failed_log_download_never_cleans_or_relaunches(engine,setup_project):
    enable(engine);project,servers=setup_project;task=create(engine,project,servers[0]);remote,run=interrupt(engine,servers[0],task)
    remote.fail_download=True;engine.recover();assert (run/'workspace').is_dir()
    value=engine.store.get('tasks',task['id']);assert value['status']=='lost' and value['attempt_id']==task['attempt_id']
    remote.fail_download=False;value['recovery_check_after']=0;engine.store.put('tasks',value);engine.recover()
    assert engine.store.get('tasks',task['id'])['status']=='queued'


def test_missing_input_never_retries_with_changed_code(engine,setup_project):
    enable(engine);project,servers=setup_project;task=create(engine,project,servers[0]);pool=Content(engine,project['id'])
    pool.catalog(task['attempt_id']).unlink();_,run=interrupt(engine,servers[0],task);engine.recover()
    value=engine.store.get('tasks',task['id']);assert value['status']=='failed' and '快照缺失' in value['reason']
    assert not (run/'workspace').exists()


def test_agent_boot_id_proof_and_safe_cleanup(agent,tmp_path,monkeypatch):
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32,'attempt_id':'b'*32}
    base=agent.project_path(spec,create=True);run=base/'runs'/spec['attempt_id'];ctrl=run/'_control';ctrl.mkdir(parents=True)
    (ctrl/'process.json').write_text(json.dumps({'pid':1,'supervisor_pid':2,'token':'old','supervisor_token':'old','boot_id':'old-boot','started_at':100}))
    monkeypatch.setattr(agent,'boot_identity',lambda:{'boot_id':'new-boot','boot_started_at':200})
    # Even a coincidentally reused PID token cannot make an old-boot run live.
    monkeypatch.setattr(agent,'process_token',lambda pid:'old')
    proof=agent.dispatch('interruption_status',spec);assert proof['confirmed_stopped'] and proof['state']=='lost'
    (run/'workspace').mkdir();(run/'workspace'/'file.bin').write_bytes(b'x'*100)
    with pytest.raises(ValueError):agent.dispatch('discard_failed_workspace',spec)
    agent.dispatch('seal_interrupted',spec);assert agent.run_status(spec)['exit_code'] is None
    result=agent.dispatch('discard_failed_workspace',spec);assert result['removed_workspace_bytes']==100
    assert not (run/'workspace').exists() and (ctrl/'process.json').is_file()
    assert agent.dispatch('discard_failed_workspace',spec)==result


def test_agent_same_boot_unknown_never_seals(agent,tmp_path,monkeypatch):
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32,'attempt_id':'b'*32}
    base=agent.project_path(spec,create=True);ctrl=base/'runs'/spec['attempt_id']/'_control';ctrl.mkdir(parents=True)
    (ctrl/'process.json').write_text(json.dumps({'pid':11,'supervisor_pid':12,'token':'1','supervisor_token':'1','boot_id':'same','started_at':100}))
    monkeypatch.setattr(agent,'boot_identity',lambda:{'boot_id':'same','boot_started_at':200})
    monkeypatch.setattr(agent,'process_token',lambda pid:None)
    assert not agent.dispatch('interruption_status',spec)['confirmed_stopped']
    with pytest.raises(ValueError,match='未证明'):agent.dispatch('seal_interrupted',spec)


@pytest.mark.parametrize('tokens,confirmed',[('200000',True),('10',False)])
def test_legacy_reboot_needs_clock_and_start_tick_evidence(agent,tmp_path,monkeypatch,tokens,confirmed):
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32,'attempt_id':'b'*32}
    base=agent.project_path(spec,create=True);ctrl=base/'runs'/spec['attempt_id']/'_control';ctrl.mkdir(parents=True)
    (ctrl/'process.json').write_text(json.dumps({'pid':11,'supervisor_pid':12,'token':tokens,'supervisor_token':tokens,'started_at':100}))
    monkeypatch.setattr(agent,'boot_identity',lambda:{'boot_id':'current','boot_started_at':200})
    monkeypatch.setattr(agent,'process_token',lambda pid:None)
    monkeypatch.setattr(agent.os,'sysconf',lambda key:100,raising=False)
    original=agent.Path.read_text
    monkeypatch.setattr(agent.Path,'read_text',lambda p,*a,**k:'1000.00 0' if p.as_posix()=='/proc/uptime' else original(p,*a,**k))
    assert agent.dispatch('interruption_status',spec)['confirmed_stopped'] is confirmed


def test_settings_validate_and_persist_independent_recovery_budget(client):
    c,app=client;headers=login(c,app)
    response=c.post('/api/settings',json={'auto_requeue_interrupted':False,'interruption_retry_limit':4,'interruption_retry_delay':60},headers=headers)
    assert response.status_code==200 and response.json['interruption_retry_limit']==4
    assert response.json['auto_requeue_interrupted'] is False
    assert c.post('/api/settings',json={'auto_requeue_interrupted':'false'},headers=headers).status_code==400
    assert c.post('/api/settings',json={'interruption_retry_limit':-1},headers=headers).status_code==400


def test_failed_workspace_cleanup_preserves_successful_run_and_archived_outputs(engine,setup_project):
    enable(engine);project,servers=setup_project;failed=create(engine,project,servers[0]);remote=engine.remote(servers[0])
    remote.complete(failed,exit_code=1,output='useful diagnostic output');engine.tick()
    successful=create(engine,project,servers[0]);remote.complete(successful,output='keep successful result');engine.tick()
    report=engine.cleanup_failed_workspaces()
    assert report['cleaned_count']==1
    assert not (remote.project(failed)/'runs'/failed['attempt_id']/'workspace').exists()
    assert (remote.project(successful)/'runs'/successful['attempt_id']/'workspace').exists()
    assert Content(engine,project['id']).artifact(failed['attempt_id'],'output.txt').read_text()=='useful diagnostic output'
    assert engine.cleanup_failed_workspaces()['cleaned_count']==0
