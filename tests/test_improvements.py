import time

import pytest

from conftest import FakeRemote
from test_api import login, client
from scheduler.files import manifest
from scheduler.graph import outputs
from scheduler.sharing import detect


def share_work(engine, servers):
    base=engine.root/'shared-work'
    for server in servers:
        FakeRemote.work_roots[server['id']]=base
    return base


def test_work_sharing_is_proven_and_cleanup_requires_expanded_confirmation(engine,setup_project):
    p,servers=setup_project;share_work(engine,servers)
    a,b=[s['id'] for s in servers]
    r=engine.remote(servers[0]);r.call('project',{'project_id':p['id']})
    (r.project({'project_id':p['id']})/'manual.txt').write_text('preserve this')
    plan=engine.cleanup_plan(p['id'],[a])
    assert plan['shared'] and set(plan['affected_server_ids'])=={a,b}
    assert not list(r.work_base.glob('.compute-share-*'))
    with pytest.raises(ValueError,match='共享'):
        engine.clean_project(p['id'],[a])
    assert r.project({'project_id':p['id']}).exists()
    result=engine.clean_project(p['id'],[a],confirmed_server_ids=[a,b])
    assert len(result)==1 and set(result[0]['shared_server_ids'])=={a,b}
    assert not r.project({'project_id':p['id']}).exists()
    assert (engine.local_project(p['id'])/'remote-archives'/a/'snapshot'/'manual.txt').read_text()=='preserve this'
    assert all(not engine.store.get('replicas',p['id']+':'+sid)['exists'] for sid in (a,b))


def test_same_path_spelling_does_not_prove_sharing(engine,setup_project):
    p,servers=setup_project
    assert servers[0]['work_root']==servers[1]['work_root']
    value=engine.detect_storage('work')
    assert len(value['groups'])==2
    assert not engine.cleanup_plan(p['id'],[servers[0]['id']])['shared']


def test_unreachable_peer_blocks_cleanup_before_any_deletion(engine,setup_project):
    p,servers=setup_project
    engine.remote(servers[1]).offline=True
    with pytest.raises(ValueError,match='无法排除'):
        engine.clean_project(p['id'],[servers[0]['id']])
    assert not list(engine.remote(servers[0]).work_base.glob('.compute-share-*'))


def test_sharing_configuration_changes_invalidate_cache(engine,setup_project):
    p,servers=setup_project;engine.detect_storage('work')
    assert engine.current_sharing('work')
    engine.save_server({**servers[0],'work_root':'/new/compute'})
    assert not engine.current_sharing('work')


def test_cleanup_rejects_changed_confirmation_and_running_project(engine,setup_project):
    p,servers=setup_project;share_work(engine,servers)
    ids=[s['id'] for s in servers]
    with pytest.raises(ValueError,match='变化'):
        engine.clean_project(p['id'],None,confirmed_server_ids=[ids[0]])
    task=engine.create_task(p['id'],{'name':'running','command':'echo hi'})
    task['status']='running';engine.store.put('tasks',task)
    with pytest.raises(ValueError,match='运行'):
        engine.cleanup_plan(p['id'],[ids[0]])


def test_work_and_data_share_groups_are_independent(engine,setup_project):
    p,servers=setup_project;share_work(engine,servers)
    assert len(engine.detect_storage('work')['groups'])==1
    assert len(engine.detect_storage('data')['groups'])==2


def test_shared_ttl_uses_newest_use_and_longest_retention(engine,setup_project,monkeypatch):
    p,servers=setup_project;share_work(engine,servers)
    for s in servers:
        s.update(enabled=True,connected=True,retention_days=1);engine.store.put('servers',s)
    r=engine.remote(servers[0]);r.call('project',{'project_id':p['id']})
    engine.refresh_replica(p['id'],servers[0]['id'])
    old=engine.store.get('replicas',p['id']+':'+servers[0]['id']);old['last_used_at']=time.time()-3*86400;engine.store.put('replicas',old)
    engine.auto_cleanup()
    assert r.project({'project_id':p['id']}).exists()
    for replica in engine.store.all('replicas'):
        replica['last_used_at']=time.time()-3*86400;engine.store.put('replicas',replica)
    s=engine.store.get('servers',servers[1]['id']);s['retention_days']=5;engine.store.put('servers',s)
    engine.auto_cleanup();assert r.project({'project_id':p['id']}).exists()
    s['retention_days']=1;engine.store.put('servers',s)
    calls=[]
    for server in servers:
        remote=engine.remote(server);original=remote.call
        def counted(action,payload=None,timeout=120,original=original):
            if action=='clean':calls.append(action)
            return original(action,payload,timeout)
        monkeypatch.setattr(remote,'call',counted)
    engine.auto_cleanup();assert not r.project({'project_id':p['id']}).exists()
    assert calls==['clean']


def test_asymmetric_storage_is_unverified(engine,setup_project):
    p,servers=setup_project
    class OneWay:
        def __init__(self,s):self.server=s
        def call(self,action,spec,timeout=120):
            return {'visible':self.server['id']==servers[0]['id'],'root':'/root'} if not spec.get('write') and not spec.get('remove') else {'visible':True,'root':'/root'}
    value=detect(servers,OneWay,'work')
    assert all(s['status']=='unverified' for s in value['states'].values())
    assert not value['groups']


def test_outputs_exclude_unchanged_code_and_dependencies():
    task={'input_manifest':{'train.py':{'sha256':'a','size':1},'deps/x/result':{'sha256':'b','size':2},'config.json':{'sha256':'c','size':3}},'result_manifest':{'train.py':{'sha256':'a','size':1},'deps/x/result':{'sha256':'b','size':2},'config.json':{'sha256':'d','size':4},'weights.pt':{'sha256':'e','size':20}}}
    assert [(v['path'],v['change']) for v in outputs(task)]==[('config.json','modified'),('weights.pt','created')]


def test_benchmark_persists_measured_time(engine,setup_project):
    p,servers=setup_project
    value=engine.benchmark(servers[0]['id'],0)
    assert value['measured_at']>time.time()-10
    assert value['gpu_uuid']=='GPU-'+servers[0]['id']
    assert engine.store.get('servers',servers[0]['id'])['benchmarks'][value['gpu_uuid']]['tensor_tflops']==50


def test_graph_and_notes_remain_independent_of_scheduler_updates(client):
    c,app=client;headers=login(c,app);engine=app.extensions['engine']
    p=engine.create_project({'name':'notes'})
    a=engine.create_task(p['id'],{'name':'first','command':'echo first'})
    b=engine.create_task(p['id'],{'name':'second','command':'echo second','depends_on':[a['id']]})
    stale=engine.store.get('tasks',a['id']);stale['status']='running'
    url='/api/tasks/'+a['id']+'/notes'
    note=c.post(url,json={'body':'参数 batch=64\n观察：稳定'},headers=headers).get_json()
    engine.store.put('tasks',stale)
    graph=c.get('/api/projects/'+p['id']+'/graph').get_json()
    assert graph['edges']==[{'source':a['id'],'target':b['id']}]
    assert graph['nodes'][0]['notes'][0]['body']==note['body']
    assert c.post('/api/tasks/'+b['id']+'/notes',json={'id':note['id'],'body':'bad'},headers=headers).status_code==400
    assert c.post(url,json={'id':note['id'],'body':'结论：正常'},headers=headers).status_code==200
    assert c.get('/api/tasks/'+a['id']+'/inspection').get_json()['notes'][0]['body']=='结论：正常'
    assert c.post(url,json={'id':note['id'],'delete':True},headers=headers).status_code==200
    assert not c.get('/api/tasks/'+a['id']+'/inspection').get_json()['notes']
    assert c.post(url,json={'body':' '*3},headers=headers).status_code==400


def test_notes_require_authentication_and_csrf(client):
    c,app=client
    assert c.post('/api/tasks/x/notes',json={'body':'hello'}).status_code==401
    login(c,app)
    assert c.post('/api/tasks/x/notes',json={'body':'hello'}).status_code==403


def test_notes_keep_markdown_and_formula_source_through_editing(client):
    c,app=client;headers=login(c,app);engine=app.extensions['engine']
    p=engine.create_project({'name':'Markdown_Notes'})
    task=engine.create_task(p['id'],{'name':'math','command':'echo test'})
    body=r'''## Observation

**Formula** $E=mc^2$ and \(\alpha+\beta\).

$$
L=\frac{1}{N}\sum_i (y_i-\hat{y}_i)^2
$$

```bash
echo "$CUDA_VISIBLE_DEVICES"
```'''
    url='/api/tasks/'+task['id']+'/notes'
    note=c.post(url,json={'body':body},headers=headers).get_json()
    assert note['body']==body
    assert c.get('/api/tasks/'+task['id']+'/inspection').get_json()['notes'][0]['body']==body
    edited=body+'\n\n- Follow-up observation'
    updated=c.post(url,json={'id':note['id'],'body':edited},headers=headers).get_json()
    assert updated['id']==note['id'] and updated['body']==edited
    assert engine.store.get('notes',note['id'])['body']==edited
