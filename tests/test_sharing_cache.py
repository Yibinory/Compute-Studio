import time

import pytest

from conftest import FakeRemote
from scheduler.engine import Engine
from scheduler.files import manifest
from scheduler.storage import uid
from test_api import client, login


def count_markers(engine,servers,monkeypatch):
    calls=[]
    for server in servers:
        remote=engine.remote(server);original=remote.call
        def call(action,payload=None,timeout=120,original=original):
            if action in ('storage_marker','dataset_marker'):
                calls.append((action,(payload or {}).get('scope')))
            return original(action,payload,timeout)
        monkeypatch.setattr(remote,'call',call)
    return calls


def wait_jobs(engine):
    until=time.monotonic()+5
    while time.monotonic()<until:
        if not any(j['status'] in ('queued','running') for j in engine.store.all('jobs')):return
        time.sleep(.01)
    raise AssertionError('background probe did not finish')


def test_automatic_probe_once_without_time_expiry(engine,setup_project,monkeypatch):
    _,servers=setup_project;calls=count_markers(engine,servers,monkeypatch)
    engine.thread=object()
    try:
        engine.tick();wait_jobs(engine)
        first=list(calls);assert first
        for scope in ('work','data'):
            value=engine.store.get('storage',scope);value['checked_at']=1;engine.store.put('storage',value)
        for _ in range(3):engine.tick();wait_jobs(engine)
        assert calls==first
        assert len(engine.store.all('jobs'))==2
        # A resource/disconnection change is not a storage configuration change.
        engine.remote(servers[1]).offline=True
        engine.tick();wait_jobs(engine)
        assert calls==first
    finally:engine.thread=None


def test_failed_detection_is_cached_until_manual_retry(engine,setup_project,monkeypatch):
    _,servers=setup_project;calls=count_markers(engine,servers,monkeypatch)
    engine.remote(servers[1]).offline=True
    value=engine.ensure_storage('work');assert value['states'][servers[1]['id']]['status']=='unverified'
    first=list(calls);engine.remote(servers[1]).offline=False
    assert engine.ensure_storage('work')['checked_at']==value['checked_at']
    assert calls==first
    new=engine.detect_storage('work')
    assert len(calls)>len(first) and all(v['status']=='verified' for v in new['states'].values())


def test_replica_scan_and_cleanup_reuse_old_sharing(engine,setup_project,monkeypatch):
    project,servers=setup_project;calls=count_markers(engine,servers,monkeypatch)
    shared=engine.ensure_storage('work');shared['checked_at']=1;engine.store.put('storage',shared)
    first=list(calls)
    remote=engine.remote(servers[0]);remote.call('project',{'project_id':project['id']})
    (remote.project({'project_id':project['id']})/'manual.txt').write_text('keep')
    engine.refresh_replica(project['id'],servers[0]['id'])
    engine.cleanup_plan(project['id'],[servers[0]['id']])
    engine.clean_project(project['id'],[servers[0]['id']])
    assert calls==first
    assert (engine.local_project(project['id'])/'remote-archives'/servers[0]['id']/'snapshot'/'manual.txt').read_text()=='keep'


def test_work_change_only_invalidates_work_and_other_settings_do_not(engine,setup_project):
    _,servers=setup_project
    work=engine.ensure_storage('work');data=engine.ensure_storage('data')
    server=engine.save_server({**servers[0],'name':'new-label','retention_days':12,'benchmark_env':'bench'})
    assert engine.current_sharing('work')['checked_at']==work['checked_at']
    assert engine.current_sharing('data')['checked_at']==data['checked_at']
    server=engine.save_server({**server,'work_root':'/new/work'})
    assert not engine.current_sharing('work')
    assert engine.current_sharing('data')['checked_at']==data['checked_at']
    engine.ensure_storage('work')
    engine.save_server({**server,'data_root':'/new/data'})
    assert engine.current_sharing('work') and not engine.current_sharing('data')


@pytest.mark.parametrize('change',[{'host':'new-host'},{'identity_file':'key-file'},{'proxy_jump':'jump'},{'password':'temporary-fixture-password'}])
def test_connection_changes_invalidate_both_scopes(engine,setup_project,change):
    _,servers=setup_project;engine.ensure_storage('work');engine.ensure_storage('data')
    engine.save_server({**servers[0],**change})
    assert not engine.current_sharing('work') and not engine.current_sharing('data')


def test_dataset_uploads_reuse_group_and_fallback_without_markers(engine,setup_project,monkeypatch):
    _,servers=setup_project
    for s in servers:FakeRemote.roots[s['id']]=engine.root/'shared-data'
    calls=count_markers(engine,servers,monkeypatch);engine.ensure_storage('data');first=list(calls)
    ds={'id':uid(),'name':'CachedData','servers':{}}
    local=engine.root/'datasets'/ds['id']/'files';local.mkdir(parents=True);(local/'a').write_text('data')
    ds['manifest']=manifest(local);engine.store.put('datasets',ds)
    engine.upload_dataset(ds['id']);engine.upload_dataset(ds['id'])
    assert calls==first
    engine.remote(servers[0]).offline=True
    (local/'b').write_text('extra');ds['manifest']=manifest(local);engine.store.put('datasets',ds)
    with pytest.raises(ValueError):engine.upload_dataset(ds['id'])
    result=engine.store.get('datasets',ds['id'])
    assert result['servers'][servers[0]['id']]['status']=='verification_pending'
    assert result['servers'][servers[1]['id']]['status']=='success'
    assert (engine.remote(servers[1]).data_base/ds['name']/'b').read_text()=='extra'
    assert calls==first


def test_cached_results_survive_restart_and_legacy_upgrade(engine,setup_project):
    _,servers=setup_project
    for scope in ('work','data'):
        value=engine.ensure_storage(scope);value['checked_at']=123
        # Simulate a result from the previous release.
        value['configuration']={sid:fields[:4] for sid,fields in value['configuration'].items()}
        engine.store.put('storage',value)
    root=engine.root;engine.close();replacement=Engine(root,remote_factory=FakeRemote)
    try:
        for scope in ('work','data'):
            assert replacement.ensure_storage(scope)['checked_at']==123
        assert not replacement.remotes  # No SSH factory invoked to refresh the cache.
    finally:replacement.close()


def test_manual_scan_api_reprobes_even_when_cache_valid(client,monkeypatch):
    c,app=client;headers=login(c,app);engine=app.extensions['engine']
    server=engine.save_server({'name':'manual-test','host':'fixture','username':'test','work_root':'/work/test','data_root':'/data/test'})
    calls=count_markers(engine,[server],monkeypatch);engine.ensure_storage('work');first=len(calls)
    result=c.post('/api/storage/scan',json={'scope':'work'},headers=headers)
    assert result.status_code==200
    wait_jobs(engine);assert len(calls)>first
    assert engine.store.get('jobs',result.get_json()['id'])['status']=='success'
