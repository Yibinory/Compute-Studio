import copy
import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from werkzeug.exceptions import Conflict

from scheduler.result_tables import read,save,scopes,assign_dataset,export_csv,summarize
from test_api import client,login


def table(engine,pid,dataset='',metric='Dice',value=.9):
    doc=read(engine,pid,dataset)
    doc.update(request_id='request_'+str(len(dataset)),columns=[{'id':'score','name':metric,'type':'number','precision':3,'direction':'max','unit':''}],
               rows=[{'id':'row42','group':'same_method','seed':'42','values':{'score':value},'notes':'keep this'}])
    return doc


def test_datasets_isolate_metrics_records_revisions_and_seed_groups(engine,setup_project):
    p,_=setup_project;pid=p['id']
    original=save(engine,pid,table(engine,pid))
    a=save(engine,pid,table(engine,pid,'BraTS','Dice',.95),'BraTS')
    b=save(engine,pid,table(engine,pid,'Fundus','AUROC',.82),'Fundus')
    assert a['id']!=b['id']!=original['id'] and a['revision']==b['revision']==1
    assert read(engine,pid)==original and read(engine,pid,'Fundus')['columns'][0]['name']=='AUROC'
    assert summarize(a)[0]['values']['score']['mean']==.95 and summarize(b)[0]['values']['score']['mean']==.82
    a.update(request_id='a_new');a['rows'][0]['values']['score']=.99;save(engine,pid,a,'BraTS')
    assert read(engine,pid,'Fundus')==b and read(engine,pid)==original
    other=engine.create_project({'name':'other'})
    assert read(engine,other['id'],'BraTS')['rows']==[]


def test_scope_catalog_includes_saved_registered_and_configured_datasets_without_creating_tables(engine,setup_project):
    p,_=setup_project;pid=p['id']
    engine.store.put('datasets',{'id':'managed','name':'managed','manifest':{}})
    engine.create_task(pid,{'name':'test','command':'true','datasets':['external']})
    save(engine,pid,table(engine,pid,'custom'),'custom')
    assert set(scopes(engine,pid)['datasets'])=={'managed','external','custom'}
    assert scopes(engine,pid)['saved_datasets']==['custom']
    before=copy.deepcopy(engine.store.all('result_tables'));read(engine,pid,'managed')
    assert engine.store.all('result_tables')==before


def test_atomic_assignment_preserves_all_metrics_and_provenance_and_blocks_old_editor(engine,setup_project):
    p,_=setup_project;pid=p['id'];task=engine.create_task(pid,{'name':'run','command':'true'})
    task.update(status='success',attempt_id='run42');engine.store.put('tasks',task)
    data=table(engine,pid);data['rows'][0].update(task_id=task['id'],attempt_id='run42')
    original=save(engine,pid,data);request={'dataset':'BraTS','revision':original['revision'],'request_id':'assign_once'}
    tasks=engine.store.all('tasks');assigned=assign_dataset(engine,pid,request)
    assert assigned['rows']==original['rows'] and assigned['columns']==original['columns']
    assert assigned['dataset']=='BraTS' and read(engine,pid)['rows']==[]
    assert assign_dataset(engine,pid,request)==assigned
    original['request_id']='old_editor'
    with pytest.raises(Conflict):save(engine,pid,original)
    assert engine.store.all('tasks')==tasks


def test_assignment_conflict_never_changes_source_or_target(engine,setup_project):
    p,_=setup_project;pid=p['id'];original=save(engine,pid,table(engine,pid));target=save(engine,pid,table(engine,pid,'occupied'),'occupied')
    with pytest.raises(Conflict,match='已有结果表'):
        assign_dataset(engine,pid,{'dataset':'occupied','revision':1,'request_id':'test'})
    with pytest.raises(Conflict,match='原结果表'):
        assign_dataset(engine,pid,{'dataset':'new','revision':0,'request_id':'test'})
    assert read(engine,pid)==original and read(engine,pid,'occupied')==target
    assert 'new' not in scopes(engine,pid)['saved_datasets']


def test_parallel_datasets_save_independently_and_catalog_does_not_lose_names(engine,setup_project):
    p,_=setup_project;pid=p['id']
    docs={name:table(engine,pid,name) for name in ('dataset_a','dataset_b')}
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved=list(pool.map(lambda name:save(engine,pid,docs[name],name),docs))
    assert [d['revision'] for d in saved]==[1,1]
    assert set(scopes(engine,pid)['saved_datasets'])==set(docs)


def test_export_always_identifies_dataset_and_no_other_dataset_results(engine,setup_project):
    p,_=setup_project;doc=save(engine,p['id'],table(engine,p['id'],'BraTS'), 'BraTS')
    for summary in (False,True):
        rows=list(csv.reader(io.StringIO(export_csv(doc,summary).decode('utf-8-sig'))))
        assert rows[0][0]=='数据集' and rows[1][0]=='BraTS'
        assert 'Fundus' not in str(rows)


@pytest.mark.parametrize('name',[None,'bad\nname','a'*121,True])
def test_invalid_dataset_names_rejected_without_writes(engine,setup_project,name):
    p,_=setup_project
    with pytest.raises(ValueError):read(engine,p['id'],name)
    assert not engine.store.all('result_tables')


def test_api_scoped_reads_writes_export_and_assignment(client):
    c,app=client;headers=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'datasets'});pid=p['id'];url='/api/projects/'+pid+'/results'
    data=table(e,pid,'中文数据集','准确率')
    assert c.post(url,query_string={'dataset':'中文数据集'},json=data,headers=headers).status_code==200
    assert c.get(url).json['rows']==[]
    assert c.get(url,query_string={'dataset':'中文数据集'}).json['columns'][0]['name']=='准确率'
    assert c.get(url+'/datasets').json['saved_datasets']==['中文数据集']
    assert '中文数据集' in c.get(url+'/export',query_string={'dataset':'中文数据集','view':'summary'}).data.decode('utf-8-sig')
    assert c.post(url,json=data,headers=headers).status_code==400  # wrong table endpoint
    save(e,pid,table(e,pid))
    assignment={'dataset':'another','revision':1,'request_id':'api_assign'}
    assert c.post(url+'/assign-dataset',json=assignment).status_code==403
    response=c.post(url+'/assign-dataset',json=assignment,headers=headers)
    assert response.status_code==200 and response.json['dataset']=='another'
    assert c.get(url).json['rows']==[] and c.get(url,query_string={'dataset':'another'}).json['rows']
