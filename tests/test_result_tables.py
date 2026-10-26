import copy
import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from werkzeug.exceptions import Conflict

from scheduler.result_tables import read,save,summarize,export_csv
from test_api import client,login


def payload(engine,p):
    table=read(engine,p['id']);table.update(request_id='first',rows=[{'id':'r'+str(i),'group':'model/test','seed':str(seed),'task_id':None,'attempt_id':None,'values':{'metric_1':value},'notes':'note'} for i,(seed,value) in enumerate(((42,.8),(43,.9),(44,1.0)))])
    return table


def test_read_is_lazy_save_persists_and_seed_summary_uses_sample_sd(engine,setup_project):
    p,_=setup_project;table=payload(engine,p)
    assert not engine.store.all('result_tables')
    tasks=copy.deepcopy(engine.store.all('tasks'));settings=copy.deepcopy(engine.settings)
    value=save(engine,p['id'],table);stats=summarize(value)[0]
    assert value['revision']==1 and stats['values']['metric_1']['mean']==pytest.approx(.9)
    assert stats['values']['metric_1']['std']==pytest.approx(.1) and stats['values']['metric_1']['n']==3
    assert stats['values']['metric_2']=={'mean':None,'std':None,'n':0}
    assert read(engine,p['id'])==value and engine.store.all('tasks')==tasks and engine.settings==settings


def test_missing_values_single_seed_text_and_ungrouped_rows_are_distinct(engine,setup_project):
    p,_=setup_project;data=payload(engine,p);data['columns'].append({'id':'text','name':'设置','type':'text','direction':'none','precision':0,'unit':''})
    data['rows'][0]['values']['text']='frozen';data['rows'][1]['values']={'text':'frozen'};data['rows'][2].update(group='',seed='')
    value=save(engine,p['id'],data);groups=summarize(value)
    assert len(groups)==2 and groups[0]['values']['metric_1']=={'mean':.8,'std':None,'n':1}
    assert groups[0]['values']['text']['text']==['frozen'] and groups[0]['count']==2
    assert groups[1]['count']==1 and groups[1]['values']['metric_1']['std'] is None


def test_experiment_provenance_project_isolation_and_rerun_never_overwrite_results(engine,setup_project):
    p,_=setup_project;t=engine.create_task(p['id'],{'name':'seed_run','command':'echo run'});t.update(status='success',attempt_id='old');engine.store.put('tasks',t)
    data=payload(engine,p)
    for row in data['rows']:row.update(task_id=t['id'],attempt_id='old')
    result=save(engine,p['id'],data);before=copy.deepcopy(result)
    t.update(attempt_id='new',previous_attempts=[{'attempt_id':'old'}]);engine.store.put('tasks',t)
    assert read(engine,p['id'])==before
    result.update(request_id='next');result['rows'][0]['notes']='updated';saved=save(engine,p['id'],result)
    assert saved['rows'][0]['attempt_id']=='old' and saved['rows'][0]['task_snapshot']['name']=='seed_run'
    other=engine.create_project({'name':'other'});assert not read(engine,other['id'])['rows']
    wrong=read(engine,other['id']);wrong['rows']=[{**data['rows'][0],'group':'other'}]
    with pytest.raises(ValueError,match='不属于'):save(engine,other['id'],wrong)
    wrong=copy.deepcopy(saved);wrong['rows'][0]['attempt_id']='foreign-run'
    with pytest.raises(ValueError,match='不属于'):save(engine,p['id'],wrong)
    assert read(engine,p['id'])==saved


def test_duplicate_seed_normalization_and_invalid_numeric_never_partially_save(engine,setup_project):
    p,_=setup_project;data=payload(engine,p);data['rows'][1]['seed']='042'
    with pytest.raises(ValueError,match='Seed 重复'):save(engine,p['id'],data)
    assert not engine.store.all('result_tables')
    data=payload(engine,p);data['rows'][0]['values']['metric_1']='NaN'
    with pytest.raises(ValueError,match='有限数值'):save(engine,p['id'],data)
    assert not engine.store.all('result_tables')


@pytest.mark.parametrize('change',[lambda d:d['columns'][0].update(id='bad"id'),lambda d:d['columns'][0].update(precision=True),lambda d:d['columns'][1].update(name='指标 1'),lambda d:d['rows'][0]['values'].update(unknown=1),lambda d:d['rows'][0]['values'].update(metric_1=True),lambda d:d.update(revision=True),lambda d:d.update(rows='bad')])
def test_invalid_schema_is_atomic(engine,setup_project,change):
    p,_=setup_project;data=payload(engine,p);change(data)
    with pytest.raises(ValueError):save(engine,p['id'],data)
    assert not engine.store.all('result_tables')


def test_idempotent_retry_and_concurrent_edit_conflict(engine,setup_project):
    p,_=setup_project;data=payload(engine,p);one=save(engine,p['id'],data)
    assert save(engine,p['id'],data)==one
    stale=copy.deepcopy(data);stale.update(request_id='different');stale['rows'][0]['notes']='stale'
    with pytest.raises(Conflict):save(engine,p['id'],stale)
    def update(label):
        candidate=copy.deepcopy(one);candidate['request_id']=label;candidate['rows'][0]['notes']=label
        try:return save(engine,p['id'],candidate)['revision']
        except Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(update,['a','b']))
    assert sorted(map(str,results))==['2','conflict']


def test_csv_exports_provenance_numeric_summary_and_neutralizes_formulas(engine,setup_project):
    p,_=setup_project;data=payload(engine,p);data['columns'][1].update(name='=formula',type='text');data['rows'][0]['values']['metric_2']='=1+1';data['rows'][0]['notes']=' @formula'
    value=save(engine,p['id'],data);raw=export_csv(value).decode('utf-8-sig');rows=list(csv.reader(io.StringIO(raw)))
    assert "'=formula" in rows[0] and "'=1+1" in rows[1] and "'@formula" in rows[1]
    summary=list(csv.reader(io.StringIO(export_csv(value,True).decode('utf-8-sig'))))
    assert '指标 1 样本标准差' in summary[0] and float(summary[1][summary[0].index('指标 1 均值')])==pytest.approx(.9)


def test_results_api_requires_auth_preserves_archive_and_does_not_poll_big_table(client):
    c,app=client;h=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'table'});url='/api/projects/'+p['id']+'/results'
    data=payload(e,p)
    assert c.post(url,json=data).status_code==403
    result=c.post(url,json=data,headers=h);assert result.status_code==200,result.json
    assert len(c.get(url).json['summary'])==1
    assert c.post(url,json={**data,'request_id':'stale'},headers=h).status_code==409
    assert c.get(url+'/export?view=summary').status_code==200
    assert c.get(url+'/export?view=bad').status_code==400
    e.archive_project(p['id'],True);assert c.get(url).json['rows']==result.json['rows']
    assert 'result_tables' not in c.get('/api/state').json
