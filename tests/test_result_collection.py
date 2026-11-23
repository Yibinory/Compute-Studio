import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from scheduler.content import Content
from scheduler.result_collection import merge
from scheduler.result_tables import read,save
from scheduler.storage import uid
from test_api import client,login
from test_studio_cli import cli


def finished(engine,pid,name):
    task=engine.create_task(pid,{'name':name,'command':'echo fixture','datasets':['ECG']})
    task.update(status='success',attempt_id=uid(),exit_code=0,synced_at=10)
    engine.store.put('tasks',task)
    return task


def observation(task,seed,score):
    return {'task_id':task['id'],'attempt_id':task['attempt_id'],'key':'test',
            'seed':seed,'group':'model/test','values':{'auc':score},'source':'outputs/metrics.json#test.auc'}


def test_cli_collects_returned_files_and_rereads_exactly_once(cli,client,tmp_path):
    browser,app=client;headers=login(browser,app);engine=app.extensions['engine']
    pid=engine.create_project({'name':'collect'})['id'];first=finished(engine,pid,'eval_42');second=finished(engine,pid,'eval_43')
    previous=uid()
    first['previous_attempts']=[{'attempt_id':previous,'exit_code':0,'synced_at':5},{'attempt_id':uid(),'exit_code':1,'synced_at':6}]
    engine.store.put('tasks',first)
    path='outputs/metrics.json';raw=json.dumps({'说明':'核验','test':{'auc':.75}},ensure_ascii=False).encode()
    pool=Content(engine,pid);source=tmp_path/'metrics.json';source.write_bytes(raw)
    item=pool.ingest(source);pool.save_catalog(first['attempt_id'],outputs={path:item})
    legacy=pool.project/'runs'/second['attempt_id']/'result'/path
    legacy.parent.mkdir(parents=True);legacy.write_text('{"test":{"auc":0.95}}',encoding='utf8')
    (legacy.parents[2]/'run.log').write_text('test AUC=0.95',encoding='utf8')
    def run(cmd,*args):return cli(cmd,'--project',pid,*args)
    assert run('result-runs','--limit',1)['total']==3
    assert not run('result-runs','--limit',1)['runs'][0]['current']
    assert run('result-files','--attempt',first['attempt_id'],'--query','metrics')['files'][0]['path']==path
    def evidence(t,*args):return run('result-evidence','--task',t['id'],'--attempt',t['attempt_id'],*args)
    head=evidence(first,'--path',path,'--limit',5)
    tail=evidence(first,'--path',path,'--offset',head['next_offset'])
    a=json.loads(head['text']+tail['text'])['test']['auc']
    b=json.loads(evidence(second,'--path',path)['text'])['test']['auc']
    assert evidence(second,'--log')['log_tail_snapshot']
    payload={'name':'基线表','columns':[{'id':'auc','name':'AUROC'}],'rows':[observation(first,'42',a),observation(second,'43',b)]}
    file=tmp_path/'observations.json';file.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8-sig')
    rec=run('result-merge','--dataset','ECG',file)
    assert rec['created']==2 and rec['revision']==1
    assert run('result-merge','--dataset','ECG',file)=={**rec,'created':0,'unchanged':2}
    assert run('result-tables')['tables_by_dataset']['ECG'][-1]['table_id']==rec['table_id']
    scope=('--dataset','ECG','--table',rec['table_id'])
    doc=run('result-show',*scope,'--row-id',rec['row_ids'][0])
    assert doc['rows'][0]['sources']['auc']==payload['rows'][0]['source'] and len(doc['rows'])==1
    chart=run('result-chart',*scope,'--mode','box','--metric','auc','--group','group:model/test')
    stat=chart['groups'][0]['values']['auc']
    assert stat['n']==2 and stat['mean']==pytest.approx(.85) and stat['std']==pytest.approx(.2/2**.5)
    assert stat['q1']==pytest.approx(.8) and stat['q3']==pytest.approx(.9)
    url='/api/projects/'+pid+'/results/merge?dataset=ECG'
    assert browser.post(url,json=payload).status_code==403
    bad={**payload,'rows':[observation(first,'42',.5)]}
    assert browser.post(url,json=bad,headers=headers).status_code==409
    assert read(engine,pid,'ECG',rec['table_id'])['revision']==1
    alien=engine.create_project({'name':'another'})['id']
    assert browser.post(url.replace(pid,alien),json=payload,headers=headers).status_code==400
    bad_evidence=f'/api/projects/{alien}/results/runs/{first["id"]}/{first["attempt_id"]}/evidence'
    assert browser.get(bad_evidence,query_string={'path':path}).status_code==400
    evidence_url=bad_evidence.replace(alien,pid)
    assert browser.get(evidence_url,query_string={'path':'unlisted.json'}).status_code==400
    assert browser.get(evidence_url,query_string={'log':1}).status_code==400
    assert browser.get(evidence_url,query_string={'path':path,'offset':99999}).status_code==400
    binary=legacy.parent/'weights.bin';binary.write_bytes(b'\x00\x01')
    legacy_url=f'/api/projects/{pid}/results/runs/{second["id"]}/{second["attempt_id"]}/evidence'
    assert browser.get(legacy_url,query_string={'path':'outputs/weights.bin'}).status_code==400


def test_manual_provenance_sparse_merge_and_concurrency(engine,setup_project):
    project,_=setup_project;pid=project['id'];t=finished(engine,pid,'run')
    data={'name':'尝试','rows':[observation(t,'042','0.82')],'columns':[{'id':'auc','name':'AUROC'}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        writes=list(pool.map(lambda _:merge(engine,pid,'ECG',data),range(2)))
    assert sorted(r['created'] for r in writes)==[0,1]
    target=writes[0]['table_id']
    doc=read(engine,pid,'ECG',target);row=doc['rows'][0];row['notes']='人工备注'
    row['id']='manual'
    save(engine,pid,doc,'ECG',target)
    from werkzeug.exceptions import Conflict
    with pytest.raises(Conflict):merge(engine,pid,'ECG',data)
    data['rows'][0].update(row_id='manual',values={'loss':.1},source='summary.csv:2')
    data['columns']=[{'id':'loss','name':'Loss','direction':'min'}]
    assert merge(engine,pid,'ECG',data)['updated']==1
    doc=read(engine,pid,'ECG',target);row=doc['rows'][0]
    assert row['notes']=='人工备注' and row['values']=={'auc':.82,'loss':.1}
    row.pop('sources');row['values']['loss']=.2
    updated=save(engine,pid,doc,'ECG',target)['rows'][0]
    assert updated['sources']=={'auc':'outputs/metrics.json#test.auc'}


def test_invalid_batch_never_creates_table_and_chart_filter_keeps_best_across_pages(engine,setup_project):
    from scheduler.result_tables import chart_data,table_list
    project,_=setup_project;pid=project['id']
    t=finished(engine,pid,'evaluation')
    one=observation(t,'1',1)
    other={**one,'key':'other_set','group':'other','values':{'auc':0}}
    body={'name':'对照','columns':[{'id':'auc','name':'AUROC'}],'rows':[one,{**other,'source':''}]}
    before=table_list(engine,pid,'ECG')
    with pytest.raises(ValueError):merge(engine,pid,'ECG',body)
    assert table_list(engine,pid,'ECG')==before
    body['rows']=[one,other];receipt=merge(engine,pid,'ECG',body)
    table=read(engine,pid,'ECG',receipt['table_id'])
    stats=chart_data(table,['auc'],[],'point',1,1)['groups'][0]['values']['auc']
    assert stats=={'mean':0,'n':1,'std':None,'best':False}
    assert chart_data(table,[],[],'bar',0,1)['groups'][0]['values']['auc']['best']
    assert not chart_data(table,[],['group:model/test'],'line',0,1)['groups'][0]['values']['auc']['best']
