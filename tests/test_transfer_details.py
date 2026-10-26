import hashlib
import time

from conftest import FakeRemote
from test_api import client, login, archive
from scheduler.files import manifest
from scheduler.storage import uid


def wait(engine,job):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        value=engine.store.get('jobs',job['id'])
        if value['status'] not in ('queued','running'):return value
        time.sleep(.01)
    raise AssertionError('Job timed out')


def dataset(engine,name='journal'):
    value={'id':uid(),'name':name,'servers':{}}
    root=engine.root/'datasets'/value['id']/'files';root.mkdir(parents=True)
    for path,text in {'same.txt':'same','new.txt':'new','replace.txt':'replacement'}.items():(root/path).write_text(text)
    value['manifest']=manifest(root);engine.store.put('datasets',value)
    return value,root


def test_durable_per_server_skip_upload_replace_decisions(engine,setup_project):
    _,servers=setup_project;ds,local=dataset(engine)
    for s in servers:
        remote=engine.remote(s);target=remote.data_base/ds['name'];target.mkdir()
        (target/'same.txt').write_text('same');(target/'replace.txt').write_text('old')
    job=engine.enqueue('file decisions',engine.upload_dataset,ds['id']);assert wait(engine,job)['status']=='success'
    page=engine.transfers.page(job['id']);assert page['total_count']==6
    for s in servers:
        rows={f['path']:f for f in page['files'] if f['server_id']==s['id']}
        assert (rows['same.txt']['initial_state'],rows['same.txt']['action'],rows['same.txt']['status'])==('matching','skip','skipped')
        assert (rows['new.txt']['initial_state'],rows['new.txt']['action'])==('missing','upload')
        changed=rows['replace.txt'];assert (changed['initial_state'],changed['action'],changed['status'])==('mismatch','replace','success')
        assert changed['previous_sha256']==hashlib.sha256(b'old').hexdigest()
        assert changed['current_sha256']==hashlib.sha256(b'replacement').hexdigest()
        assert changed['bytes']==changed['total'] and changed['verified_at']
        assert [e['status'] for e in changed['events']]==['planned','uploading','verifying','success','verified']
        assert (engine.remote(s).data_base/ds['name']/'replace.txt').read_text()=='replacement'
    filtered=engine.transfers.page(job['id'],limit=1,query='REPLACE',server=servers[0]['id'],status='success')
    assert filtered['matched_count']==1 and filtered['files'][0]['path']=='replace.txt'
    # Records live in SQLite, not a transient map or giant /state payload.
    assert engine.store.db.execute('SELECT count(*) FROM transfer_files WHERE job_id=?',(job['id'],)).fetchone()[0]==6


def test_shared_group_records_one_actual_upload_with_peer_verification(engine,setup_project):
    _,servers=setup_project;ds,_=dataset(engine,'shared')
    shared=engine.root/'shared-datasets';shared.mkdir()
    for s in servers:FakeRemote.roots[s['id']]=shared
    calls=[]
    for s in servers:
        remote=engine.remote(s);original=remote.sftp.put
        def put(source,target,callback=None,original=original,sid=s['id']):calls.append(sid);return original(source,target,callback)
        remote.sftp.put=put
    job=engine.enqueue('shared decisions',engine.upload_dataset,ds['id']);assert wait(engine,job)['status']=='success'
    assert len(calls)==3 and len(set(calls))==1
    rows=engine.transfers.page(job['id'])['files'];assert len(rows)==6
    assert sum(f['shared'] for f in rows)==3 and all(f['current_state']=='matching' for f in rows)


def test_shared_peer_never_rehashes_and_timeout_remains_pending(engine,setup_project):
    import pytest
    _,servers=setup_project;ds,_=dataset(engine,'one-hash')
    shared=engine.root/'one-hash-shared';shared.mkdir()
    for s in servers:FakeRemote.roots[s['id']]=shared
    calls=[]
    for s in servers:
        r=engine.remote(s);original=r.call
        def call(action,payload=None,timeout=120,original=original,sid=s['id']):
            calls.append((sid,action,timeout))
            if sid==servers[1]['id'] and action=='dataset_manifest':raise AssertionError('Shared peer must not rehash')
            return original(action,payload,timeout)
        r.call=call
    engine.upload_dataset(ds['id'])
    assert len([c for c in calls if c[1]=='dataset_verify_snapshot'])==1
    assert len([c for c in calls if c[1]=='dataset_manifest'])==1
    assert all(c[2]>=300 for c in calls if c[1] in ('dataset_manifest','dataset_verify_snapshot','dataset_shared_verify'))
    assert not list((shared/ds['name']).glob('.compute-verify-*'))
    peer=engine.remote(servers[1]);original=peer.call
    def timeout(action,payload=None,timeout=120):
        if action=='dataset_shared_verify':raise TimeoutError('simulated slow connection')
        return original(action,payload,timeout)
    peer.call=timeout
    job=engine.enqueue('shared timeout',engine.upload_dataset,ds['id']);result=wait(engine,job)
    assert result['status']=='failed' and '数据已上传' in result['error']
    state=engine.store.get('datasets',ds['id'])['servers']
    assert state[servers[0]['id']]['status']=='success' and state[servers[1]['id']]['status']=='verification_pending'
    rows=engine.transfers.page(job['id'],server=servers[1]['id'])['files']
    assert all(f['status']=='verification_pending' and f['bytes']==f['total'] and 'verified_at' not in f for f in rows)


def test_changed_dataset_submount_is_not_inferred_from_cached_root(engine,setup_project):
    _,servers=setup_project;ds,_=dataset(engine,'submount')
    shared=engine.root/'cached-shared';shared.mkdir()
    for s in servers:FakeRemote.roots[s['id']]=shared
    engine.upload_dataset(ds['id'])
    peer=engine.remote(servers[1]);peer.data_base=engine.root/'new-mount';peer.data_base.mkdir()
    job=engine.enqueue('changed mount',engine.upload_dataset,ds['id']);assert wait(engine,job)['status']=='failed'
    state=engine.store.get('datasets',ds['id'])['servers']
    assert state[servers[0]['id']]['status']=='success'
    assert state[servers[1]['id']]['status']=='verification_pending'
    assert 'marker not visible' in state[servers[1]['id']]['error']


def test_verify_only_endpoint_never_uploads_or_overwrites(client):
    c,app=client;headers=login(c,app);e=app.extensions['engine']
    servers=[e.save_server({'name':'verify-'+str(i),'host':'fake-'+str(i),'username':'tester','work_root':'/work','data_root':'/data'}) for i in range(2)]
    shared=e.root/'verify-shared';shared.mkdir()
    for s in servers:FakeRemote.roots[s['id']]=shared
    ds,_=dataset(e,'verify-only');e.upload_dataset(ds['id'])
    before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in (shared/ds['name']).iterdir()}
    for s in servers:
        remote=e.remote(s);original=remote.call
        def call(action,payload=None,timeout=120,original=original):
            assert action not in ('dataset_prepare','dataset_commit','dataset_manifest')
            return original(action,payload,timeout)
        remote.call=call
        remote.upload=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('No uploads allowed'))
    response=c.post('/api/datasets/'+ds['id']+'/verify',json={},headers=headers)
    assert response.status_code==200
    job=response.get_json();assert job['cancelable'] and wait(e,job)['status']=='success'
    assert before=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in (shared/ds['name']).iterdir()}
    detail=c.get('/api/jobs/'+job['id']+'/transfer-details').get_json()
    assert len(detail['files'])==6 and all(f['action']=='verify' and f['status']=='success' for f in detail['files'])
    assert all(f['source_server_name']==servers[0]['name'] for f in detail['files'] if f.get('shared'))
    assert 'manifest' not in c.get('/api/state').get_json()['jobs'][-1]['result']


def test_interrupt_keeps_partial_file_progress_and_waiting_records(engine,setup_project):
    _,servers=setup_project;ds,_=dataset(engine,'interrupted');remote=engine.remote(servers[0])
    def put(source,target,callback=None):
        from pathlib import Path
        Path(target).write_bytes(b'n');callback(1,3)
        engine.cancel_job(holder['job']['id'])
        callback(2,3)
    holder={};release=__import__('threading').Event()
    remote.sftp.put=put
    def upload_dataset(dataset_id,job=None):release.wait(2);return engine.upload_dataset(dataset_id,job=job)
    job=engine.enqueue('interrupt record',upload_dataset,ds['id']);holder['job']=job;release.set()
    assert wait(engine,job)['status']=='canceled'
    rows=engine.transfers.page(job['id'])['files'];assert rows and all(f['status']=='interrupted' for f in rows)
    assert any(0<f['bytes']<f['total'] for f in rows)


def test_upload_detail_receipts_and_local_zip_inventory(client):
    c,app=client;headers=login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'details'})
    code=e.local_project(p['id'])/'code';(code/'same.py').write_text('same');old_mtime=(code/'same.py').stat().st_mtime_ns
    raw=archive({'same.py':'same','new.yaml':'epochs: 2'}).getvalue()
    u=c.post('/api/uploads',json={'kind':'project','project_id':p['id'],'mode':'zip','files':[{'path':'code.zip','size':len(raw)}]},headers=headers).get_json()
    assert c.get('/api/uploads/'+u['id']+'/details').get_json()['receipts'][0]['received']==0
    c.put('/api/uploads/'+u['id']+'/files/0?offset=0',data=raw,headers=headers)
    result=c.post('/api/uploads/'+u['id']+'/finish',json={},headers=headers).get_json();assert wait(e,{'id':result['job_id']})['status']=='success'
    detail=c.get('/api/uploads/'+u['id']+'/details').get_json();assert detail['receipt_total']==1 and detail['total_count']==2
    rows={f['path']:f for f in detail['files']};assert rows['same.py']['status']=='skipped' and rows['new.yaml']['status']=='success'
    assert (code/'same.py').stat().st_mtime_ns==old_mtime
    assert c.get('/api/uploads/'+u['id']+'/details?job_id=other').status_code==400
    assert c.get('/api/jobs/'+result['job_id']+'/transfer-details?limit=0').status_code==400


def test_transfer_history_newest_first_pagination_and_small_state(client):
    c,app=client;login(c,app);e=app.extensions['engine']
    for n in range(35):
        e.store.put('jobs',{'id':str(n),'created_at':n+1,'status':'running' if n==0 else 'success','label':'dataset '+str(n),
                            'operation':'upload_dataset:["dataset"]','result':{'manifest':{'large':{'size':1,'sha256':'x'}},'id':'dataset'}})
    history=c.get('/api/transfers').get_json();assert history['total_count']==35 and history['entries'][0]['value']['id']=='34'
    assert history['entries'][-1]['value']['id']=='5'
    older=c.get('/api/transfers?offset=30').get_json();assert len(older['entries'])==5
    assert c.get('/api/transfers?status=active').get_json()['entries'][0]['value']['id']=='0'
    assert all('manifest' not in j['result'] for j in c.get('/api/state').get_json()['jobs'])
