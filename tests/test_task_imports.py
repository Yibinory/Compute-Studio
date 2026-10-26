import io
import json
from pathlib import Path

import pytest

from test_api import client,login
from test_transfers import wait_job


def folder(tmp_path,document):
    source=tmp_path.with_name(tmp_path.name+'-experiments');source.mkdir()
    (source/'experiments.json').write_text(json.dumps(document),encoding='utf-8')
    (source/'train.sh').write_bytes(b'\xef\xbb\xbf#!/bin/bash\r\necho train\r\n')
    (source/'eval.sh').write_bytes(b'#!/bin/bash\necho eval\n')
    return source


def preview(c,h,e,p,source):
    r=c.post('/api/projects/'+p['id']+'/batch-import/preview',json={'source_path':str(source)},headers=h)
    assert r.status_code==202,r.get_json()
    job=wait_job(e,r.get_json()['id']);assert job['status']=='success',job
    assert c.get('/api/jobs/'+job['id']).get_json()['job']['result']['tasks']
    return job['result']


def commit(c,h,e,key):
    r=c.post('/api/batch-imports/'+key+'/commit',json={},headers=h)
    if r.status_code==200:return r.get_json()
    assert r.status_code==202,r.get_json()
    return wait_job(e,r.get_json()['id'])


def test_local_sh_create_and_update_preserve_normalized_task_script(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Shell'})
    config={'name':'ShellTask','level':'low_interference','memory_mb':1234}
    r=c.post('/api/projects/'+p['id']+'/tasks',data={'config':json.dumps(config),'script':(io.BytesIO(b'\xef\xbb\xbf#!/bin/bash\r\necho test\r\n'),'run test.sh')},headers=h)
    assert r.status_code==200,r.get_json();t=r.get_json()
    assert t['command']=="bash -- 'run test.sh'" and t['script_scope']=='task' and t['memory_mb']==1234
    dest=e.local_project(p['id'])/'tasks'/t['id']/'files'/'run test.sh'
    assert dest.read_bytes()==b'#!/bin/bash\necho test\n'
    r=c.post('/api/tasks/'+t['id']+'/update',json={'priority':12},headers=h)
    assert r.status_code==200 and r.get_json()['script_path']=='run test.sh' and dest.exists()
    r=c.post('/api/tasks/'+t['id']+'/update',data={'config':json.dumps({'name':'ShellTask'}),'script':(io.BytesIO(b'echo new\r\n'),'run test.sh')},headers=h)
    assert r.status_code==200 and dest.read_bytes()==b'echo new\n'


def test_project_script_list_and_selection_snapshot_no_public_overwrite(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Scripts'})
    code=e.local_project(p['id'])/'code';(code/'nested').mkdir();original=code/'nested'/'run.SH';original.write_bytes(b'echo hello\r\n');(code/'ignored.sh.bak').write_text('no')
    r=c.get('/api/projects/'+p['id']+'/scripts').get_json()
    assert [v['path'] for v in r['scripts']]==['nested/run.SH']
    r=c.post('/api/projects/'+p['id']+'/tasks',json={'name':'Selected','script_path':'nested/run.SH'},headers=h)
    assert r.status_code==200,r.get_json();t=r.get_json()
    assert t['command']=='bash -- nested/run.SH' and original.read_bytes()==b'echo hello\r\n'
    assert (e.local_project(p['id'])/'tasks'/t['id']/'files'/'nested'/'run.SH').read_bytes()==b'echo hello\n'


def test_batch_notes_exceed_old_character_limit_without_polling_full_body(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Large_Batch_Notes'})
    body='## Observation\n\n$E=mc^2$\n\n'*1000
    source=folder(tmp_path,{'experiments':[{'key':'train','name':'train','script':'train.sh','notes':body}]})
    plan=preview(c,h,e,p,source);job=commit(c,h,e,plan['id']);assert job['status']=='success'
    note=e.store.all('notes')[0];assert note['body']==body
    assert e.store.summaries('notes')[0]['large'] and 'body' not in e.store.summaries('notes')[0]
    assert c.get('/api/tasks/'+note['task_id']+'/notes/'+note['id']+'/text').data.decode()==body


@pytest.mark.parametrize('filename,content',[('a.py',b'echo bad'),('../bad.sh',b'bad'),('empty.sh',b''),('bad.sh',b'\x00'),('large.sh',b'x'*(2*1024**2+1))],ids=['extension','traversal','empty','binary','too-large'])
def test_bad_scripts_never_create_tasks(client,filename,content):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Invalid'})
    r=c.post('/api/projects/'+p['id']+'/tasks',data={'config':json.dumps({'name':'Bad'}),'script':(io.BytesIO(content),filename)},headers=h)
    assert r.status_code==400 and not e.store.all('tasks')


def test_batch_forward_dependencies_defaults_notes_and_idempotent_confirmation(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Batch'})
    source=folder(tmp_path,{'version':1,'defaults':{'priority':15,'level':'low_interference','memory_mb':2000},'experiments':[
        {'key':'eval','name':'Evaluation','script':'eval.sh','depends_on':['train'],'notes':'check scores'},
        {'key':'train','name':'Training','script':'./train.sh','args':['--name','a b']} ]})
    plan=preview(c,h,e,p,source);assert plan['count']==2 and not e.store.all('tasks')
    assert plan['tasks'][1]['command']=="bash -- train.sh --name 'a b'"
    value=commit(c,h,e,plan['id']);assert value['status']=='success',value
    tasks=e.store.all('tasks');eval_task,train=tasks
    assert eval_task['depends_on']==[train['id']] and all(t['status']=='draft' and t['priority']==15 for t in tasks)
    assert e.store.all('notes')[0]['body']=='check scores'
    path=e.local_project(p['id'])/'tasks'/train['id']/'files'/'train.sh'
    assert path.read_bytes()==b'#!/bin/bash\necho train\n' and b'\r\n' in (source/'train.sh').read_bytes()
    assert commit(c,h,e,plan['id'])['status']=='success' and len(e.store.all('tasks'))==2
    assert not (e.root/'batch-imports'/plan['id']).exists()


@pytest.mark.parametrize('rows',[
    [{'key':'a','script':'train.sh','depends_on':['missing']}],
    [{'key':'a','script':'train.sh','depends_on':['b']},{'key':'b','script':'eval.sh','depends_on':['a']}],
    [{'key':'a','script':'../outside.sh'}],
    [{'key':'a','script':'train.sh','priority':0}],
    [{'key':'a','script':'train.sh','typo':1}],
    [{'key':'a','script':'train.sh'},{'key':'a','script':'eval.sh'}],
])
def test_invalid_batch_is_rejected_without_partial_tasks(client,tmp_path,rows):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'InvalidBatch'});source=folder(tmp_path,{'experiments':rows})
    r=c.post('/api/projects/'+p['id']+'/batch-import/preview',json={'source_path':str(source)},headers=h)
    assert wait_job(e,r.get_json()['id'])['status']=='failed'
    assert not e.store.all('tasks') and not e.store.all('batch_imports') and not list((e.root/'batch-imports').iterdir())


def test_source_changes_and_commit_failure_leave_no_partial_tasks(client,tmp_path,monkeypatch):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Atomic'})
    source=folder(tmp_path,{'experiments':[{'key':'a','script':'train.sh'},{'key':'b','script':'eval.sh'}]})
    plan=preview(c,h,e,p,source);original=(source/'eval.sh').read_bytes();(source/'eval.sh').write_bytes(b'echo changed')
    assert commit(c,h,e,plan['id'])['status']=='failed' and not e.store.all('tasks')
    (source/'eval.sh').write_bytes(original);save=e.store.put_many
    def fail(records):raise OSError('fixture transaction failure')
    monkeypatch.setattr(e.store,'put_many',fail)
    assert commit(c,h,e,plan['id'])['status']=='failed' and not e.store.all('tasks')
    assert all(not (e.local_project(p['id'])/'tasks'/t['id']).exists() for t in plan['tasks'])
    monkeypatch.setattr(e.store,'put_many',save)
    assert commit(c,h,e,plan['id'])['status']=='success' and len(e.store.all('tasks'))==2


def test_recursive_experiment_configs_and_task_files(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Folders'})
    source=tmp_path.with_name(tmp_path.name+'-folders');source.mkdir()
    for key in ('train','eval'):
        path=source/key;path.mkdir();(path/'run.sh').write_bytes(b'echo test\n');(path/'params.json').write_text('{}')
        (path/'experiment.json').write_text(json.dumps({'key':key,'script':'run.sh','files':['params.json'],'depends_on':['train'] if key=='eval' else []}))
    plan=preview(c,h,e,p,source);assert plan['count']==2 and len(plan['configs'])==2
    assert commit(c,h,e,plan['id'])['status']=='success'
    for t in e.store.all('tasks'):assert (e.local_project(p['id'])/'tasks'/t['id']/'files'/'params.json').read_text()=='{}'


def test_concurrent_batch_confirmations_publish_only_once(client,tmp_path,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import scheduler.task_imports as imports
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'Concurrent'})
    source=folder(tmp_path,{'experiments':[{'key':'train','script':'train.sh'}]})
    plan=preview(c,h,e,p,source);entered=threading.Event();release=threading.Event();count=[];original=imports.run
    def delayed(root,action,**kwargs):
        if action=='batch-publish':
            count.append(action);entered.set()
            assert release.wait(10)
        return original(root,action,**kwargs)
    monkeypatch.setattr(imports,'run',delayed)
    batch=app.extensions['batch_imports']
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(batch.commit,plan['id']);assert entered.wait(10)
        second=pool.submit(batch.commit,plan['id']);release.set()
        assert first.result(timeout=15)['status']==second.result(timeout=15)['status']=='success'
    assert len(count)==1 and len(e.store.all('tasks'))==1
    task=e.store.all('tasks')[0]
    assert (e.local_project(p['id'])/'tasks'/task['id']/'files'/'train.sh').is_file()
