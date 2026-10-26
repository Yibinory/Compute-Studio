import hashlib

import pytest

from scheduler.sharing import configuration
from test_api import client, login


def seed(app):
    engine=app.extensions['engine']
    entries={f'train/image_{i:04d}.txt':{'size':3,'sha256':hashlib.sha256(b'abc').hexdigest()} for i in range(305)}
    entries['验证/labels.csv']={'size':5,'sha256':hashlib.sha256(b'label').hexdigest()}
    dataset={'id':'detail-test','name':'Fundus','created_at':100,'servers':{},'manifest':entries}
    engine.store.put('datasets',dataset)
    return engine,dataset


def test_details_are_read_only_and_do_not_rehash_or_probe(client,monkeypatch):
    c,app=client;engine,dataset=seed(app)
    assert c.get('/api/datasets/detail-test').status_code==401
    assert c.get('/api/datasets/detail-test/files').status_code==401
    assert c.get('/api/datasets/detail-test/file?path=train/image_0000.txt').status_code==401
    login(c,app)
    for i in range(2):
        engine.store.put('servers',{'id':str(i),'name':'Server '+str(i),'host':'fixture','username':'test','data_root':'/shared'})
    engine.store.put('storage',{'id':'data','scope':'data','checked_at':12,'groups':[['0','1']],
                              'states':{'0':{'status':'verified','root':'/resolved'}},
                              'configuration':configuration(engine.store.all('servers'),'data')})
    engine.store.put('tasks',{'id':'t','name':'Train','project_id':'p','status':'draft','datasets':['Fundus']})
    def forbidden(*args,**kwargs):raise AssertionError('Details must not perform SSH or hash')
    monkeypatch.setattr(engine,'remote',forbidden)
    monkeypatch.setattr('app.manifest',forbidden)
    monkeypatch.setattr('scheduler.files.digest',forbidden)
    result=c.get('/api/datasets/detail-test').get_json()
    assert 'manifest' not in result['dataset']
    assert result['dataset']['file_count']==306
    assert result['dataset']['total_bytes']==920
    assert result['dataset']['local_path']==str(engine.root/'datasets'/'detail-test'/'files')
    assert result['servers'][0]['dataset_path']=='/resolved/Fundus'
    assert result['servers'][0]['shared_with']==['1']
    assert result['tasks'][0]['id']=='t'
    assert result['sharing']['checked_at']==12
    assert c.get('/api/datasets/detail-test/files').get_json()['total_count']==306
    assert engine.store.all('jobs')==[]


def test_paginated_inventory_search_unicode_and_stable_order(client):
    c,app=client;seed(app);login(c,app)
    first=c.get('/api/datasets/detail-test/files').get_json()
    second=c.get('/api/datasets/detail-test/files?offset=100&limit=100').get_json()
    assert len(first['files'])==100 and len(second['files'])==100
    assert first['files'][-1]['path']<second['files'][0]['path']
    assert not {v['path'] for v in first['files']} & {v['path'] for v in second['files']}
    found=c.get('/api/datasets/detail-test/files?query=TRAIN/IMAGE_000').get_json()
    assert found['matched_count']==10 and found['total_count']==306
    found=c.get('/api/datasets/detail-test/files',query_string={'query':'验证/'}).get_json()
    assert [v['path'] for v in found['files']]==['验证/labels.csv']
    assert c.get('/api/datasets/detail-test/files?offset=1000').get_json()['files']==[]
    assert c.get('/api/datasets/detail-test/files?query=absent').get_json()['matched_count']==0


@pytest.mark.parametrize('query',['offset=-1','limit=0','limit=201','limit=no','offset=no'])
def test_reject_invalid_pagination(client,query):
    c,app=client;seed(app);login(c,app)
    assert c.get('/api/datasets/detail-test/files?'+query).status_code==400


def test_download_restricts_paths_and_uses_current_storage_root(client,tmp_path):
    c,app=client;engine,dataset=seed(app);login(c,app)
    for suffix in ('','/files','/file?path=unknown'):
        assert c.get('/api/datasets/missing'+suffix).status_code==404
    for path in ('../access-token.txt','/absolute','C:/unsafe','a\\b'):
        assert c.get('/api/datasets/detail-test/file',query_string={'path':path}).status_code==400
    assert c.get('/api/datasets/detail-test/file?path=unknown').status_code==404
    assert c.get('/api/datasets/detail-test/file?path=train/image_0000.txt').status_code==404
    previous=engine.root
    try:
        engine.root=tmp_path/'relocated'
        target=engine.root/'datasets'/'detail-test'/'files'/'train'/'image_0000.txt'
        target.parent.mkdir(parents=True);target.write_bytes(b'abc')
        result=c.get('/api/datasets/detail-test/file?path=train/image_0000.txt')
        assert result.status_code==200 and result.data==b'abc'
        assert 'attachment;' in result.headers['Content-Disposition']
        assert c.get('/api/datasets/detail-test').get_json()['dataset']['local_path'].startswith(str(engine.root))
    finally:engine.root=previous
