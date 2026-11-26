import json

import pytest

from conftest import FakeRemote
from scheduler import workdirs
from scheduler.content import Content
from scheduler.engine import Engine


class DiskRemote(FakeRemote):
    space = {}
    fail_prepare = None

    def __init__(self, server, root, password=None):
        super().__init__(server,root,password)
        self.directory = server['work_root']
        self.work_base = self.root / self.directory.rsplit('/',1)[-1]
        self.work_base.mkdir(exist_ok=True)

    def call(self, action, payload=None, timeout=120):
        if action=='probe':
            sample=super().call(action,payload,timeout)
            sample['work_free_gb']=self.space[self.directory]
            if (payload or {}).get('work_paths'):
                sample['work_volumes']={p:{'device':p,'free_gb':v} for p,v in self.space.items()}
            return sample
        if action=='discard_unstarted_workspace':
            assert self.call('status',payload)['state']=='absent'
            return {'removed_files':0}
        return super().call(action,payload,timeout)

    def ensure_objects(self,*args):
        if self.directory==self.fail_prepare:
            raise workdirs.WorkDiskFull('写入工作盘时空间不足')
        return super().ensure_objects(*args)


@pytest.fixture
def disks(engine,monkeypatch):
    engine.remote_factory=DiskRemote
    monkeypatch.setattr(DiskRemote,'space',{'/mnt/ssd':100,'/mnt/large':300})
    server=engine.save_server({'host':'host','username':'tester','work_root':'/mnt/ssd',
                               'extra_work_roots':['/mnt/large'],'data_root':'/data/datasets'})
    project=engine.create_project({'name':'multi'})
    (engine.local_project(project['id'])/'code'/'main.py').write_text('original')
    return project['id'],server['id']


def queued(engine,project):
    task=engine.create_task(project,{'name':'experiment','command':'python main.py','include_files':['main.py']})
    engine.task_action(task['id'],'queue')
    return task['id']


def test_sticky_choice_disk_failure_snapshot_and_cleanup(engine,disks):
    project,sid=disks
    task_id=queued(engine,project)
    engine.tick()
    get=lambda:engine.store.get('tasks',task_id)
    old=get()
    server=engine.store.get('servers',sid)
    assert old['status']=='running' and old['work_root']=='/mnt/large'
    old_remote=engine.run_remote(server,old)
    attempt=old['attempt_id']
    original=Content(engine,project).read_catalog(attempt)['inputs']
    (engine.local_project(project)/'code'/'main.py').write_text('changed after first launch')
    old_remote.complete(old,1,log='No space left on device')
    ctrl=old_remote.project(old)/'runs'/attempt/'_control'
    status=json.loads((ctrl/'status.json').read_text())
    (ctrl/'status.json').write_text(json.dumps({**status,'work_disk_full':True}))
    # Even across a failed log download, do not remove the old workspace or double-launch.
    old_remote.fail_download=True
    engine.tick()
    assert get()['status']=='sync_failed' and get()['attempt_id']==attempt
    assert (ctrl.parent/'workspace').is_dir()
    old_remote.fail_download=False
    engine.store.put('tasks',{**get(),'sync_retry_at':0})
    engine.tick()
    current=get()
    assert current['status']=='running' and current['work_root']=='/mnt/ssd'
    assert current['attempt_id']!=attempt and current['server_id']==sid
    assert current['previous_attempts'][-1]['work_root']=='/mnt/large'
    assert Content(engine,project).read_catalog(current['attempt_id'])['inputs']==original
    assert not (ctrl.parent/'workspace').exists() and (ctrl/'run.log').exists()
    assert (engine.local_project(project)/'runs'/attempt/'control'/'run.log').exists()
    assert engine.store.get('projects',project)['preferred_work_roots'][sid]=='/mnt/ssd'
    engine.run_remote(server,current).complete(current)
    engine.tick()
    assert get()['status']=='success'
    next_id=queued(engine,project)
    engine.tick()
    next_task=engine.store.get('tasks',next_id)
    assert next_task['work_root']=='/mnt/ssd'
    # An ordinary script failure must follow the ordinary return path.
    engine.run_remote(server,next_task).complete(next_task,1,log='invalid configuration')
    engine.tick()
    assert engine.store.get('tasks',next_id)['status']=='failed'
    secondary=workdirs.endpoint_id(server,'/mnt/large')
    engine.refresh_replica(project,secondary)
    engine.clean_project(project,[secondary])
    assert not old_remote.project(old).exists()
    assert engine.remote(server).project(old).exists()


def test_preparation_fails_over_and_stops_when_disks_exhausted(engine,disks,monkeypatch):
    project,sid=disks
    monkeypatch.setattr(DiskRemote,'fail_prepare','/mnt/large')
    task_id=queued(engine,project)
    engine.tick()
    task=engine.store.get('tasks',task_id)
    assert task['status']=='waiting' and task['retry_snapshot_attempt']
    monkeypatch.setattr(DiskRemote,'fail_prepare','/mnt/ssd')
    engine.tick()
    engine.tick()
    task=engine.store.get('tasks',task_id)
    assert task['status']=='waiting' and len(task['previous_attempts'])==2
    assert len(task['disk_exclusions'])==2 and not task['attempt_id']
    assert not FakeRemote.launches
    DiskRemote.space['/mnt/large']+=10
    engine.tick()
    restarted=engine.store.get('tasks',task_id)
    assert restarted['status']=='running' and restarted['work_root']=='/mnt/large'


def test_alias_paths_and_failed_mount_are_not_candidates():
    server={'id':'s','work_root':'/mnt/one','extra_work_roots':['/mnt/alias','/mnt/two'],
            'snapshot':{'work_volumes':{'/mnt/one':{'device':'a','free_gb':40},'/mnt/alias':{'device':'a','free_gb':40},
                                        '/mnt/two':{'error':'挂载已改变'}}}}
    assert not workdirs.available({'disk_retry_server':'s','disk_exclusions':{'/mnt/one':'a'}},server)
    with pytest.raises(ValueError):workdirs.validate_extra(['/mnt/one/'],'/mnt/one')


def test_existing_project_keeps_old_root_when_server_gains_disk(engine,disks):
    old,sid=disks
    engine.save_server({'id':sid,'extra_work_roots':[]})
    server=engine.save_server({'id':sid,'extra_work_roots':['/mnt/large']})
    assert workdirs.preferred_root(engine.store.get('projects',old),server)=='/mnt/ssd'
    new=engine.create_project({'name':'new'})
    assert workdirs.preferred_root(new,server) is None
    task_id=queued(engine,old)
    engine.tick()
    assert engine.store.get('tasks',task_id)['work_root']=='/mnt/ssd'


def test_upgrade_defaults_once_and_keeps_explicit_choices(engine,disks):
    old,sid=disks
    server=engine.store.get('servers',sid)
    server.pop('existing_project_root')
    engine.store.put('servers',server)
    chosen=engine.create_project({'name':'already_assigned'})
    engine.store.put('projects',{**chosen,'preferred_work_roots':{sid:'/mnt/large'}})
    engine.close()
    for restart in range(2):
        live=Engine(engine.root,remote_factory=DiskRemote)
        try:
            server=live.store.get('servers',sid)
            assert workdirs.preferred_root(live.store.get('projects',old),server)=='/mnt/ssd'
            assert workdirs.preferred_root(live.store.get('projects',chosen['id']),server)=='/mnt/large'
            if restart==0:new=live.create_project({'name':'created_after_upgrade'})
            assert workdirs.preferred_root(live.store.get('projects',new['id']),server) is None
        finally:live.close()


def test_capacity_counts_only_distinct_sampled_work_filesystems():
    server={'work_root':'/one','extra_work_roots':['/alias','/two','/offline','/pending'],
            'snapshot':{'work_disk':{'free_bytes':999},'data_disk':{'free_bytes':555},
                        'work_volumes':{'/one':{'device':'a','free_bytes':3,'total_bytes':10,'used_bytes':6},
                                        '/alias':{'device':'a','free_bytes':3,'total_bytes':10,'used_bytes':6},
                                        '/two':{'device':'b','free_bytes':0,'total_bytes':90,'used_bytes':87},
                                        '/offline':{'error':'挂载已改变'}}}}
    summary=workdirs.capacity(server)
    assert [s['paths'] for s in summary['disks']]==[['/one','/alias'],['/two']]
    assert (summary['total_bytes'],summary['used_bytes'],summary['free_bytes'])==(100,93,3)
    assert summary['unavailable']==[{'path':'/offline','error':'挂载已改变'},{'path':'/pending','error':'等待采样'}]
    server['snapshot']['work_volumes']={}
    assert workdirs.capacity(server)['free_bytes'] is None
    server['extra_work_roots']=[]
    assert workdirs.capacity(server)['free_bytes']==999
    server['snapshot']={'work_free_gb':.5}
    assert workdirs.capacity(server)['free_bytes']==2**29
    assert workdirs.capacity(server)['total_bytes'] is None


def test_restart_tracks_existing_attempt_on_original_volume(engine,disks):
    project,sid=disks
    DiskRemote.space['/mnt/ssd']=0
    task_id=queued(engine,project)
    engine.tick()
    old=engine.store.get('tasks',task_id)
    assert old['status']=='running' and old['work_root']=='/mnt/large'
    count=len(FakeRemote.launches)
    engine.close()
    restarted=Engine(engine.root,remote_factory=DiskRemote)
    try:
        DiskRemote.space['/mnt/ssd']=500
        restarted.tick()
        saved=restarted.store.get('tasks',task_id)
        assert saved['attempt_id']==old['attempt_id'] and saved['status']=='running'
        assert len(FakeRemote.launches)==count
        restarted.run_remote(restarted.store.get('servers',sid),saved).complete(saved)
        restarted.tick()
        assert restarted.store.get('tasks',task_id)['status']=='success'
    finally:restarted.close()
