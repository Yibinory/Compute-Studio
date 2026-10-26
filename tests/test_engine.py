import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from scheduler.engine import Engine
from scheduler.files import manifest
from scheduler.storage import uid
from conftest import FakeRemote


def task(engine,p,server=None,deps=None,name='experiment',**extra):
    t=engine.create_task(p['id'],{'name':name,'command':'python model.py','server_ids':[server['id']] if server else [],'depends_on':deps or [],**extra})
    engine.task_action(t['id'],'queue')
    return t


def test_cross_server_multiple_dependencies(engine,setup_project):
    p,servers=setup_project
    a=task(engine,p,servers[0],name='train-A')
    b=task(engine,p,servers[1],name='train-B')
    c=task(engine,p,servers[1],[a['id'],b['id']],name='evaluate')
    engine.tick()
    aa=engine.store.get('tasks',a['id']);bb=engine.store.get('tasks',b['id'])
    assert aa['status']==bb['status']=='running'
    assert engine.store.get('tasks',c['id'])['status']=='blocked'
    engine.remote(servers[0]).complete(aa,output='from-A')
    engine.tick()
    assert engine.store.get('tasks',a['id'])['status']=='success'
    assert engine.store.get('tasks',c['id'])['status']=='blocked'
    engine.remote(servers[1]).complete(bb,output='from-B')
    engine.tick()
    cc=engine.store.get('tasks',c['id'])
    assert cc['status']=='running'
    remote=engine.remote(servers[1]).project(cc)/'runs'/cc['attempt_id']/'workspace'/'deps'
    assert (remote/a['id']/'output.txt').read_text()=='from-A'
    assert (remote/b['id']/'output.txt').read_text()=='from-B'


def test_actual_python_code_and_dependency_execution(engine,setup_project):
    p,servers=setup_project
    FakeRemote.auto_execute=True
    code=engine.local_project(p['id'])/'code'
    examples=Path(__file__).parents[1]/'examples'
    shutil.copy2(examples/'train.py',code/'train.py')
    shutil.copy2(examples/'evaluate.py',code/'evaluate.py')
    a=task(engine,p,servers[0],name='training',command='"'+sys.executable+'" train.py')
    b=task(engine,p,servers[1],[a['id']],name='evaluation',command='"'+sys.executable+'" evaluate.py')
    engine.tick();engine.tick();engine.tick()
    aa=engine.store.get('tasks',a['id']);bb=engine.store.get('tasks',b['id'])
    assert aa['status']==bb['status']=='success'
    result=engine.local_project(p['id'])/'runs'/bb['attempt_id']/'result'
    assert json.loads((result/'evaluation.json').read_text())[a['id']]['metric']==0.91


def test_failed_sync_blocks_dependency_cleanup_and_retry(engine,setup_project):
    p,servers=setup_project
    a=task(engine,p,servers[0]);b=task(engine,p,servers[1],[a['id']],name='follow')
    engine.tick();aa=engine.store.get('tasks',a['id']);remote=engine.remote(servers[0])
    remote.complete(aa);remote.fail_download=True
    engine.tick()
    assert engine.store.get('tasks',a['id'])['status']=='sync_failed'
    assert engine.store.get('tasks',b['id'])['status']=='blocked'
    with pytest.raises(ValueError):engine.clean_project(p['id'])
    with pytest.raises(ValueError):engine.task_action(a['id'],'retry')
    remote.fail_download=False
    engine.task_action(a['id'],'sync')
    assert engine.store.get('tasks',a['id'])['status']=='success'


def test_user_deleted_remote_code_is_restored(engine,setup_project):
    p,servers=setup_project
    a=task(engine,p,servers[0]);engine.tick();aa=engine.store.get('tasks',a['id'])
    r=engine.remote(servers[0]);r.complete(aa);engine.tick()
    (r.project(aa)/'code'/'model.py').unlink()
    report=engine.refresh_replica(p['id'],servers[0]['id'])
    assert report['differences'][0]['state']=='missing_remote'
    task(engine,p,servers[0],name='new');engine.tick()
    assert (r.project(aa)/'code'/'model.py').is_file()


def test_modified_local_predecessor_is_not_silently_used(engine,setup_project):
    p,servers=setup_project
    a=task(engine,p,servers[0]);engine.tick();aa=engine.store.get('tasks',a['id'])
    engine.remote(servers[0]).complete(aa);engine.tick();aa=engine.store.get('tasks',a['id'])
    (engine.local_project(p['id'])/'runs'/aa['attempt_id']/'result'/'output.txt').write_text('manually changed')
    b=task(engine,p,servers[1],[a['id']],name='follow');engine.tick()
    bb=engine.store.get('tasks',b['id'])
    assert bb['status']=='waiting' and '被修改或删除' in bb['reason']


def test_missing_finished_workspace_never_success(engine,setup_project):
    p,servers=setup_project;a=task(engine,p,servers[0]);engine.tick();aa=engine.store.get('tasks',a['id'])
    r=engine.remote(servers[0]);r.complete(aa)
    shutil.rmtree(r.project(aa)/'runs'/aa['attempt_id']/'workspace')
    engine.tick()
    assert engine.store.get('tasks',a['id'])['status']=='sync_failed'


def test_cleanup_scope_and_ttl(engine,setup_project):
    p,servers=setup_project
    for s in servers:
        engine.remote(s).call('project',{'project_id':p['id']})
        engine.refresh_replica(p['id'],s['id'])
    engine.clean_project(p['id'],[servers[0]['id']])
    assert not engine.remote(servers[0]).project({'project_id':p['id']}).exists()
    assert engine.remote(servers[1]).project({'project_id':p['id']}).exists()
    key=p['id']+':'+servers[1]['id'];r=engine.store.get('replicas',key);r['last_used_at']=time.time()-8*86400;engine.store.put('replicas',r)
    s=engine.store.get('servers',servers[1]['id']);s['connected']=True;engine.store.put('servers',s)
    engine.auto_cleanup()
    assert not engine.remote(s).project({'project_id':p['id']}).exists()
    assert (engine.local_project(p['id'])/'code'/'model.py').is_file()


def test_running_project_blocks_single_server_cleanup(engine,setup_project):
    p,servers=setup_project;task(engine,p,servers[0]);engine.tick()
    with pytest.raises(ValueError,match='拒绝清理'):engine.clean_project(p['id'],[servers[1]['id']])


def test_oom_bounded_retry_preserves_artifacts(engine,setup_project):
    p,servers=setup_project;a=task(engine,p,servers[0],memory_mb=1000,max_retries=1);engine.tick()
    aa=engine.store.get('tasks',a['id']);first=aa['attempt_id'];engine.remote(servers[0]).complete(aa,1,log='CUDA out of memory');engine.tick()
    aa=engine.store.get('tasks',a['id'])
    assert aa['status']=='running' and aa['memory_mb']==2024 and aa['attempts']==2
    assert (engine.local_project(p['id'])/'runs'/first/'result'/'output.txt').exists()
    engine.remote(servers[0]).complete(aa,1,log='CUDA out of memory');engine.tick()
    assert engine.store.get('tasks',a['id'])['status']=='failed'


def test_restart_recovers_without_duplicate_launch(engine,setup_project):
    p,servers=setup_project;a=task(engine,p,servers[0]);engine.tick();count=len(FakeRemote.launches)
    engine.close()
    other=Engine(engine.root,remote_factory=FakeRemote)
    try:
        other.tick()
        assert len(FakeRemote.launches)==count
        assert other.store.get('tasks',a['id'])['status']=='running'
    finally:other.close()


def test_lost_is_never_automatically_retried(engine,setup_project):
    p,servers=setup_project;a=task(engine,p,servers[0]);engine.tick();aa=engine.store.get('tasks',a['id'])
    status=engine.remote(servers[0]).project(aa)/'runs'/aa['attempt_id']/'_control'/'status.json'
    status.write_text('{"state":"lost"}')
    count=len(FakeRemote.launches);engine.tick();engine.tick()
    assert len(FakeRemote.launches)==count
    assert engine.store.get('tasks',a['id'])['status']=='lost'
    with pytest.raises(ValueError):engine.clean_project(p['id'])


def test_shared_storage_upload_once_and_offline_retry(engine,setup_project,tmp_path):
    p,servers=setup_project
    shared=tmp_path/'shared';FakeRemote.roots={s['id']:shared for s in servers}
    ds={'id':uid(),'name':'imagenet','servers':{}}
    local=engine.root/'datasets'/ds['id']/'files';local.mkdir(parents=True);(local/'sample.txt').write_text('dataset')
    ds['manifest']=manifest(local);engine.store.put('datasets',ds)
    result=engine.upload_dataset(ds['id'])
    assert len(result['shared_groups'])==1
    assert all(v['status']=='success' for v in result['servers'].values())
    assert (shared/'imagenet'/'sample.txt').read_text()=='dataset'
    assert not list(shared.glob('.compute-share-*'))
    engine.remote(servers[1]).offline=True
    with pytest.raises(ValueError):engine.upload_dataset(ds['id'])
    result=engine.store.get('datasets',ds['id'])
    assert result['servers'][servers[1]['id']]['status']=='verification_pending'
    engine.remote(servers[1]).offline=False;engine.upload_dataset(ds['id'])
    assert engine.store.get('datasets',ds['id'])['servers'][servers[1]['id']]['status']=='success'


def test_dataset_different_contents_are_replaced_after_hash_verification(engine,setup_project):
    p,servers=setup_project;ds={'id':uid(),'name':'data','servers':{}}
    local=engine.root/'datasets'/ds['id']/'files';local.mkdir(parents=True);(local/'x').write_text('new')
    ds['manifest']=manifest(local);engine.store.put('datasets',ds)
    r=engine.remote(servers[0]);(r.data_base/'data').mkdir();(r.data_base/'data'/'x').write_text('original')
    engine.upload_dataset(ds['id'])
    assert (r.data_base/'data'/'x').read_text()=='new'


def test_task_edit_cycle_guard_and_running_lock(engine,setup_project):
    p,servers=setup_project
    a=task(engine,p,servers[0]);b=task(engine,p,servers[1],[a['id']],name='follow')
    with pytest.raises(ValueError,match='成环'):engine.update_task(a['id'],{'depends_on':[b['id']]})
    engine.update_task(a['id'],{'priority':3,'memory_mb':4096})
    assert engine.store.get('tasks',a['id'])['priority']==3
    engine.tick()
    with pytest.raises(ValueError,match='未执行'):engine.update_task(a['id'],{'command':'echo changed'})


def test_cleanup_archives_manual_remote_edits(engine,setup_project):
    p,servers=setup_project;r=engine.remote(servers[0]);base=Path(r.call('project',{'project_id':p['id']})['path'])
    (base/'manual.txt').write_text('retain this remote-only file')
    engine.clean_project(p['id'],[servers[0]['id']])
    archived=engine.local_project(p['id'])/'remote-archives'/servers[0]['id']/'snapshot'/'manual.txt'
    assert archived.read_text()=='retain this remote-only file'


def test_interrupted_dataset_upload_retry_removes_partial(engine,setup_project):
    p,servers=setup_project;ds={'id':uid(),'name':'partial','servers':{}}
    local=engine.root/'datasets'/ds['id']/'files';local.mkdir(parents=True);(local/'x').write_text('full contents')
    ds['manifest']=manifest(local);engine.store.put('datasets',ds)
    r=engine.remote(servers[0]);(r.data_base/'partial').mkdir()
    temporary='x.upload-'+ds['manifest']['x']['sha256']
    (r.data_base/'partial'/temporary).write_text('interrupted')
    engine.upload_dataset(ds['id'])
    assert manifest(r.data_base/'partial')==ds['manifest']


def test_remote_code_manual_change_is_backed_up_before_overwrite(engine,setup_project):
    p,servers=setup_project;a=task(engine,p,servers[0]);engine.tick()
    aa=engine.store.get('tasks',a['id']);remote=engine.remote(servers[0]);remote.complete(aa);engine.tick()
    (remote.project(aa)/'code'/'model.py').write_text('REMOTE_MANUAL_CHANGE=1\n')
    task(engine,p,servers[0],name='next');engine.tick()
    backup=engine.local_project(p['id'])/'remote-code-history'/servers[0]['id']/'snapshot'/'model.py'
    assert backup.read_text()=='REMOTE_MANUAL_CHANGE=1\n'
    assert (remote.project(aa)/'code'/'model.py').read_text()=='VERSION=1\n'
