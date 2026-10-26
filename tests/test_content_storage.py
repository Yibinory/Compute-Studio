import hashlib
import json
from pathlib import Path

import pytest

from scheduler.content import Content
from scheduler.batch_states import apply,preview
from scheduler.scheduling_edits import update_policies
from scheduler.operations import OperationCanceled
from conftest import FakeRemote
from test_api import client,login


def enable(engine):
    engine.settings['content_store_enabled']=True;engine.store.put('settings',engine.settings)


def create(engine,project,server,**extra):
    t=engine.create_task(project['id'],{'name':'node-'+str(len(engine.store.summaries('tasks'))),'command':'echo hello','server_ids':[server['id']],**extra})
    engine.task_action(t['id'],'queue');engine.tick();return engine.store.get('tasks',t['id'])


def finish(engine,task,server,output='shared-result'):
    engine.remote(server).complete(task,output=output);engine.tick();value=engine.store.get('tasks',task['id'])
    assert value['status']=='success',value.get('reason');return value


def test_chain_transfers_only_unique_outputs_and_never_copies_recursive_dependencies(engine,setup_project):
    enable(engine);p,servers=setup_project
    a=finish(engine,create(engine,p,servers[0]),servers[0])
    assert list(a['result_manifest'])==['output.txt']
    b=create(engine,p,servers[1],depends_on=[a['id']]);assert not any('/deps/' in key[5:] for key in b['input_manifest'] if key.startswith('deps/'))
    b=finish(engine,b,servers[1])
    assert b['content_return']['downloaded_bytes']==0
    c=create(engine,p,servers[1],depends_on=[a['id'],b['id']]);assert c['content_transfer']['uploaded_bytes']==0
    pool=Content(engine,p['id']);assert pool.stats()['objects']==2
    assert not list((pool.project/'runs').glob('*/input')) and not list((pool.project/'runs').glob('*/result'))
    path=engine.remote(servers[1]).project(c)/'runs'/c['attempt_id']/'workspace'
    assert (path/'deps'/a['id']/'output.txt').read_text()=='shared-result'
    assert not (path/'deps'/b['id']/'deps').exists()


def test_explicit_dependency_and_output_selection(engine,setup_project):
    enable(engine);p,servers=setup_project;a=finish(engine,create(engine,p,servers[0]),servers[0])
    t=create(engine,p,servers[1],depends_on=[a['id']],dependency_files={a['id']:[]},output_files=['output.txt'])
    assert not any(key.startswith('deps/') for key in t['input_manifest'])
    workspace=engine.remote(servers[1]).project(t)/'runs'/t['attempt_id']/'workspace';(workspace/'unneeded.bin').write_bytes(b'x'*1024)
    final=finish(engine,t,servers[1]);assert list(final['result_manifest'])==['output.txt']
    assert not Content(engine,p['id']).contains(hashlib.sha256(b'x'*1024).hexdigest(),1024)


def test_missing_or_corrupt_remote_content_is_repaired_without_full_reupload(engine,setup_project):
    enable(engine);p,servers=setup_project;a=finish(engine,create(engine,p,servers[0]),servers[0]);pool=Content(engine,p['id'])
    remote=engine.remote(servers[0]);sha=a['result_manifest']['output.txt']['sha256'];from scheduler.object_agent import sha_path
    sha_path(remote.project(a),sha).unlink()
    t=create(engine,p,servers[0],depends_on=[a['id']]);assert t['status']=='running'
    assert t['content_transfer']['uploaded_bytes']==len('shared-result')
    t=finish(engine,t,servers[0]);code_sha=t['input_manifest']['model.py']['sha256'];length=t['input_manifest']['model.py']['size'];sha_path(remote.project(t),code_sha).write_bytes(b'X'*length)
    next_task=create(engine,p,servers[0]);assert next_task['content_transfer']['uploaded_bytes']==length


def test_local_content_corruption_is_not_silently_used_as_dependency(engine,setup_project):
    enable(engine);p,servers=setup_project;a=finish(engine,create(engine,p,servers[0]),servers[0]);pool=Content(engine,p['id'])
    pool.path(a['result_manifest']['output.txt']['sha256']).write_text('X'*len('shared-result'))
    b=create(engine,p,servers[1],depends_on=[a['id']]);assert b['status']=='waiting' and '修改或删除' in b['reason']


def test_private_code_modification_does_not_change_content_snapshot(engine,setup_project):
    enable(engine);p,servers=setup_project;t=create(engine,p,servers[0]);pool=Content(engine,p['id']);original=t['input_manifest']['model.py']['sha256']
    path=engine.remote(servers[0]).project(t)/'runs'/t['attempt_id']/'workspace';(path/'model.py').write_text('CHANGED=1')
    t=finish(engine,t,servers[0]);assert 'model.py' in t['result_manifest']
    assert pool.path(original).read_text()=='VERSION=1\n'


def test_legacy_compaction_preserves_all_files_and_resumes_after_interruption(engine,setup_project,monkeypatch):
    p,servers=setup_project;t=create(engine,p,servers[0]);t=finish(engine,t,servers[0]);pool=Content(engine,p['id']);run=pool.project/'runs'/t['attempt_id']
    before={p.relative_to(run).as_posix():p.read_bytes() for scope in ('input','result') for p in (run/scope).rglob('*') if p.is_file()}
    original=pool.save_catalog;calls=[]
    def stop(attempt,**value):
        result=original(attempt,**value);calls.append(True)
        if len(calls)==1:raise OperationCanceled('interrupted')
        return result
    monkeypatch.setattr(pool,'save_catalog',stop)
    with pytest.raises(OperationCanceled):pool.compact()
    monkeypatch.setattr(pool,'save_catalog',original);pool.compact();catalog=pool.read_catalog(t['attempt_id'])
    for path,data in before.items():
        scope,rel=path.split('/',1);item=catalog['inputs' if scope=='input' else 'artifacts'][rel]
        assert pool.path(item['sha256']).read_bytes()==data
    assert not (run/'input').exists() and not (run/'result').exists()
    assert pool.artifact(t['attempt_id'],'output.txt').read_text()=='shared-result'


def test_new_artifact_download_and_input_plan_api(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];enable(e)
    p=e.create_project({'name':'download'});s=e.save_server({'name':'fake','host':'fake','username':'u','work_root':'/work/content','data_root':'/data/content'})
    t=finish(e,create(e,p,s),s);r=c.get('/api/tasks/'+t['id']+'/artifacts?path=output.txt');assert r.status_code==200 and r.data==b'shared-result'
    files=c.get('/api/tasks/'+t['id']+'/artifacts').json['files'];assert list(files)==['output.txt']
    follower=e.create_task(p['id'],{'name':'plan','command':'echo hi','depends_on':[t['id']]})
    plan=e.report_input_plan(follower['id']);assert plan['servers'][0]['upload_bytes']==0
    assert c.get('/api/tasks/'+follower['id']+'/input-plan').status_code==200
    assert c.get('/api/projects/'+p['id']+'/storage').status_code==200
    assert c.post('/api/projects/'+p['id']+'/storage/refresh',json={},headers=h).status_code==202
    assert c.post('/api/tasks/'+follower['id']+'/input-plan',json={},headers=h).status_code==202
    assert not list((e.local_project(p['id'])/'runs').glob('*/input'))


def test_bulk_retry_resets_descendants_first_and_protects_external_success(engine,setup_project):
    p,servers=setup_project;a=finish(engine,create(engine,p,servers[0]),servers[0]);b=finish(engine,create(engine,p,servers[1],depends_on=[a['id']]),servers[1])
    assert preview(engine,[a['id']],'retry')['excluded']
    engine.settings['paused']=True;result=apply(engine,[a['id'],b['id']],'retry');assert result['accepted_count']==2
    assert all(engine.store.get('tasks',key)['status']=='queued' for key in (a['id'],b['id']))
    assert result['results'][0]['id']==b['id']


def test_bulk_cancel_includes_drafts_and_waiting_nodes_without_touching_finished(engine,setup_project):
    p,servers=setup_project;a=finish(engine,create(engine,p,servers[0]),servers[0]);b=engine.create_task(p['id'],{'name':'draft','command':'echo hi'})
    result=apply(engine,[a['id'],b['id']],'cancel');assert result['accepted_count']==1 and len(result['excluded'])==1
    assert engine.store.get('tasks',a['id'])['status']=='success' and engine.store.get('tasks',b['id'])['status']=='canceled'


def test_bulk_file_policy_applies_to_each_nodes_own_dependencies(engine,setup_project):
    p,servers=setup_project;a=engine.create_task(p['id'],{'name':'a','command':'echo hi'});b=engine.create_task(p['id'],{'name':'b','command':'echo hi','depends_on':[a['id']]})
    update_policies(engine,[a['id'],b['id']],{'dependency_patterns':['output.txt'],'code_exclude':['cache/**']})
    assert engine.store.get('tasks',a['id'])['dependency_files']=={}
    assert engine.store.get('tasks',b['id'])['dependency_files']=={a['id']:['output.txt']}


def test_safe_bulk_restart_waits_for_exit_and_keeps_dispatch_paused(engine,setup_project):
    import time
    enable(engine);p,servers=setup_project;a=create(engine,p,servers[0]);b=engine.create_task(p['id'],{'name':'after','command':'echo after','depends_on':[a['id']]})
    engine.task_action(b['id'],'queue');engine.settings['paused']=True;engine.start()
    result=apply(engine,[a['id'],b['id']],'restart')
    assert result['accepted_count']==2 and engine.settings['paused']
    assert engine.store.get('tasks',a['id'])['previous_attempts'][-1]['attempt_id']==a['attempt_id']
    assert all(engine.store.get('tasks',key)['status']=='queued' for key in (a['id'],b['id']))


def test_cached_objects_are_not_rehashed_on_repeated_need(engine,setup_project,monkeypatch):
    from scheduler import object_agent
    enable(engine);p,servers=setup_project;t=finish(engine,create(engine,p,servers[0]),servers[0]);objects={v['sha256']:v for v in t['result_manifest'].values()}
    def no_hash(path):raise AssertionError('Unchanged cache entries must be checked by metadata only')
    monkeypatch.setattr(object_agent,'file_hash',no_hash)
    response=engine.remote(servers[0]).call('content_need',{'project_id':p['id'],'objects':objects});assert response['missing']==[]


def test_project_shell_normalization_override_keeps_both_code_and_execution_versions(engine,setup_project):
    from scheduler.task_imports import save_task
    enable(engine);p,servers=setup_project;code=engine.local_project(p['id'])/'code';(code/'run.sh').write_bytes(b'#!/bin/bash\r\necho hello\r\n')
    t=save_task(engine,p['id'],{'name':'shell-versions','script_path':'run.sh','script_scope':'project','server_ids':[servers[0]['id']]})
    engine.task_action(t['id'],'queue');engine.tick();t=engine.store.get('tasks',t['id']);assert t['status']=='running',t['reason']
    base=engine.remote(servers[0]).project(t)
    assert (base/'code'/'run.sh').read_bytes()==b'#!/bin/bash\r\necho hello\r\n'
    assert (base/'runs'/t['attempt_id']/'workspace'/'run.sh').read_bytes()==b'#!/bin/bash\necho hello\n'


def test_concurrent_identical_returns_download_one_blob(engine,setup_project,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    enable(engine);p,servers=setup_project;t=finish(engine,create(engine,p,servers[0]),servers[0]);pool=Content(engine,p['id']);sha=t['result_manifest']['output.txt']['sha256'];pool.path(sha).unlink()
    remote=engine.remote(servers[0]);original=remote.download_object;entered=threading.Event();release=threading.Event();calls=[]
    def slow(*args,**kwargs):calls.append(True);entered.set();assert release.wait(3);return original(*args,**kwargs)
    monkeypatch.setattr(remote,'download_object',slow)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a=executor.submit(pool.receive,remote,engine.spec(t),t['result_manifest']);assert entered.wait(1)
        b=executor.submit(Content(engine,p['id']).receive,remote,engine.spec(t),t['result_manifest']);release.set();results=[a.result(3),b.result(3)]
    assert len(calls)==1 and sum(r['downloaded_bytes'] for r in results)==len('shared-result')
    assert pool.path(sha).read_text()=='shared-result'


@pytest.mark.parametrize('field,value',[('code_files',['../escape']),('output_files','*.json'),('dependency_files',{'unknown':['**']})])
def test_file_policies_reject_unsafe_or_unknown_references(engine,setup_project,field,value):
    p,servers=setup_project
    with pytest.raises(ValueError):engine.create_task(p['id'],{'name':'unsafe','command':'echo hi',field:value})


def test_object_inputs_reuse_existing_content_and_are_not_returned_as_outputs(engine,setup_project):
    enable(engine);p,servers=setup_project;pool=Content(engine,p['id']);source=engine.root/'initial.pth';source.write_bytes(b'fixed-initial-weights')
    item=pool.ingest(source);refs={'inputs/initial/model.pth':item}
    a=create(engine,p,servers[0],object_files=refs,code_files=[])
    assert a['input_manifest']==refs and pool.read_catalog(a['attempt_id'])['read_only_files']==list(refs)
    a=finish(engine,a,servers[0]);assert 'inputs/initial/model.pth' not in a['result_manifest']
    b=create(engine,p,servers[0],object_files=refs,code_files=[])
    assert b['content_transfer']['uploaded_bytes']==0
    assert not list((pool.project/'tasks'/b['id']/'files').rglob('*.pth'))


@pytest.mark.parametrize('value',[
    {'../outside':{'sha256':'a'*64,'size':1}},
    {'deps/a':{'sha256':'a'*64,'size':1}},
    {'inputs/model':{'sha256':'wrong','size':1}},
    {'inputs/model':{'sha256':'a'*64,'size':True}},
    {'inputs':{'sha256':'a'*64,'size':1},'inputs/model':{'sha256':'a'*64,'size':1}},
    {'.':{'sha256':'a'*64,'size':1}},
])
def test_object_reference_validation(engine,setup_project,value):
    p,_=setup_project
    with pytest.raises(ValueError):engine.create_task(p['id'],{'name':'bad-object','command':'echo hi','object_files':value})


def test_missing_or_conflicting_object_reference_blocks_preparation(engine,setup_project):
    enable(engine);p,_=setup_project;pool=Content(engine,p['id'])
    t=engine.create_task(p['id'],{'name':'missing','command':'echo hi','object_files':{'inputs/a':{'sha256':'a'*64,'size':2}}})
    with pytest.raises(ValueError,match='缺失或损坏'):pool.build(t,save=False)
    code=pool.project/'code'/'model.py';item=pool.ingest(code)
    t=engine.create_task(p['id'],{'name':'collision','command':'echo hi','object_files':{'model.py':item}})
    with pytest.raises(ValueError,match='冲突'):pool.build(t,save=False)


def test_object_reference_survives_batch_policy_and_task_edit(client):
    from scheduler.task_imports import save_task,normalize
    c,app=client;engine=app.extensions['engine'];p=engine.create_project({'name':'Refs'});refs={'inputs/a':{'sha256':'a'*64,'size':2}}
    t=engine.create_task(p['id'],{'name':'input','command':'echo hi'})
    update_policies(engine,[t['id']],{'object_files':refs})
    edited=save_task(engine,p['id'],{'level':'low_interference'},task_id=t['id'])
    assert edited['object_files']==refs
    imported=normalize(engine,p['id'],[{'key':'ref','name':'Reference','command':'echo hi','object_files':refs}])
    assert imported[0]['object_files']==refs
