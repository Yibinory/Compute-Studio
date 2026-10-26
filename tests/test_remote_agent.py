import importlib.util
import json
import sys
from pathlib import Path

import pytest

from scheduler import probe
from scheduler.remote import read_ssh_config


@pytest.fixture
def agent(tmp_path,monkeypatch):
    monkeypatch.setitem(sys.modules,'probe',probe)
    spec=importlib.util.spec_from_file_location('test_agent',Path(__file__).parents[1]/'scheduler'/'agent.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'root_path',lambda raw:Path(raw).resolve())
    return module


def test_agent_ownership_and_cleanup_guard(agent,tmp_path):
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32}
    base=Path(agent.dispatch('project',spec)['path'])
    assert json.loads((base/'.owner.json').read_text())=={'project_id':'a'*32}
    ctrl=base/'runs'/('b'*32)/'_control';ctrl.mkdir(parents=True)
    (ctrl/'launch.lock').touch()
    with pytest.raises(ValueError,match='拒绝清理'):agent.dispatch('clean',spec)
    (ctrl/'exit.json').write_text('{"exit_code":0}')
    assert agent.dispatch('clean',spec)['removed']


def test_agent_refuses_unowned_project(agent,tmp_path):
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32}
    base=tmp_path/'work'/'.compute-projects'/('a'*32);base.mkdir(parents=True)
    (base/'foreign.txt').write_text('preserve me')
    with pytest.raises(ValueError,match='所有权'):agent.dispatch('clean',spec)
    assert (base/'foreign.txt').exists()


def test_agent_hash_commit(agent,tmp_path):
    import hashlib
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32}
    agent.dispatch('project',spec)
    temp=agent.dispatch('prepare',{**spec,'relative':'code/test.tmp'})['path'];Path(temp).write_bytes(b'contents')
    with pytest.raises(ValueError):agent.dispatch('commit',{**spec,'relative':'code/test.py','temporary':'code/test.tmp','sha256':'wrong'})
    agent.dispatch('commit',{**spec,'relative':'code/test.py','temporary':'code/test.tmp','sha256':hashlib.sha256(b'contents').hexdigest()})
    inv=agent.dispatch('manifest',spec)
    assert 'code/test.py' in inv['files']
    assert 'code/test.tmp' not in inv['files']


def test_ssh_include_wildcard_defaults_and_jump(tmp_path):
    included=tmp_path/'hosts.conf';included.write_text('Host gpu\n  HostName 10.0.0.9\n  User scientist\n  ProxyJump gateway\n')
    config=tmp_path/'config';config.write_text('Include hosts.conf\nHost *\n  Port 2200\n  User default\nHost gateway\n  HostName 10.0.0.1\n')
    _,hosts=read_ssh_config(config)
    gpu=next(h for h in hosts if h['alias']=='gpu')
    assert gpu['username']=='scientist' and gpu['port']==2200 and gpu['proxy_jump']=='gateway'
    assert len(hosts)==2


def test_ssh_include_loop(tmp_path):
    config=tmp_path/'config';config.write_text('Include config\n')
    with pytest.raises(ValueError,match='循环'):read_ssh_config(config)


def test_missing_pid_token_is_not_treated_as_alive(agent,tmp_path):
    spec={'work_root':str(tmp_path/'work'),'project_id':'a'*32,'attempt_id':'b'*32}
    base=Path(agent.dispatch('project',spec)['path'])
    ctrl=base/'runs'/spec['attempt_id']/'_control';ctrl.mkdir(parents=True)
    (ctrl/'process.json').write_text(json.dumps({'pid':999999999,'token':None,'supervisor_pid':999999998,'supervisor_token':'missing'}))
    assert agent.dispatch('status',spec)['state']=='lost'


def test_family_includes_session_groups_and_adopted_detached_children(agent,monkeypatch):
    monkeypatch.setattr(agent.os,'getpid',lambda:900)
    def row(parent,group,session,token,state='S'):return {'ppid':parent,'pgid':group,'sid':session,'token':token,'state':state}
    table={100:row(900,100,100,'root'),900:row(1,900,900,'super'),101:row(100,101,100,'worker'),102:row(900,102,102,'detached'),103:row(102,102,102,'grandchild'),777:row(1,777,777,'unrelated')}
    identity={'pid':100,'token':'root','supervisor_pid':900,'supervisor_token':'super'};tracked={}
    assert set(agent.family_processes(identity,tracked,table))=={100,101,102,103}
    del table[100];table[101]['ppid']=1;table[102]['ppid']=1
    assert set(agent.family_processes(identity,tracked,table))=={101,102,103}
    table[101]=row(1,777,777,'reused')
    assert set(agent.family_processes(identity,tracked,table))=={102,103}


def test_storage_markers_and_resource_disks(agent,tmp_path,monkeypatch):
    spec={'work_root':str(tmp_path/'work'),'data_root':str(tmp_path/'data'),'token':'d'*32}
    for scope in ('work','data'):
        value=agent.dispatch('storage_marker',{**spec,'scope':scope,'write':True})
        assert value['visible'] and value['root']==spec[scope+'_root']
        assert agent.dispatch('storage_marker',{**spec,'scope':scope})['visible']
        agent.dispatch('storage_marker',{**spec,'scope':scope,'remove':True})
        assert not agent.dispatch('storage_marker',{**spec,'scope':scope})['visible']
    with pytest.raises(ValueError):agent.dispatch('storage_marker',{**spec,'scope':'work','token':'../../escape'})
    monkeypatch.setattr(agent.probe,'status',lambda:{'gpus':[]})
    value=agent.dispatch('probe',spec)
    for key in ('work_disk','data_disk'):
        assert value[key]['total_bytes']>0 and value[key]['free_bytes']>=0
        assert 0<=value[key]['used_percent']<=100


def test_benchmark_binds_physical_uuid(agent,monkeypatch):
    import os
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES','old-mask')
    seen=[]
    monkeypatch.setattr(agent.probe,'benchmark',lambda index,seconds:seen.append((index,seconds,os.environ['CUDA_VISIBLE_DEVICES'])) or {'ok':True})
    assert agent.dispatch('benchmark',{'gpu_index':3,'gpu_uuid':'GPU-physical'})['ok']
    assert seen==[(0,5,'GPU-physical')]


def test_dataset_shared_snapshot_proves_subdirectory_and_does_not_read_content(agent,tmp_path,monkeypatch):
    root=tmp_path/'data';ds=root/'sample';ds.mkdir(parents=True)
    (ds/'a.bin').write_bytes(b'x'*1000);token='c'*32
    spec={'data_root':str(root),'name':'sample','token':token}
    snapshot=agent.dispatch('dataset_verify_snapshot',spec)
    assert snapshot['files']['a.bin']['size']==1000 and len(snapshot['files']['a.bin']['sha256'])==64
    original=Path.open
    def open_file(path,*args,**kwargs):
        if path.name=='a.bin' and 'r' in (args[0] if args else kwargs.get('mode','r')):raise AssertionError('Shared verification must not read file content')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_file)
    peer=agent.dispatch('dataset_shared_verify',spec)
    assert peer['metadata']==snapshot['metadata'] and peer['files']=={'a.bin':{'size':1000}}
    changed=tmp_path/'other'/'sample';changed.mkdir(parents=True);(changed/'a.bin').write_bytes(b'x'*1000)
    with pytest.raises(ValueError,match='共享数据目录'):agent.dispatch('dataset_shared_verify',{**spec,'data_root':str(changed.parent)})
    assert agent.dispatch('dataset_verify_clear',spec)['removed']
    assert not list(ds.glob('.compute-verify-*'))
    with pytest.raises(ValueError):agent.dispatch('dataset_verify_clear',{**spec,'token':'../escape'})
