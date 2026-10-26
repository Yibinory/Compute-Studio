import pytest

from test_api import client, login


def test_file_index_lists_current_inputs_without_hashing_or_ssh(client,monkeypatch):
    c,app=client;assert c.get('/api/projects/no/file-index').status_code==401
    login(c,app);engine=app.extensions['engine'];p=engine.create_project({'name':'code'})
    root=engine.local_project(p['id'])/'code'
    (root/'src').mkdir();(root/'src'/'train.py').write_text('print(1)',encoding='utf-8')
    (root/'配置.yaml').write_bytes(b'epochs: 2')
    (root/'node_modules').mkdir();(root/'node_modules'/'ignored.js').write_text('ignored')
    for n in range(205):(root/f'config_{n:03}.json').write_text('{}')
    def forbidden(*args,**kw):raise AssertionError('Browsing must not hash contents or connect SSH')
    monkeypatch.setattr('scheduler.files.digest',forbidden);monkeypatch.setattr('app.manifest',forbidden);monkeypatch.setattr(engine,'remote',forbidden)
    url='/api/projects/'+p['id']+'/file-index'
    first=c.get(url).get_json();second=c.get(url+'?offset=100').get_json()
    assert first['total_count']==207 and len(first['files'])==100
    assert not {v['path'] for v in first['files']} & {v['path'] for v in second['files']}
    assert all('sha256' not in v and 'modified_at' in v for v in first['files'])
    found=c.get(url,query_string={'query':'配置'}).get_json();assert found['files'][0]['path']=='配置.yaml'
    assert c.get(url+'?query=SRC/').get_json()['matched_count']==1
    (root/'src'/'train.py').unlink()
    assert c.get(url+'?query=src/').get_json()['files']==[]


def test_task_file_scope_and_invalid_pagination(client):
    c,app=client;login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'A'});other=e.create_project({'name':'B'})
    task=e.create_task(p['id'],{'name':'train','command':'echo test'})
    target=e.local_project(p['id'])/'tasks'/task['id']/'files';(target/'task.yaml').write_bytes(b'epochs: 3')
    url='/api/projects/'+p['id']+'/file-index'
    assert c.get(url).get_json()['total_count']==0
    assert c.get(url,query_string={'task_id':task['id']}).get_json()['files'][0]['path']=='task.yaml'
    assert c.get('/api/projects/'+other['id']+'/file-index',query_string={'task_id':task['id']}).status_code==400
    for query in ('offset=-1','offset=bad','limit=0','limit=201','limit=bad'):
        assert c.get(url+'?'+query).status_code==400


def test_file_index_rejects_symlink_directories(client,tmp_path):
    c,app=client;login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'links'});root=e.local_project(p['id'])/'code'
    target=tmp_path/'outside';target.mkdir();(target/'secret.txt').write_text('secret')
    try:(root/'linked').symlink_to(target,target_is_directory=True)
    except OSError:pytest.skip('Host does not permit filesystem symlinks')
    assert c.get('/api/projects/'+p['id']+'/file-index').status_code==400


def test_state_preserves_older_active_transfers_beyond_history_limits(client):
    c,app=client;login(c,app);e=app.extensions['engine']
    e.store.put('uploads',{'id':'active-upload','status':'hashing','files':[]})
    e.store.put('jobs',{'id':'active-job','status':'running','operation':'upload_dataset:["dataset"]'})
    for n in range(105):e.store.put('uploads',{'id':'old-'+str(n),'status':'success','files':[]})
    for n in range(55):e.store.put('jobs',{'id':'old-'+str(n),'status':'success'})
    result=c.get('/api/state').get_json()
    assert len(result['uploads'])==101 and any(u['id']=='active-upload' for u in result['uploads'])
    assert len(result['jobs'])==51 and any(j['id']=='active-job' for j in result['jobs'])
