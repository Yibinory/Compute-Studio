import json
import shutil
from pathlib import Path

import pytest

from scheduler.content import Content
from scheduler.task_imports import normalize
from scheduler.scheduling_edits import update_policies
from test_api import client,login


def source(engine):
    folder=engine.root.with_name(engine.root.name+'-editor');folder.mkdir()
    (folder/'train.py').write_text('VERSION=1\n')
    engine.code_sources.settle_seconds=0
    return folder


def bind(engine,project,folder):
    engine.code_sources.configure(project['id'],{'path':str(folder),'exclude':[]})
    return engine.code_sources.synchronize(project['id'])


def test_incremental_publish_history_and_noop(engine,setup_project):
    p,_=setup_project;editor=source(engine);first=bind(engine,p,editor)
    assert first['added']==1 and first['deleted']==1
    prior_jobs=len(engine.store.all('jobs'))
    assert not engine.code_sources.synchronize(p['id'])['changed']
    assert len(engine.store.all('jobs'))==prior_jobs
    (editor/'train.py').write_text('VERSION=2\n');(editor/'new.txt').write_text('new')
    result=engine.code_sources.synchronize(p['id']);assert result['modified']==1 and result['added']==1
    (editor/'train.py').unlink();last=engine.code_sources.synchronize(p['id']);assert last['deleted']==1
    mirror=engine.local_project(p['id'])/'code';assert not (mirror/'train.py').exists() and (mirror/'new.txt').read_text()=='new'
    historical=engine.code_sources.read_version(p['id'],{'manifest':'versions/'+first['revision']+'.json'})
    item=historical['files']['train.py'];assert Content(engine,p['id']).path(item['sha256']).read_text()=='VERSION=1\n'
    assert all(j['result']['record_id'] for j in engine.store.all('jobs'))


@pytest.mark.parametrize('failure',['empty','missing','lock','conflict'])
def test_unavailable_source_never_erases_last_version(engine,setup_project,failure):
    p,_=setup_project;editor=source(engine);bind(engine,p,editor);mirror=engine.local_project(p['id'])/'code/train.py'
    if failure=='empty':(editor/'train.py').unlink()
    elif failure=='missing':editor.rename(editor.with_name(editor.name+'-gone'))
    elif failure=='lock':(editor/'.compute-sync.lock').touch()
    else:mirror.write_text('manual mirror edit')
    with pytest.raises(ValueError):engine.code_sources.synchronize(p['id'])
    assert mirror.read_text()==('manual mirror edit' if failure=='conflict' else 'VERSION=1\n')
    assert engine.code_sources.get(p['id'])['status']=='error'


def test_editor_writes_during_capture_do_not_publish(engine,setup_project,monkeypatch):
    p,_=setup_project;editor=source(engine);bind(engine,p,editor);original=engine.code_sources._worker
    (editor/'train.py').write_text('VERSION=2\n')
    def changing(*args,**kwargs):
        if not kwargs.get('scan_only'):(editor/'train.py').write_text('VERSION=3\n')
        return original(*args,**kwargs)
    monkeypatch.setattr(engine.code_sources,'_worker',changing)
    with pytest.raises(ValueError,match='变化|稳定'):engine.code_sources.synchronize(p['id'])
    assert (engine.local_project(p['id'])/'code/train.py').read_text()=='VERSION=1\n'


def test_failed_mirror_publish_rolls_back(engine,setup_project,monkeypatch):
    p,_=setup_project;editor=source(engine);first=bind(engine,p,editor)
    (editor/'train.py').write_text('VERSION=2\n');(editor/'another.py').write_text('new')
    original=engine.code_sources.copy_object;calls=[0]
    def fail_once(*args):
        calls[0]+=1
        if calls[0]==2:raise OSError('disk failure')
        return original(*args)
    monkeypatch.setattr(engine.code_sources,'copy_object',fail_once)
    with pytest.raises(OSError,match='disk failure'):engine.code_sources.synchronize(p['id'])
    mirror=engine.local_project(p['id'])/'code';assert (mirror/'train.py').read_text()=='VERSION=1\n'
    assert not (mirror/'another.py').exists()
    assert engine.code_sources.get(p['id'])['revision']==first['revision']
    assert engine.code_sources.synchronize(p['id'])['modified']==1


def test_follow_rules_and_running_workspace_are_frozen(engine,setup_project):
    p,servers=setup_project;editor=source(engine);bind(engine,p,editor)
    engine.settings['content_store_enabled']=True
    task=engine.create_task(p['id'],{'name':'follow','command':'python train.py','server_ids':[servers[0]['id']],
        'code_files':[],'follow_project_files':['train.py']})
    inputs=engine.local_project(p['id'])/'tasks'/task['id']/'files';(inputs/'train.py').write_text('OLD_TASK=1\n');(inputs/'seed.json').write_text('{"seed":42}')
    engine.task_action(task['id'],'queue');engine.tick();running=engine.store.get('tasks',task['id']);assert running['status']=='running',running
    workspace=engine.remote(servers[0]).project(running)/'runs'/running['attempt_id']/'workspace'
    assert (workspace/'train.py').read_text()=='VERSION=1\n'
    previous=running['input_revision'];(editor/'train.py').write_text('VERSION=2\n');engine.code_sources.synchronize(p['id'])
    assert (workspace/'train.py').read_text()=='VERSION=1\n'
    assert engine.store.get('tasks',task['id'])['input_revision']==previous
    engine.remote(servers[0]).complete(running);engine.tick()
    engine.task_action(task['id'],'retry');engine.tick();next_run=engine.store.get('tasks',task['id'])
    updated=engine.remote(servers[0]).project(next_run)/'runs'/next_run['attempt_id']/'workspace'
    assert (updated/'train.py').read_text()=='VERSION=2\n' and (updated/'seed.json').read_text()=='{"seed":42}'
    assert next_run['input_revision']!=previous


def test_task_snapshot_stays_default_and_unmatched_follow_blocks(engine,setup_project):
    p,_=setup_project;editor=source(engine);bind(engine,p,editor);pool=Content(engine,p['id'])
    task=engine.create_task(p['id'],{'name':'snapshot','command':'echo hi'})
    own=engine.local_project(p['id'])/'tasks'/task['id']/'files/train.py';own.write_text('TASK=1')
    inventory,_=pool.build(task,save=False);assert pool.path(inventory['train.py']['sha256']).read_text()=='TASK=1'
    task['follow_project_files']=['missing.py']
    with pytest.raises(ValueError,match='没有匹配'):pool.build(task,save=False)


def test_oom_reuses_exact_original_inputs_but_manual_retry_refreshes(engine,setup_project):
    p,servers=setup_project;editor=source(engine);bind(engine,p,editor);engine.settings['content_store_enabled']=True
    task=engine.create_task(p['id'],{'name':'oom','command':'echo train','max_retries':1,'server_ids':[servers[0]['id']]})
    engine.task_action(task['id'],'queue');engine.tick();first=engine.store.get('tasks',task['id'])
    (editor/'train.py').write_text('VERSION=2\n');engine.code_sources.synchronize(p['id'])
    engine.remote(servers[0]).complete(first,exit_code=137);engine.tick();second=engine.store.get('tasks',task['id'])
    assert second['status']=='running',second
    assert second['attempt_id']!=first['attempt_id'] and second['input_revision']==first['input_revision']
    workspace=engine.remote(servers[0]).project(second)/'runs'/second['attempt_id']/'workspace'
    assert (workspace/'train.py').read_text()=='VERSION=1\n'


def test_unbound_managed_code_edits_refresh_inputs(engine,setup_project):
    p,_=setup_project;task=engine.create_task(p['id'],{'name':'managed','command':'echo hi'});pool=Content(engine,p['id'])
    before,_=pool.build(task,save=False);(engine.local_project(p['id'])/'code/model.py').write_text('VERSION=2')
    after,_=pool.build(task,save=False);assert before['model.py']['sha256']!=after['model.py']['sha256']


def test_source_api_blocks_mirror_writes_and_serves_history(client):
    c,app=client;headers=login(c,app);engine=app.extensions['engine'];p=engine.create_project({'name':'Sources'});editor=source(engine);first=bind(engine,p,editor)
    assert c.get('/api/projects/'+p['id']+'/source').get_json()['revision']==first['revision']
    state=c.get('/api/state').get_json();assert state['runtime_features']['code_sync'] and len(state['code_sources'])==1
    response=c.post('/api/projects/'+p['id']+'/file',json={'path':'train.py','text':'bad'},headers=headers);assert response.status_code==400
    with pytest.raises(ValueError):app.extensions['uploads'].scope(p['id'])
    history=c.get('/api/projects/'+p['id']+'/source/history').get_json();assert history['versions'][0]['id']==first['revision']
    detail=c.get('/api/projects/'+p['id']+'/source/history/'+first['revision']+'?limit=1').get_json();assert len(detail['changes'])==1
    assert c.get('/api/projects/'+p['id']+'/source/history/invalid').status_code==400
    engine.code_sources.configure(p['id'],{'path':''})
    assert (engine.local_project(p['id'])/'code/train.py').read_text()=='VERSION=1\n'


def test_batch_import_and_edit_follow_field(engine,setup_project):
    p,_=setup_project
    tasks=normalize(engine,p['id'],[{'key':'a','name':'a','command':'echo hi','follow_project_files':['*.py']}])
    assert tasks[0]['follow_project_files']==['*.py']
    task=engine.create_task(p['id'],{'name':'a','command':'echo hi'})
    update_policies(engine,[task['id']],{'follow_project_files':['src/**']})
    assert engine.store.get('tasks',task['id'])['follow_project_files']==['src/**']


def test_rejects_storage_recursion_and_preserves_source_sh(engine,setup_project):
    p,_=setup_project
    with pytest.raises(ValueError):engine.code_sources.configure(p['id'],{'path':str(engine.local_project(p['id'])/'code')})
    editor=source(engine);original=b'\xef\xbb\xbf#!/bin/bash\r\necho hi\r\n';(editor/'run.sh').write_bytes(original)
    bind(engine,p,editor)
    assert (editor/'run.sh').read_bytes()==original
    assert (engine.local_project(p['id'])/'code/run.sh').read_bytes()==b'#!/bin/bash\necho hi\n'


def test_result_return_and_source_sync_cannot_overwrite_each_other(engine,setup_project,monkeypatch):
    p,servers=setup_project;editor=source(engine);bind(engine,p,editor);engine.settings['content_store_enabled']=True
    task=engine.create_task(p['id'],{'name':'return_isolation','command':'echo hi','server_ids':[servers[0]['id']]})
    engine.task_action(task['id'],'queue');engine.tick();task=engine.store.get('tasks',task['id']);remote=engine.remote(servers[0])
    workspace=remote.project(task)/'runs'/task['attempt_id']/'workspace'
    # The experiment changes a same-name input. It is an output, never new source.
    (workspace/'train.py').write_text('EXPERIMENT_MODIFIED=1\n')
    remote.complete(task);original=remote.download_object;during=[False]
    def returning(*args,**kwargs):
        if not during[0]:
            during[0]=True;(editor/'train.py').write_text('EDITOR_VERSION=2\n');engine.code_sources.synchronize(p['id'])
        return original(*args,**kwargs)
    monkeypatch.setattr(remote,'download_object',returning)
    engine.tick();finished=engine.store.get('tasks',task['id']);assert finished['status']=='success',finished
    assert during[0] and (editor/'train.py').read_text()=='EDITOR_VERSION=2\n'
    assert (engine.local_project(p['id'])/'code/train.py').read_text()=='EDITOR_VERSION=2\n'
    returned=finished['result_manifest']['train.py'];pool=Content(engine,p['id'])
    assert pool.path(returned['sha256']).read_text()=='EXPERIMENT_MODIFIED=1\n'
    initial=pool.read_catalog(task['attempt_id'])['inputs']['train.py']
    assert pool.path(initial['sha256']).read_text()=='VERSION=1\n'


def test_return_destination_rejects_alias_to_source(engine,setup_project):
    p,_=setup_project;pool=Content(engine,p['id']);attempt='a'*32;run=pool.project/'runs'/attempt;run.mkdir(parents=True)
    try:(run/'control').symlink_to(pool.project/'code',target_is_directory=True)
    except OSError:pytest.skip('Windows symlinks require developer mode')
    with pytest.raises(ValueError,match='链接'):pool.return_destination(attempt,'control')


def test_recovery_after_commit_does_not_restore_old_code(engine,setup_project):
    p,_=setup_project;editor=source(engine);first=bind(engine,p,editor)
    (editor/'train.py').write_text('VERSION=2\n');second=engine.code_sources.synchronize(p['id'])
    old=engine.code_sources.read_version(p['id'],{'manifest':'versions/'+first['revision']+'.json'})
    folder=engine.code_sources.folder(p['id'])
    (folder/'pending.json').write_text(json.dumps({'before':old['files'],'touched':['train.py'],'target_revision':second['revision']}))
    engine.code_sources.synchronize(p['id'])
    assert (engine.local_project(p['id'])/'code/train.py').read_text()=='VERSION=2\n'
    assert not (folder/'pending.json').exists()
