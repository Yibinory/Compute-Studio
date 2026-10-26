import copy
import threading

import pytest

from scheduler.scheduling_edits import update_policies
from test_api import client, login


def tasks_for(engine, project, statuses=('draft', 'queued', 'waiting', 'blocked', 'paused')):
    tasks=[]
    for i,status in enumerate(statuses):
        task=engine.create_task(project['id'],{'name':'policy-'+str(i),'command':'bash scripts/train.sh',
               'depends_on':[tasks[0]['id']] if tasks else [],'datasets':['dataset'], 'conda_env':'research'})
        task.update(status=status,reason='retain queue reason',attempts=1,script_path='scripts/train.sh',
                    custom_input={'path':'input.bin'},previous_attempts=[{'attempt_id':'prior'}])
        engine.store.put('tasks',task);tasks.append(task)
    return tasks


def test_bulk_policy_retains_execution_inputs_and_status(engine,setup_project):
    project,servers=setup_project;tasks=tasks_for(engine,project)
    changes={'level':'low_interference','priority':7,'memory_mb':4096,'max_utilization':15.5,
             'gpu_name':' A100 ','server_ids':[servers[1]['id'],servers[1]['id']],'max_retries':5}
    result=update_policies(engine,[t['id'] for t in tasks]+[tasks[0]['id']],changes)
    assert result['count']==result['changed_count']==5
    expected={**changes,'gpu_name':'A100','server_ids':[servers[1]['id']]}
    for task in tasks:
        actual=engine.store.get('tasks',task['id'])
        assert actual=={**task,**expected}
    audit=engine.store.get('scheduling_edits',result['edit_id'])
    assert audit['before'][tasks[0]['id']]['priority']==tasks[0]['priority']
    assert audit['changes']==expected
    assert '批量修改' in engine.store.events()[0]['message']


def test_bulk_only_changes_checked_fields_and_can_clear_restrictions(engine,setup_project):
    project,servers=setup_project;tasks=tasks_for(engine,project,('draft','paused'))
    for i,t in enumerate(tasks):
        t.update(server_ids=[servers[i]['id']],gpu_name='RTX',priority=20+i,memory_mb=4096+i)
        engine.store.put('tasks',t)
    result=update_policies(engine,[t['id'] for t in tasks],{'server_ids':[],'gpu_name':''})
    for t in tasks:assert engine.store.get('tasks',t['id'])=={**t,'server_ids':[],'gpu_name':''}
    result=update_policies(engine,[t['id'] for t in tasks],{'server_ids':[]})
    assert result['changed_count']==0


@pytest.mark.parametrize('status',['staging','launching','running','suspended','canceling','syncing','sync_failed','lost','success','failed','canceled'])
def test_any_ineligible_status_aborts_whole_batch(engine,setup_project,status):
    tasks=tasks_for(engine,setup_project[0],('draft',status))
    with pytest.raises(ValueError,match='未修改任何'):update_policies(engine,[t['id'] for t in tasks],{'priority':1})
    assert engine.store.all('tasks')==tasks
    assert not engine.store.all('scheduling_edits')


@pytest.mark.parametrize('changes',[
    {},{'command':'rm -rf /'},{'priority':0},{'priority':1.1},{'priority':True},
    {'memory_mb':-1},{'max_utilization':101},{'max_utilization':'NaN'},
    {'max_retries':21},{'max_retries':None},{'level':'invalid'},
    {'server_ids':'all'},{'server_ids':[[]]},{'server_ids':['missing-server']},{'gpu_name':[]},
])
def test_invalid_policy_is_atomic(engine,setup_project,changes):
    tasks=tasks_for(engine,setup_project[0],('draft','queued'))
    with pytest.raises(ValueError):update_policies(engine,[t['id'] for t in tasks],changes)
    assert engine.store.all('tasks')==tasks
    assert not engine.store.all('scheduling_edits')


def test_deleted_task_and_dispatch_race_do_not_partially_update(engine,setup_project):
    tasks=tasks_for(engine,setup_project[0],('draft','queued'))
    with pytest.raises(ValueError):update_policies(engine,[tasks[0]['id'],'missing'],{'priority':2})
    errors=[];started=threading.Event()
    def edit():
        started.set()
        try:update_policies(engine,[t['id'] for t in tasks],{'priority':2})
        except ValueError as exc:errors.append(str(exc))
    with engine.store.lock:
        worker=threading.Thread(target=edit);worker.start();assert started.wait(2)
        tasks[1]['status']='staging';engine.store.put('tasks',tasks[1])
    worker.join(3);assert not worker.is_alive() and errors
    assert engine.store.all('tasks')==tasks


def test_database_failure_rolls_back_all_tasks_and_audit(engine,setup_project):
    import sqlite3
    tasks=tasks_for(engine,setup_project[0],('draft','queued'))
    engine.store.db.execute("CREATE TRIGGER reject_policy BEFORE UPDATE ON records WHEN NEW.id='"+tasks[1]['id']+"' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    engine.store.db.commit()
    with pytest.raises(sqlite3.IntegrityError):update_policies(engine,[t['id'] for t in tasks],{'priority':2})
    assert engine.store.all('tasks')==tasks
    assert not engine.store.all('scheduling_edits')


def test_policy_api_auth_csrf_and_update(client):
    c,app=client;engine=app.extensions['engine']
    assert c.post('/api/tasks/scheduling',json={}).status_code==401
    headers=login(c,app);project=engine.create_project({'name':'API_policy'})
    tasks=tasks_for(engine,project,('draft','paused'))
    payload={'task_ids':[t['id'] for t in tasks],'changes':{'priority':'3','level':'emergency'}}
    assert c.post('/api/tasks/scheduling',json=payload).status_code==403
    response=c.post('/api/tasks/scheduling',json=payload,headers=headers)
    assert response.status_code==200 and response.json['count']==2
    for t in tasks:assert engine.store.get('tasks',t['id'])=={**t,'priority':3,'level':'emergency'}
    before=copy.deepcopy(engine.store.all('tasks'))
    for invalid in ([],{}, {'task_ids':[],'changes':{'priority':1}}, {'task_ids':'wrong','changes':{'priority':1}}):
        assert c.post('/api/tasks/scheduling',json=invalid,headers=headers).status_code==400
    assert engine.store.all('tasks')==before
