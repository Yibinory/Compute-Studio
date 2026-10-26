import io
import time

from test_api import client,login
from scheduler.logs import tail_stream,chunk_stream,TAIL_BYTES
from scheduler.storage import uid


def create_log_task(app,status='running'):
    e=app.extensions['engine'];server=e.save_server({'name':'logs','host':'fake','username':'tester','work_root':'/work/compute','data_root':'/data/datasets'})
    p=e.create_project({'name':'Logs'});t=e.create_task(p['id'],{'name':'failed_test','command':'python fail.py'})
    t.update(status=status,attempt_id=uid(),server_id=server['id']);e.store.put('tasks',t)
    remote=e.remote(server);path=remote.project(t)/'runs'/t['attempt_id']/'_control'/'run.log';path.parent.mkdir(parents=True);path.write_text(''.join('日志 %d\n'%i for i in range(500)),encoding='utf-8')
    return e,t,path


def test_tail_is_exactly_latest_200_lines_and_bounded():
    text=''.join('日志 %d\n'%i for i in range(500));raw=b'x'*5*1024**2+b'\n'+text.encode()
    result=tail_stream(io.BytesIO(raw),len(raw));assert result['text']==''.join('日志 %d\n'%i for i in range(300,500))
    assert result['lines']==200 and result['has_more'] and result['read_bytes']<=65536
    huge=tail_stream(io.BytesIO(b'x'*(TAIL_BYTES+10)),TAIL_BYTES+10);assert huge['truncated_line'] and huge['read_bytes']==TAIL_BYTES


def test_chunk_pages_preserve_utf8_and_cover_whole_file():
    text='中文 🚀\n'*12000;raw=text.encode();offset=0;pages=[]
    while offset<len(raw):
        result=chunk_stream(io.BytesIO(raw),len(raw),offset,1031);assert result['next_offset']>offset
        pages.append(result['text']);offset=result['next_offset']
    assert ''.join(pages)==text
    assert chunk_stream(io.BytesIO('中'.encode()),3,0,1)['text']=='中'


def test_active_log_polling_cache_tail_and_full_download(client,monkeypatch):
    c,app=client;login(c,app);e,t,path=create_log_task(app);remote=e.remote(e.store.get('servers',t['server_id']))
    original=remote.read_log;calls=[]
    def read(*args,**kwargs):calls.append(args);return original(*args,**kwargs)
    monkeypatch.setattr(remote,'read_log',read)
    url='/api/tasks/'+t['id']+'/log';tail=c.get(url).get_json();assert tail['lines']==200 and tail['text'].splitlines()[0]=='日志 300'
    assert tail['source']=='server' and c.get(url).get_json()==tail and len(calls)==1
    with path.open('a',encoding='utf-8') as stream:stream.write('new crash: exit 1\n')
    now=time.monotonic();monkeypatch.setattr('app.time.monotonic',lambda:now+3)
    assert c.get(url).get_json()['text'].splitlines()[-1]=='new crash: exit 1' and len(calls)==2
    complete=c.get(url+'?mode=full&limit=1031').get_json();assert complete['text'].splitlines()[0]=='日志 0' and complete['has_more']
    assert c.get(url+'/download').data==path.read_bytes()
    assert c.get(url+'?attempt_id=foreign').status_code==400
    for q in ('offset=-1','mode=unknown','limit=0','limit=131073'):assert c.get(url+'?'+q).status_code==400


def test_completed_and_previous_attempt_logs_use_local_full_copy(client,monkeypatch):
    c,app=client;login(c,app);e,t,_=create_log_task(app,'failed')
    local=e.local_project(t['project_id'])/'runs'/t['attempt_id']/'control'/'run.log';local.parent.mkdir(parents=True);local.write_text('line\n'*400,encoding='utf-8')
    monkeypatch.setattr(e,'remote',lambda *_:(_ for _ in ()).throw(AssertionError('Completed local logs must not open SSH')))
    url='/api/tasks/'+t['id']+'/log';assert c.get(url).get_json()['lines']==200
    with local.open('a',encoding='utf-8') as stream:stream.write('appended\n')
    assert c.get(url).get_json()['text'].splitlines()[-1]=='appended'
    attempt=t['attempt_id'];t.update(status='waiting',previous_attempts=[{'attempt_id':attempt,'server_id':t['server_id'],'exit_code':1}],attempt_id=None,server_id=None);e.store.put('tasks',t)
    assert c.get(url).get_json()['attempt_id']==attempt
    assert c.get(url+'/download?attempt_id='+attempt).data==local.read_bytes()


def test_empty_logs_and_foreign_run_guard(client):
    c,app=client;login(c,app);e=app.extensions['engine'];p=e.create_project({'name':'No_run'});t=e.create_task(p['id'],{'name':'draft','command':'echo test'})
    url='/api/tasks/'+t['id']+'/log';assert c.get(url).get_json()['text']=='尚无运行日志'
    assert c.get(url+'/download').status_code==400
    assert c.get(url+'?attempt_id=../../other').status_code==400
    _,running,path=create_log_task(app);path.unlink()
    missing='/api/tasks/'+running['id']+'/log'
    assert c.get(missing).get_json()['exists'] is False
    assert c.get(missing+'/download').status_code==400


def test_log_channel_handshake_timeout_preserves_connection(tmp_path,monkeypatch):
    import threading
    from types import SimpleNamespace
    from scheduler.remote import Remote
    closed=threading.Event()
    class Session:
        def settimeout(self,value):pass
        def invoke_subsystem(self,name):
            assert closed.wait(1)
            raise EOFError('handshake interrupted')
        def close(self):closed.set()
    transport=SimpleNamespace(is_active=lambda:True,open_session=lambda **kw:Session())
    remote=Remote({'id':'logs'},tmp_path);remote.client=SimpleNamespace(get_transport=lambda:transport)
    monkeypatch.setattr('scheduler.remote.LOG_OPEN_TIMEOUT',.05)
    import pytest
    start=time.monotonic()
    with pytest.raises(EOFError):remote.log_channel()
    assert time.monotonic()-start<.5 and remote.client is not None


def test_log_channel_does_not_wait_for_dataset_command_lock(tmp_path,monkeypatch):
    import threading
    from types import SimpleNamespace
    from scheduler.remote import Remote
    held=threading.Event();release=threading.Event()
    session=SimpleNamespace(settimeout=lambda _:None,invoke_subsystem=lambda _:None,close=lambda:None)
    transport=SimpleNamespace(is_active=lambda:True,open_session=lambda **kw:session)
    remote=Remote({'id':'logs'},tmp_path);remote.client=SimpleNamespace(get_transport=lambda:transport)
    channel=object();monkeypatch.setattr('scheduler.remote.paramiko.SFTPClient',lambda _:channel)
    def hold():
        with remote.lock:held.set();release.wait(1)
    worker=threading.Thread(target=hold);worker.start();assert held.wait(.5)
    try:
        start=time.monotonic();assert remote.log_channel() is channel;assert time.monotonic()-start<.2
    finally:release.set();worker.join()


def test_slow_log_read_does_not_accumulate_poll_requests(client,monkeypatch):
    import threading
    c,app=client;login(c,app);e,t,_=create_log_task(app)
    remote=e.remote(e.store.get('servers',t['server_id']));original=remote.read_log
    started=threading.Event();release=threading.Event();results=[]
    def blocked(*args,**kwargs):started.set();assert release.wait(3);return original(*args,**kwargs)
    monkeypatch.setattr(remote,'read_log',blocked)
    other=app.test_client();login(other,app);url='/api/tasks/'+t['id']+'/log'
    worker=threading.Thread(target=lambda:results.append(other.get(url).get_json()));worker.start();assert started.wait(1)
    try:
        start=time.monotonic();assert c.get(url).get_json()['pending'] is True
        assert c.get(url+'?mode=full').get_json()['pending'] is True
        assert time.monotonic()-start<.5
    finally:release.set();worker.join()
    assert results[0]['lines']==200
