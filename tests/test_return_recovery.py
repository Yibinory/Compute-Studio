import contextlib
import time

import pytest

from scheduler.content import Content
from test_content_storage import enable,create


@pytest.mark.parametrize('corrupt_partial',[False,True])
def test_interrupted_content_return_resumes_same_run_and_reports_progress(engine,setup_project,monkeypatch,corrupt_partial):
    enable(engine);p,servers=setup_project;t=create(engine,p,servers[0]);attempt=t['attempt_id'];remote=engine.remote(servers[0]);data='z'*(2*1024*1024)
    remote.complete(t,output=data);original=remote.sftp.file;state={'fail':True,'read':0,'statuses':[],'progress':[]}
    class Reader:
        def __init__(self,stream):self.stream=stream
        def __getattr__(self,name):return getattr(self.stream,name)
        def read(self,size):
            current=engine.store.get('tasks',t['id']);state['statuses'].append(current['status']);state['progress'].append(current.get('runtime_progress',{}))
            if state['fail'] and state['read']>=256*1024:raise OSError('simulated interrupted network')
            block=self.stream.read(size);state['read']+=len(block);return block
    @contextlib.contextmanager
    def file(path,mode):
        with original(path,mode) as stream:yield Reader(stream)
    monkeypatch.setattr(remote.sftp,'file',file)
    engine.tick();failed=engine.store.get('tasks',t['id']);assert failed['status']=='sync_failed'
    assert failed['sync_last_error']=='simulated interrupted network' and failed['attempt_id']==attempt
    pool=Content(engine,p['id']);partials=list(pool.root.glob('*/*.download'));assert len(partials)==1;offset=partials[0].stat().st_size;assert offset==256*1024
    if corrupt_partial:
        with partials[0].open('r+b') as f:f.seek(100000);f.write(b'X')
    state.update(fail=False,read=0,statuses=[],progress=[])
    engine.task_action(t['id'],'sync');done=engine.store.get('tasks',t['id'])
    assert done['status']=='success' and done['attempt_id']==attempt and 'sync_last_error' not in done
    assert set(state['statuses'])=={'syncing'}
    assert any(v.get('bytes',0)>0 and v.get('total',0)==len(data) for v in state['progress'])
    assert done['content_return']['resumed_bytes']==offset
    if not corrupt_partial:assert done['content_return']['downloaded_bytes']==len(data)-offset
    assert pool.artifact(attempt,'output.txt').read_text()==data


def test_background_cleanup_accepts_job_context(engine):
    job=engine.enqueue('cleanup',engine.auto_cleanup)
    end=time.monotonic()+3
    while time.monotonic()<end:
        value=engine.store.get('jobs',job['id'])
        if value['status'] in ('success','failed'):break
        time.sleep(.01)
    assert value['status']=='success',value.get('error')
