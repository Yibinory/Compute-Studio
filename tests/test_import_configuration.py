import copy

import pytest

from scheduler.policy import dependencies_ready, dependency_reason
from test_api import client, login
from test_task_imports import folder, preview, commit
from test_transfers import wait_job


def test_import_overrides_repair_foreign_server_settings_before_validation(client,tmp_path):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'override'})
    s=e.save_server({'name':'real','host':'fake','username':'u','work_root':'/work','data_root':'/data'})
    source=folder(tmp_path,{'defaults':{'server_names':['foreign'],'server_conda_envs':{'foreign-id':'/old/env'}},'experiments':[{'key':'a','script':'train.sh'},{'key':'b','script':'eval.sh','depends_on':['a'],'memory_mb':2048,'notes':'preserve note'}]})
    original=(source/'experiments.json').read_bytes()
    changes={'server_ids':[s['id']],'conda_env':'','server_conda_envs':{},'level':'low_interference','priority':7}
    r=c.post('/api/projects/'+p['id']+'/batch-import/preview',json={'source_path':str(source),'changes':changes},headers=h)
    assert r.status_code==202;job=wait_job(e,r.json['id']);assert job['status']=='success',job
    plan=job['result'];assert not e.store.all('tasks')
    assert all(t['server_ids']==[s['id']] and not t['server_conda_envs'] and t['priority']==7 for t in plan['tasks'])
    assert plan['tasks'][1]['memory_mb']==2048
    assert commit(c,h,e,plan['id'])['status']=='success'
    tasks=e.store.all('tasks');assert tasks[1]['depends_on']==[tasks[0]['id']]
    assert tasks[1]['status']=='draft' and e.store.all('notes')[0]['body']=='preserve note'
    assert (source/'experiments.json').read_bytes()==original


def test_configure_preview_without_rehash_preserves_ids_and_can_restore_original(client,tmp_path,monkeypatch):
    import scheduler.task_imports as imports
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'revise'})
    source=folder(tmp_path,{'experiments':[{'key':'a','script':'train.sh','priority':12},{'key':'b','script':'eval.sh','priority':13,'depends_on':['a']}]})
    plan=preview(c,h,e,p,source);ids=[t['id'] for t in plan['tasks']]
    def no_file_work(*args,**kwargs):raise AssertionError('调整配置不可重新读取或 hash 文件')
    original_run=imports.run;monkeypatch.setattr(imports,'run',no_file_work)
    url='/api/batch-imports/'+plan['id']+'/configure'
    response=c.post(url,json={'changes':{'priority':3,'memory_mb':4096},'configuration_revision':0},headers=h)
    assert response.status_code==200,response.json;updated=response.json
    assert [t['id'] for t in updated['tasks']]==ids and all(t['priority']==3 for t in updated['tasks'])
    assert updated['tasks'][1]['depends_on']==[ids[0]] and not e.store.all('tasks')
    assert c.post(url,json={'changes':{'priority':6},'configuration_revision':0},headers=h).status_code==400
    response=c.post(url,json={'changes':{},'configuration_revision':1},headers=h)
    assert [t['priority'] for t in response.json['tasks']]==[12,13]
    monkeypatch.setattr(imports,'run',original_run)
    r=c.post('/api/batch-imports/'+plan['id']+'/commit',json={'configuration_revision':0},headers=h)
    assert wait_job(e,r.json['id'])['status']=='failed' and not e.store.all('tasks')
    r=c.post('/api/batch-imports/'+plan['id']+'/commit',json={'configuration_revision':2},headers=h)
    assert wait_job(e,r.json['id'])['status']=='success'
    assert c.post(url,json={'changes':{},'configuration_revision':2},headers=h).status_code==400


@pytest.mark.parametrize('changes',[{'command':'changed'},{'depends_on':[]},{'priority':True},{'priority':0},{'gpu_name':[]},{'server_ids':['missing']},{'datasets':'invalid'},{'server_conda_envs':[]},{'memory_mb':float('inf')}])
def test_invalid_import_override_preserves_preview(client,tmp_path,changes):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'invalid'})
    source=folder(tmp_path,{'experiments':[{'key':'a','script':'train.sh'}]});plan=preview(c,h,e,p,source);before=copy.deepcopy(e.store.get('batch_imports',plan['id']))
    r=c.post('/api/batch-imports/'+plan['id']+'/configure',json={'changes':changes,'configuration_revision':0},headers=h)
    assert r.status_code==400 and e.store.get('batch_imports',plan['id'])==before and not e.store.all('tasks')


def test_dependency_wait_identifies_calculated_but_unsynced_parent():
    p={'id':'p','seq':62,'name':'cache','status':'syncing','synced_at':None,'runtime_progress':{'percent':42.4}}
    task={'depends_on':['p']}
    assert not dependencies_ready(task,[p]);reason=dependency_reason(task,[p])
    assert '#62 cache' in reason and '计算已结束，回传中 42%' in reason
    p.update(status='success',synced_at=123)
    assert dependencies_ready(task,[p]) and dependency_reason(task,[p])==''
    p.update(status='sync_failed',synced_at=None)
    assert '回传失败，请重试回传' in dependency_reason(task,[p])
    assert '不存在' in dependency_reason(task,[])
