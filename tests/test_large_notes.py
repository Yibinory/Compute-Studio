import io
import json
from scheduler.storage import Store
from test_api import client,login

def setup(client):
    c,app=client;h=login(c,app);engine=app.extensions['engine']
    p=engine.create_project({'name':'Large_Notes'})
    task=engine.create_task(p['id'],{'name':'experiment','command':'echo test'})
    return c,app,h,task,'/api/tasks/'+task['id']+'/notes'

def test_large_json_notes_keep_source_and_exclude_body_from_polling(client):
    c,app,h,t,url=setup(client)
    body='    indented code\n\n'+('## 观察\n\n$\\alpha^2$\n\n'*3000)+'LAST-SECTION\n'
    response=c.post(url,json={'body':body},headers=h);assert response.status_code==200
    note=response.get_json();assert note['large'] and note['body_chars']==len(body) and 'body' not in note
    text=url+'/'+note['id']+'/text';assert c.get(text).data.decode()==body
    assert 'attachment' in c.get(text+'?download=1').headers['Content-Disposition']
    assert len(json.dumps(c.get('/api/state').get_json()['notes']))<5000
    inspection=c.get('/api/tasks/'+t['id']+'/inspection').get_json()
    assert 'body' not in inspection['notes'][0] and 'body_file' not in inspection['notes'][0]
    engine=app.extensions['engine'];record=engine.store.get('notes',note['id']);old=engine.store.root/'notes'/record['body_file'];assert old.is_file()
    edited=body+'\nNew conclusion'
    updated=c.post(url,json={'id':note['id'],'body':edited},headers=h).get_json()
    assert updated['id']==note['id'] and c.get(text).data.decode()==edited and not old.exists()
    current=engine.store.root/'notes'/engine.store.get('notes',note['id'])['body_file']
    assert c.post(url,json={'id':note['id'],'body':'small note'},headers=h).get_json()['body']=='small note'
    assert not current.exists() and c.get(text).data==b'small note'

def test_streaming_md_file_handles_utf8_boundaries_and_delete(client):
    c,app,h,t,url=setup(client)
    body='# 大文档\n\n'+('中文🙂 $x^2$\n\n'*120000)+'END-OF-DOCUMENT\n'
    raw=body.encode();assert len(raw)>1024**2
    response=c.post(url+'/file',data={'file':(io.BytesIO(raw),'large.md')},headers=h);assert response.status_code==200
    note=response.get_json();assert note['large'] and note['body_chars']==len(body) and note['body_bytes']==len(raw)
    assert c.get(url+'/'+note['id']+'/text').data==raw
    engine=app.extensions['engine'];path=engine.store.root/'notes'/engine.store.get('notes',note['id'])['body_file']
    assert path.stat().st_size==len(raw)
    assert c.post(url,json={'id':note['id'],'delete':True},headers=h).status_code==200
    assert not path.exists() and not engine.store.summaries('notes')

def test_note_files_require_ownership_auth_and_utf8_and_leave_no_partial_files(client):
    c,app,h,t,url=setup(client)
    note=c.post(url,json={'body':'original'},headers=h).get_json()
    other=app.extensions['engine'].create_task(t['project_id'],{'name':'other','command':'echo test'})
    assert c.get('/api/tasks/'+other['id']+'/notes/'+note['id']+'/text').status_code==400
    for raw in (b'\xff invalid',b'\xef\xbb\xbf  \n\t'):
        assert c.post(url+'/file',data={'file':(io.BytesIO(raw),'bad.md')},headers=h).status_code==400
    assert not list((app.extensions['engine'].root/'notes').glob('*'))
    assert c.post(url+'/file',data={'file':(io.BytesIO(b'text'),'safe.md')}).status_code==403
    c.get('/');c.post('/login',json={'token':'wrong'})
    unauth=app.test_client();assert unauth.get(url+'/'+note['id']+'/text').status_code==401
    assert c.get(url+'/'+note['id']+'/text').data==b'original'

def test_legacy_large_notes_have_compact_summaries_after_reopen(tmp_path):
    store=Store(tmp_path);body='legacy data\n'*10000
    store.put('notes',{'id':'legacy','task_id':'task','project_id':'project','body':body,'created_at':1,'updated_at':1})
    store.db.execute("DELETE FROM record_summaries WHERE kind='notes'");store.db.commit();store.close()
    store=Store(tmp_path)
    try:
        summary=store.summaries('notes')[0]
        assert summary['large'] and 'body' not in summary and summary['body_chars']==len(body)
        assert store.get('notes','legacy')['body']==body
    finally:store.close()
