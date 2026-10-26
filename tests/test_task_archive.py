import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from scheduler.task_archive import apply,preview
from scheduler.batch_states import apply as batch_apply,preview as batch_preview
from scheduler.policy import ACTIVE,dependencies_ready
from scheduler.scheduling_edits import update_policies
from test_api import client,login


def task(engine,project,name,status='draft',**extra):
    value=engine.create_task(project['id'],{'name':name,'command':'echo test','depends_on':extra.pop('depends_on',[])})
    value.update(status=status,**extra);engine.store.put('tasks',value)
    return value


@pytest.mark.parametrize('status',['draft','paused','queued','waiting','blocked','success','failed','canceled'])
def test_archive_preserves_files_runs_notes_and_configuration_and_restore_never_requeues(engine,setup_project,status):
    p,_=setup_project;t=task(engine,p,'old',status,attempt_id='old-run' if status in ('failed','canceled','success') else None,result_manifest={'output':{'sha256':'a'*64,'size':4}},previous_attempts=[{'attempt_id':'older'}])
    path=engine.local_project(p['id'])/'tasks'/t['id']/'files'/'keep.txt';path.write_text('keep')
    before=copy.deepcopy(t);settings=copy.deepcopy(engine.settings)
    result=apply(engine,[t['id']],True);assert result['changed_count']==1
    archived=engine.store.get('tasks',t['id']);assert archived['archived'] and archived['archive_previous_status']==status
    assert archived['status']==('paused' if status in ('queued','waiting','blocked') else status)
    for key in ('command','result_manifest','previous_attempts','attempt_id','depends_on'):assert archived[key]==before[key]
    assert apply(engine,[t['id']],True)['changed_count']==0
    assert apply(engine,[t['id']],False)['changed_count']==1
    restored=engine.store.get('tasks',t['id']);assert not restored['archived']
    assert restored['status']==archived['status'] and path.read_text()=='keep' and engine.settings==settings


@pytest.mark.parametrize('status',sorted(ACTIVE))
def test_archive_refuses_live_returning_and_uncertain_experiments(engine,setup_project,status):
    p,_=setup_project;t=task(engine,p,'live',status,attempt_id='active')
    result=apply(engine,[t['id']],True)
    assert not result['changed_count'] and '请先中断' in result['excluded'][0]['reason']
    assert engine.store.get('tasks',t['id'])==t


def test_dependency_chain_is_checked_as_a_group_and_revalidated_after_preview(engine,setup_project):
    p,_=setup_project;a=task(engine,p,'old_failed','failed');b=task(engine,p,'old_child','blocked',depends_on=[a['id']]);c=task(engine,p,'old_leaf',depends_on=[b['id']])
    assert '后续' in preview(engine,[a['id']],True)['excluded'][0]['reason']
    assert len(preview(engine,[a['id'],b['id'],c['id']],True)['eligible'])==3
    c['status']='running';engine.store.put('tasks',c)
    result=apply(engine,[a['id'],b['id'],c['id']],True)
    assert not result['changed_count'] and len(result['excluded'])==3
    c['status']='draft';engine.store.put('tasks',c)
    assert apply(engine,[a['id'],b['id'],c['id']],True)['changed_count']==3
    assert not apply(engine,[c['id']],False)['changed_count']
    assert apply(engine,[a['id'],b['id'],c['id']],False)['changed_count']==3


def test_archived_successful_prerequisite_remains_usable_and_result_association_is_retained(engine,setup_project):
    from scheduler.result_tables import read,save
    p,_=setup_project;a=task(engine,p,'success','success',attempt_id='success-run',synced_at=1)
    b=task(engine,p,'downstream','running',depends_on=[a['id']]);doc=read(engine,p['id'])
    doc['rows']=[{'id':'r','group':'result','seed':'42','task_id':a['id'],'attempt_id':'success-run','values':{'metric_1':.9}}]
    saved=save(engine,p['id'],doc)
    assert apply(engine,[a['id']],True)['changed_count']==1
    assert dependencies_ready(b,engine.store.all('tasks'))
    assert read(engine,p['id'])==saved and engine.store.get('tasks',b['id'])==b


def test_stale_queue_retry_policy_and_edit_requests_cannot_reactivate_archived_task(engine,setup_project):
    p,_=setup_project;t=task(engine,p,'retired')
    apply(engine,[t['id']],True);before=engine.store.get('tasks',t['id'])
    for operation in ('queue','retry','resume','cancel','pause'):
        with pytest.raises(ValueError,match='已归档'):engine.task_action(t['id'],operation)
    for action in ('queue','retry','restart','cancel','pause','resume'):
        assert '已归档' in batch_preview(engine,[t['id']],action)['excluded'][0]['reason']
        assert batch_apply(engine,[t['id']],action)['accepted_count']==0
    with pytest.raises(ValueError):update_policies(engine,[t['id']],{'priority':1})
    with pytest.raises(ValueError,match='已归档'):engine.update_task(t['id'],{'command':'echo changed'})
    with pytest.raises(ValueError,match='已归档'):engine.reserve_task(before,{'id':'fake'},{'index':0})
    assert engine.store.get('tasks',t['id'])==before


def test_archive_metadata_operation_does_not_wait_for_engine_or_remote_work(engine,setup_project,monkeypatch):
    p,_=setup_project;t=task(engine,p,'fast','failed')
    monkeypatch.setattr(engine,'remote',lambda *_:pytest.fail('Archive cannot contact SSH'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine.lock:result=pool.submit(apply,engine,[t['id']],True).result(timeout=2)
    assert result['changed_count']==1


def test_new_import_or_task_cannot_attach_to_retired_failed_prerequisite(engine,setup_project):
    from scheduler.task_imports import normalize
    p,_=setup_project;old=task(engine,p,'retired_failure','failed');apply(engine,[old['id']],True)
    with pytest.raises(ValueError,match='已归档且未完成'):
        engine.create_task(p['id'],{'name':'new','command':'true','depends_on':[old['id']]})
    with pytest.raises(ValueError,match='已归档且未完成'):
        normalize(engine,p['id'],[{'key':'new','name':'new','command':'true','depends_on':[old['id']]}])
    assert len(engine.store.all('tasks'))==1


def test_runtime_pending_and_project_storage_blocks_archive(engine,setup_project):
    p,_=setup_project;t=task(engine,p,'finishing','success',synced_at=1)
    engine.runtime_pending[t['id']]={'kind':'finish'}
    assert not apply(engine,[t['id']],True)['changed_count']
    engine.runtime_pending.clear();engine.project_storage_busy.add(p['id'])
    assert '整理存储' in preview(engine,[t['id']],True)['excluded'][0]['reason']


def test_scheduler_ignores_even_stale_archived_queue_state(engine,setup_project):
    p,_=setup_project;t=task(engine,p,'stale','queued',archived=True)
    engine.settings['paused']=False;engine.tick()
    assert engine.store.get('tasks',t['id'])==t


def test_api_preview_auth_apply_restore_and_edit_protection(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'archive'});t=task(e,p,'obsolete','failed')
    data={'task_ids':[t['id']],'archived':True}
    assert c.post('/api/tasks/archive',json=data).status_code==403
    response=c.post('/api/tasks/archive/preview',json=data,headers=h);assert response.status_code==200 and len(response.json['eligible'])==1
    assert not e.store.get('tasks',t['id']).get('archived')
    assert c.post('/api/tasks/archive',json=data,headers=h).json['changed_count']==1
    assert c.get('/api/state').json['runtime_features']['task_archive']
    assert c.post('/api/tasks/'+t['id']+'/action',json={'action':'retry'},headers=h).status_code==400
    assert c.post('/api/tasks/'+t['id']+'/update',json={'name':'new','command':'true'},headers=h).status_code==400
    data['archived']=False;assert c.post('/api/tasks/archive',json=data,headers=h).json['changed_count']==1
    assert e.store.get('tasks',t['id'])['status']=='failed'
