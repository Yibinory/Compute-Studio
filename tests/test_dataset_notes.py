import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from werkzeug.exceptions import Conflict, NotFound

from scheduler.dataset_notes import read, save
from test_api import client, login


def dataset(engine):
    value={'id':'dataset-note-test','name':'notes_test','manifest':{'train.txt':{'size':4,'sha256':'a'*64}},
           'servers':{},'import_status':'syncing','created_at':1}
    engine.store.put('datasets',value)
    return value


def test_notes_are_local_metadata_and_survive_stale_transfer_writes(engine):
    d=dataset(engine);before=copy.deepcopy(d)
    tasks=engine.store.all('tasks');settings=copy.deepcopy(engine.settings)
    assert read(engine,d['id'])['revision']==0 and not engine.store.all('dataset_notes')
    note=save(engine,d['id'],{'text':'用途：验证集\r\n固定划分 <script>alert(1)</script>','revision':0})
    d['servers']={'s1':{'status':'success'}};engine.store.put('datasets',d)
    assert read(engine,d['id'])==note and '\r' not in note['text']
    assert engine.store.get('datasets',d['id'])['manifest']==before['manifest']
    assert not (engine.root/'datasets'/d['id']).exists()
    assert tasks==engine.store.all('tasks') and settings==engine.settings


def test_notes_do_not_wait_for_remote_engine_lock_and_long_text_stays_out_of_state(engine):
    d=dataset(engine);text='数据说明\n'*20000
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine.lock:
            value=pool.submit(save,engine,d['id'],{'text':text,'revision':0}).result(timeout=2)
    summary=engine.store.get_summary('dataset_notes',d['id'])
    assert value['text']==text and summary['characters']==len(text)
    assert summary['preview']==text[:240] and 'text' not in summary


def test_conflicts_retry_and_clear_are_safe(engine):
    d=dataset(engine);payload={'text':'first','revision':0}
    first=save(engine,d['id'],payload)
    assert save(engine,d['id'],payload)==first
    save(engine,d['id'],{'text':'second','revision':1})
    with pytest.raises(Conflict):save(engine,d['id'],{'text':'stale','revision':1})
    cleared=save(engine,d['id'],{'text':' \n ','revision':2})
    assert cleared['text']=='' and cleared['revision']==3
    engine.store.delete('datasets',d['id'])
    with pytest.raises(NotFound):save(engine,d['id'],{'text':'cannot resurrect','revision':3})


@pytest.mark.parametrize('payload',[None,{'text':3,'revision':0},{'text':'a','revision':True},
                                  {'text':'a\x00','revision':0},{'text':'a'*1_000_001,'revision':0}])
def test_invalid_note_does_not_save(engine,payload):
    d=dataset(engine)
    with pytest.raises(ValueError):save(engine,d['id'],payload)
    assert not engine.store.all('dataset_notes')


def test_api_permissions_state_preview_detail_and_persistence(client):
    c,app=client;e=app.extensions['engine'];d=dataset(e)
    assert c.get('/api/datasets/'+d['id']+'/note').status_code==401
    headers=login(c,app);url='/api/datasets/'+d['id']+'/note'
    assert c.post(url,json={'text':'a','revision':0}).status_code==403
    text='备注\n'*400
    assert c.post(url,json={'text':text,'revision':0},headers=headers).status_code==200
    assert c.get(url).json['text']==text
    state=c.get('/api/state').json
    assert state['runtime_features']['dataset_notes']
    assert state['datasets'][0]['note']['preview']==text[:240]
    assert 'text' not in state['datasets'][0]['note']
    assert c.get('/api/datasets/'+d['id']).json['dataset']['note']['text']==text
    assert c.post(url,json={'text':'stale','revision':0},headers=headers).status_code==409
    assert c.get('/api/datasets/unknown/note').status_code==404


def test_explicit_dataset_delete_removes_only_its_note(engine):
    from scheduler.uploads import Uploads
    engine.uploads=Uploads(engine)
    d=dataset(engine);d['import_status']='ready';engine.store.put('datasets',d)
    save(engine,d['id'],{'text':'remove with dataset','revision':0})
    other={**d,'id':'another-dataset','name':'another'};engine.store.put('datasets',other)
    save(engine,other['id'],{'text':'keep','revision':0})
    engine.delete_dataset(d['id'])
    assert [n['id'] for n in engine.store.all('dataset_notes')]==[other['id']]
    assert read(engine,other['id'])['text']=='keep'
