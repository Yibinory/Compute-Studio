import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from scheduler.engine import Engine
from scheduler.policy import choose
from conftest import FakeRemote


def running(engine,setup_project):
    project,servers=setup_project
    task=engine.create_task(project['id'],{'name':'controlled','command':'bash run.sh','server_ids':[servers[0]['id']],'memory_mb':18000})
    engine.task_action(task['id'],'queue');engine.tick()
    return engine.store.get('tasks',task['id']),project,servers


def test_pause_resume_keeps_run_identity_and_reservation(engine,setup_project):
    task,project,servers=running(engine,setup_project);attempt=task['attempt_id'];launches=len(FakeRemote.launches)
    paused=engine.task_action(task['id'],'pause')
    assert paused['status']=='suspended' and paused['attempt_id']==attempt
    engine.tick();paused=engine.store.get('tasks',task['id']);assert paused['status']=='suspended'
    server=engine.store.get('servers',servers[0]['id'])
    assert choose({'memory_mb':8000,'level':'emergency'},[server],[paused],engine.settings) is None
    with pytest.raises(ValueError):engine.clean_project(project['id'])
    with pytest.raises(ValueError):engine.task_action(task['id'],'queue')
    resumed=engine.task_action(task['id'],'resume')
    assert resumed['status']=='running' and resumed['attempt_id']==attempt and len(FakeRemote.launches)==launches


def test_cancel_ignores_scheduler_lock_and_busy_file_workers(engine,setup_project):
    task,project,servers=running(engine,setup_project);release=threading.Event()
    blockers=[engine.jobs.submit(release.wait,10) for _ in range(2)]
    try:
        with engine.lock:
            job=engine.enqueue('control',engine.task_action,task['id'],'cancel')
            until=time.monotonic()+2
            while engine.store.get('jobs',job['id'])['status'] in ('queued','running') and time.monotonic()<until:time.sleep(.01)
            assert engine.store.get('jobs',job['id'])['status']=='success'
            assert engine.store.get('tasks',task['id'])['status']=='canceling'
    finally:release.set()
    engine.tick();assert engine.store.get('tasks',task['id'])['status']=='canceled'


def test_cancel_during_preparation_never_launches_shell(engine,setup_project,monkeypatch):
    project,servers=setup_project;task=engine.create_task(project['id'],{'name':'preparing','command':'bash run.sh','server_ids':[servers[0]['id']]});engine.task_action(task['id'],'queue')
    remote=engine.remote(servers[0]);original=remote.upload;entered=threading.Event();release=threading.Event()
    def stalled(*args,**kwargs):
        entered.set();assert release.wait(5);return original(*args,**kwargs)
    monkeypatch.setattr(remote,'upload',stalled)
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick=pool.submit(engine.tick)
        try:
            assert entered.wait(5)
            value=engine.task_action(task['id'],'cancel');assert value['cancel_requested']
        finally:release.set()
        tick.result(timeout=10)
    assert engine.store.get('tasks',task['id'])['status']=='canceled' and not FakeRemote.launches


def test_restart_preserves_suspended_run_without_duplicate_launch(engine,setup_project):
    task,project,servers=running(engine,setup_project);engine.task_action(task['id'],'pause');count=len(FakeRemote.launches)
    # Closing a controller does not terminate the remote simulated run.
    other=Engine(engine.root,remote_factory=FakeRemote)
    try:
        other.tick();value=other.store.get('tasks',task['id'])
        assert value['status']=='suspended' and value['attempt_id']==task['attempt_id'] and len(FakeRemote.launches)==count
        other.task_action(task['id'],'resume');assert other.store.get('tasks',task['id'])['status']=='running'
    finally:other.close()


def test_stale_runtime_write_preserves_cancel_intent(engine,setup_project):
    task,project,servers=running(engine,setup_project);engine.task_action(task['id'],'cancel')
    engine.put_runtime_task(task)
    saved=engine.store.get('tasks',task['id']);assert saved['cancel_requested'] and saved['status']=='canceling'
    engine.remote(servers[0]).complete(saved,137,log='CUDA out of memory')
    engine.tick();saved=engine.store.get('tasks',task['id']);assert saved['status']=='canceled' and saved['attempts']==1
