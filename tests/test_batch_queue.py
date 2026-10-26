import copy

import pytest

from scheduler.batch_states import apply, preview
from scheduler.operations import OperationCanceled
from test_api import client, login
from test_transfers import wait_job


def draft(engine, project, name, **extra):
    return engine.create_task(project['id'], {'name':name,'command':'echo test',**extra})


def test_queue_dependency_chain_once_without_file_work_or_reset(engine, setup_project, monkeypatch):
    p, _=setup_project
    a=draft(engine,p,'parent');b=draft(engine,p,'child',depends_on=[a['id']])
    a.update(status='paused',reason='用户暂停排队',attempts=2,retry_snapshot_attempt='old-snapshot',previous_attempts=[{'attempt_id':'old-run'}])
    engine.store.put('tasks',a);engine.settings['paused']=True
    def no_remote(*args, **kwargs):raise AssertionError('入队不能等待 SSH 或文件传输')
    monkeypatch.setattr(engine,'remote',no_remote)
    before=copy.deepcopy(a)
    result=apply(engine,[b['id'],a['id'],b['id']],'queue')
    assert result['accepted_count']==2 and result['scheduler_paused']
    assert engine.settings['paused'] and engine.wake_event.is_set()
    for t in (a,b):assert engine.store.get('tasks',t['id'])['status']=='queued'
    before.update(status='queued',reason='')
    assert engine.store.get('tasks',a['id'])==before
    again=apply(engine,[a['id'],b['id']],'queue')
    assert again['accepted_count']==0 and len(again['excluded'])==2
    assert all('已经入队' in row['reason'] for row in again['excluded'])


def test_mixed_queue_skips_existing_runs_and_finished_results(engine, setup_project):
    p,_=setup_project;rows=[]
    for status in ('draft','paused','queued','waiting','blocked','staging','running','suspended','syncing','sync_failed','lost','success','failed','canceled'):
        t=draft(engine,p,'node_'+status);t['status']=status
        if status not in ('draft','paused','queued','waiting','blocked'):
            t.update(attempt_id='attempt-'+status,result_manifest={'result':{'sha256':'a'*64,'size':1}},previous_attempts=[{'attempt_id':'older'}])
        engine.store.put('tasks',t);rows.append(t)
    running_pause=draft(engine,p,'paused_with_attempt');running_pause.update(status='paused',attempt_id='protected');engine.store.put('tasks',running_pause);rows.append(running_pause)
    before={t['id']:copy.deepcopy(t) for t in rows}
    plan=preview(engine,[t['id'] for t in rows]+['missing'],'queue')
    assert {row['status'] for row in plan['eligible']}=={'draft','paused'}
    result=apply(engine,[t['id'] for t in rows]+['missing'],'queue')
    assert result['accepted_count']==2 and len(result['excluded'])==len(rows)-1
    for t in rows[2:]:assert engine.store.get('tasks',t['id'])==before[t['id']]


def test_queue_rechecks_preview_and_protects_downstream(engine, setup_project):
    p,_=setup_project;a=draft(engine,p,'parent');b=draft(engine,p,'child',depends_on=[a['id']])
    assert len(preview(engine,[a['id'],b['id']],'queue')['eligible'])==2
    b.update(status='running',attempt_id='now-running');engine.store.put('tasks',b)
    result=apply(engine,[a['id'],b['id']],'queue')
    assert result['accepted_count']==0
    assert engine.store.get('tasks',a['id'])['status']=='draft'
    assert engine.store.get('tasks',b['id'])==b
    engine.project_storage_busy.add(p['id'])
    assert all(row['reason']=='项目正在整理存储' for row in preview(engine,[a['id'],b['id']],'queue')['excluded'])


def test_queue_cancellation_before_commit_leaves_all_drafts(engine, setup_project, monkeypatch):
    p,_=setup_project;rows=[draft(engine,p,'node_'+str(i)) for i in range(3)];calls=[]
    def canceled(job):
        calls.append(True)
        if len(calls)==2:raise OperationCanceled('stop')
    monkeypatch.setattr(engine,'check_cancel',canceled)
    with pytest.raises(OperationCanceled):apply(engine,[t['id'] for t in rows],'queue')
    assert all(engine.store.get('tasks',t['id'])==t for t in rows)


def test_batch_queue_api_async_and_normal_dependency_dispatch(client):
    c,app=client;headers=login(c,app);engine=app.extensions['engine'];p=engine.create_project({'name':'batch'})
    engine.save_server({'name':'fake','host':'fake','username':'u','work_root':'/work/compute','data_root':'/data'})
    a=draft(engine,p,'first');b=draft(engine,p,'second',depends_on=[a['id']]);engine.settings['paused']=True
    body={'task_ids':[a['id'],b['id']],'action':'queue'}
    assert len(c.post('/api/tasks/batch-action/preview',json=body,headers=headers).json['eligible'])==2
    response=c.post('/api/tasks/batch-action',json=body,headers=headers)
    assert response.status_code==202
    result=wait_job(engine,response.json['id']);assert result['status']=='success',result
    assert result['result']['accepted_count']==2 and result['result']['scheduler_paused']
    engine.tick();assert engine.store.get('tasks',a['id'])['status']=='queued'
    engine.settings['paused']=False;engine.tick()
    assert engine.store.get('tasks',a['id'])['status']=='running'
    assert engine.store.get('tasks',b['id'])['status'] in ('blocked','queued')
    assert not engine.store.get('tasks',b['id']).get('attempt_id')
    assert c.post('/api/tasks/batch-action',json=body,headers=headers).status_code==400
