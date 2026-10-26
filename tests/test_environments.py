import json
import time
from types import SimpleNamespace

import pytest

from scheduler.environments import environment, resolve
from scheduler.engine import Engine
from conftest import FakeRemote
from test_api import client, login
from test_remote_agent import agent


@pytest.mark.parametrize('task_env,project_env,server_env,expected,source',[
    ('explicit','project','server','explicit','task'),
    ('','/opt/envs/project','server','/opt/envs/project','project'),
    ('','','server','server','server'),
    ('','','','','shell'),
])
def test_launch_uses_environment_precedence_and_records_actual_selection(engine,setup_project,monkeypatch,task_env,project_env,server_env,expected,source):
    project,servers=setup_project;server=servers[1]
    server=engine.save_server({'id':server['id'],'default_conda_env':server_env})
    engine.project_environments(project['id'],{'server_conda_envs':{servers[0]['id']:'first-host-env',server['id']:project_env}})
    task=engine.create_task(project['id'],{'name':'train','command':'echo fixture','conda_env':task_env})
    remote=engine.remote(server);original=remote.call;payloads=[]
    def call(action,payload=None,timeout=120):
        if action=='launch':payloads.append(payload)
        return original(action,payload,timeout)
    monkeypatch.setattr(remote,'call',call)
    engine.launch(task,server,{'uuid':'GPU-'+server['id'],'index':0})
    saved=engine.store.get('tasks',task['id'])
    assert saved['status']=='running' and saved['resolved_conda_env']==expected
    assert saved['conda_env_source']==source and payloads[0]['conda_env']==expected
    # Updating defaults affects future launches, not the running snapshot.
    engine.project_environments(project['id'],{'server_conda_envs':{server['id']:'changed'}})
    assert engine.store.get('tasks',task['id'])['resolved_conda_env']==expected


def test_inventory_is_cached_across_restart_and_invalidated_by_connection(tmp_path):
    original=Engine(tmp_path,remote_factory=FakeRemote)
    try:
        s=original.save_server({'name':'test','host':'fixture','username':'test','work_root':'/work/test','data_root':'/data/test'})
        assert original.current_conda(s)['status']=='unknown'
        value=original.discover_conda(s['id']);assert value['status']=='ready'
        assert value['environments'][1]['prefix']=='/opt/conda/envs/pytorch'
        assert 'configuration' not in value
    finally:original.close()
    restored=Engine(tmp_path,remote_factory=FakeRemote)
    try:
        changed=restored.save_server({'id':s['id'],'default_conda_env':'pytorch','data_root':'/data/changed'})
        assert restored.current_conda(changed)['checked_at']==value['checked_at']
        changed=restored.save_server({'id':s['id'],'conda_executable':'/different/conda'})
        assert restored.current_conda(changed)['status']=='unknown'
    finally:restored.close()


def test_inventory_failure_is_reported_and_not_replaced_with_fake_environments(engine,setup_project,monkeypatch):
    _,servers=setup_project;remote=engine.remote(servers[0])
    monkeypatch.setattr(remote,'call',lambda *args,**kw:(_ for _ in ()).throw(ValueError('No Conda')))
    with pytest.raises(ValueError,match='No Conda'):engine.discover_conda(servers[0]['id'])
    cached=engine.current_conda(servers[0]);assert cached['status']=='failed' and cached['environments']==[]
    assert cached['error']=='No Conda'


def test_async_inventory_api_and_project_configuration(client,monkeypatch):
    c,app=client;assert c.get('/api/servers/no/environments').status_code==401
    h=login(c,app);engine=app.extensions['engine']
    s=engine.save_server({'name':'test','host':'fixture','username':'test','work_root':'/work/test','data_root':'/data/test'})
    p=engine.create_project({'name':'test'})
    remote=engine.remote(s);original=remote.call;calls=[]
    def delayed(action,*args,**kw):
        calls.append(action);time.sleep(.2);return original(action,*args,**kw)
    monkeypatch.setattr(remote,'call',delayed)
    assert c.get('/api/servers/'+s['id']+'/environments').get_json()['status']=='unknown'
    assert calls==[]
    url='/api/servers/'+s['id']+'/environments/refresh'
    assert c.post(url,json={}).status_code==403
    start=time.monotonic();r=c.post(url,json={},headers=h)
    assert r.status_code==202 and time.monotonic()-start<.15
    job=r.get_json();assert c.post(url,json={},headers=h).get_json()['id']==job['id']
    for _ in range(200):
        if engine.store.get('jobs',job['id'])['status']=='success':break
        time.sleep(.01)
    cached=c.get('/api/servers/'+s['id']+'/environments').get_json()
    assert cached['status']=='ready' and calls==['conda_envs']
    url='/api/projects/'+p['id']+'/environments'
    assert c.post(url,json={'server_conda_envs':{s['id']:'/opt/conda/envs/pytorch'}},headers=h).status_code==200
    assert resolve({},engine.store.get('projects',p['id']),s)['conda_env']=='/opt/conda/envs/pytorch'
    assert c.post(url,json={'server_conda_envs':{'missing':'torch'}},headers=h).status_code==400
    assert c.post(url,json={'server_conda_envs':[]},headers=h).status_code==400
    assert c.post(url,json={'server_conda_envs':{s['id']:''}},headers=h).get_json()['server_conda_envs']=={}


@pytest.mark.parametrize('value',['-n unsafe','bad;cmd','bad\nvalue','../env','/opt/../env'])
def test_invalid_environment_values(value):
    with pytest.raises(ValueError):environment(value)


def test_agent_inventory_preserves_duplicate_names_and_runs_without_shell(agent,monkeypatch):
    seen=[]
    monkeypatch.setattr(agent,'conda_binary',lambda spec:'/opt/conda/bin/conda')
    def run(args,**kwargs):
        seen.append((args,kwargs));return SimpleNamespace(returncode=0,stdout=json.dumps({'envs':['/opt/conda','/opt/conda/envs/torch','/mnt/custom/torch','/opt/conda']}),stderr='')
    monkeypatch.setattr(agent.subprocess,'run',run)
    value=agent.dispatch('conda_envs',{})
    assert len(value['environments'])==3 and value['environments'][0]['name']=='base'
    assert len([x for x in value['environments'] if x['name']=='torch'])==2
    assert seen[0][0]==['/opt/conda/bin/conda','env','list','--json']
    assert 'shell' not in seen[0][1]
    assert agent.conda_target('/mnt/env with spaces')==['-p','/mnt/env with spaces']
    assert agent.conda_target('torch')==['-n','torch']


def test_missing_conda_writes_exit_record_instead_of_lost_run(agent,tmp_path,monkeypatch):
    project={'work_root':str(tmp_path/'work'),'project_id':'a'*32,'attempt_id':'b'*32}
    base=agent.project_path(project,True);run=base/'runs'/project['attempt_id'];(run/'_control').mkdir(parents=True);(run/'workspace').mkdir()
    spec={**project,'env':{},'command':'echo test','conda_env':'missing'}
    path=tmp_path/'spec.json';path.write_text(json.dumps(spec))
    monkeypatch.setattr(agent,'conda_binary',lambda spec:(_ for _ in ()).throw(ValueError('No Conda')))
    agent.supervise(str(path))
    result=json.loads((run/'_control'/'exit.json').read_text())
    assert result['exit_code']==111 and result['error']=='No Conda'
