import json

from scheduler.storage import Store
from test_api import client,login


def large_replica():
    return {'id':'replica','project_id':'project','server_id':'server','checked_at':1,'exists':True,
            'files':{'code/a.py':{'sha256':'a'*64,'size':1}},
            'differences':[{'path':'code/source_'+str(i)+'.py','state':'missing'} for i in range(15000)]}


def test_polling_summaries_bound_large_lists_without_losing_complete_results(tmp_path):
    store=Store(tmp_path);replica=large_replica()
    job={'id':'big','label':'refresh','operation':'refresh_replica:[]','status':'success','result':replica}
    store.put('replicas',replica);store.put('jobs',job)
    compact=store.get_summary('replicas','replica');summary=store.get_summary('jobs','big')
    assert compact['difference_count']==15000 and len(compact['differences'])==3 and compact['file_count']==1
    assert summary['result']['difference_count']==15000 and len(summary['result']['differences'])==10
    assert len(json.dumps(summary))<32768
    assert store.get('jobs','big')==job and store.get('replicas','replica')==replica
    nested={'id':'nested','status':'success','result':{'results':[{'task':{'input_manifest':{'huge':'x'*10000},'result_manifest':{'huge':'x'*10000},'name':'result'},'error':'x'*10000} for _ in range(100)]}}
    store.put('jobs',nested);small=store.get_summary('jobs','nested')
    assert small['result']['result_count']==100 and len(json.dumps(small))<40000
    assert store.get('jobs','nested')==nested
    store.close()


def test_existing_cache_upgrade_is_one_time_and_does_not_rewrite_history(tmp_path,monkeypatch):
    store=Store(tmp_path);replica=large_replica();job={'id':'legacy','status':'success','result':replica}
    store.put('jobs',job);store.put('replicas',replica)
    original=list(store.db.execute("SELECT kind,id,data FROM records ORDER BY kind,id"))
    with store.db:
        store.db.execute("UPDATE record_summaries SET data=? WHERE kind='jobs'",(json.dumps(job),))
        store.db.execute("UPDATE record_summaries SET data=? WHERE kind='replicas'",(json.dumps(replica),))
        store.db.execute('DELETE FROM summary_versions')
    store.close();store=Store(tmp_path)
    assert store.get_summary('jobs','legacy')['result']['difference_count']==15000
    assert len(store.get_summary('replicas','replica')['differences'])==3
    assert list(store.db.execute("SELECT kind,id,data FROM records ORDER BY kind,id"))==original
    store.close()
    real=Store.summary
    def no_repeat(kind,value):
        assert kind not in ('jobs','replicas'),'A normal restart cannot rebuild the whole historical summary cache again'
        return real(kind,value)
    monkeypatch.setattr(Store,'summary',staticmethod(no_repeat));store=Store(tmp_path)
    assert store.get('jobs','legacy')==job;store.close()


def test_recent_history_keeps_old_active_jobs_without_loading_ended_history(tmp_path):
    store=Store(tmp_path)
    store.put('jobs',{'id':'old-active','status':'running','created_at':0})
    for i in range(100):store.put('jobs',{'id':str(i),'status':'success','created_at':i+1})
    recent=store.recent_summaries('jobs',5,('queued','running'))
    assert [j['id'] for j in recent]==['old-active','95','96','97','98','99']
    assert [j['id'] for j in store.active_summaries('jobs')]==['old-active']
    store.put('jobs',{'id':'old-active','status':'success'})
    assert store.active_summaries('jobs')==[]
    store.delete('jobs','99');assert len(store.recent_summaries('jobs',5))==5
    store.close()


def test_pending_upload_check_and_enqueue_do_not_scan_job_history(client,monkeypatch):
    _,app=client;e=app.extensions['engine'];p=e.create_project({'name':'performance'})
    old=e.store.summaries
    def summaries(kind):
        assert kind!='jobs','Pending checks may only read active operations'
        return old(kind)
    monkeypatch.setattr(e.store,'summaries',summaries)
    assert not e.uploads.pending(p['id'])
    e.store.put('jobs',{'id':'clear','status':'running','operation':'clear_public_files:'+json.dumps([p['id']])})
    assert e.uploads.pending(p['id']) and not e.uploads.pending(p['id'],exclude_job='clear')
    e.enqueue('test',lambda job=None:{'ok':True})


def test_main_state_is_small_while_job_details_still_contain_every_file(client):
    c,app=client;login(c,app);e=app.extensions['engine'];replica=large_replica()
    e.store.put('replicas',replica)
    e.store.put('jobs',{'id':'big','label':'refresh','operation':'refresh_replica:[]','status':'success','result':replica,'created_at':1})
    response=c.get('/api/state')
    assert response.status_code==200 and len(response.data)<100000
    assert response.json['replicas'][0]['difference_count']==15000
    detail=c.get('/api/jobs/big').json['job']['result']
    assert len(detail['differences'])==15000 and len(detail['files'])==1
