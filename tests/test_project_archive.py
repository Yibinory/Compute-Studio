import copy

import pytest

from scheduler.storage import Store
from test_api import client,login


def test_archive_is_idempotent_persistent_and_preserves_tasks_files_and_settings(engine,setup_project):
    project,_=setup_project;t=engine.create_task(project['id'],{'name':'active','command':'echo test'})
    t.update(status='running',attempt_id='keep-attempt');engine.store.put('tasks',t)
    before_tasks=copy.deepcopy(engine.store.all('tasks'));settings=copy.deepcopy(engine.settings)
    code=engine.local_project(project['id'])/'code'/'model.py';original=code.read_bytes()
    result=engine.archive_project(project['id'],True)
    assert result['archived'] and result['archived_at']>0
    assert engine.archive_project(project['id'],True)==result
    events=engine.store.events();assert len([v for v in events if v['message'].startswith('归档项目：')])==1
    other=Store(engine.root)
    try:assert other.get('projects',project['id'])==result
    finally:other.close()
    restored=engine.archive_project(project['id'],False)
    assert restored['archived'] is False and 'archived_at' not in restored
    assert engine.store.all('tasks')==before_tasks and engine.settings==settings and code.read_bytes()==original


@pytest.mark.parametrize('value',[None,'false','true',0,1,[],{}])
def test_invalid_archive_flag_never_changes_project(engine,setup_project,value):
    project,_=setup_project
    with pytest.raises(ValueError):engine.archive_project(project['id'],value)
    assert engine.store.get('projects',project['id'])==project


def test_archive_api_keeps_archived_records_available_for_explicit_display(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'archivable'})
    url='/api/projects/'+p['id']+'/archive'
    assert c.post(url,json={'archived':True}).status_code==403
    assert c.post(url,json={'archived':True},headers=h).json['archived']
    state=c.get('/api/state').json;assert state['runtime_features']['project_archive']
    assert state['projects'][0]['id']==p['id'] and state['projects'][0]['archived']
    assert c.post(url,json={'archived':'false'},headers=h).status_code==400
    assert c.post(url,json=['bad'],headers=h).status_code==400
    assert c.post('/api/projects/missing/archive',json={'archived':True},headers=h).status_code==400
    assert c.post(url,json={'archived':False},headers=h).json['archived'] is False
