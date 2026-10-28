from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scheduler.engine import Engine
from scheduler.files import confined, digest, manifest
from scheduler.remote import Remote


class LocalSFTP:
    def file(self,path,mode):return Path(path).open(mode)

    def lstat(self,path):return Path(path).lstat()
    def put(self,source,target,callback=None):
        Path(target).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
        if callback:
            callback(Path(source).stat().st_size,Path(source).stat().st_size)

    def get(self,source,target):
        shutil.copy2(source,target)


class FakeRemote(Remote):
    """Real transfer/hash code against isolated filesystem-backed SSH doubles."""
    launches=[]
    auto_execute=False
    roots={}
    work_roots={}
    def __init__(self,server,root,password=None):
        self.server=server
        self.root=Path(root)/'fake-servers'/server['id']
        self.data_base=Path(self.roots.get(server['id'],self.root/'datasets'))
        self.work_base=Path(self.work_roots.get(server['id'],self.root/'work'))
        self.lock=threading.RLock()
        self.sftp=LocalSFTP()
        self.root.mkdir(parents=True,exist_ok=True)
        self.data_base.mkdir(parents=True,exist_ok=True)
        self.work_base.mkdir(parents=True,exist_ok=True)
        self.fail_download=False
        self.offline=False

    def close(self):
        pass

    def project(self,spec):
        return self.work_base/'.compute-projects'/spec['project_id']

    def log_path(self,spec):
        if self.offline:raise OSError('host offline')
        return (self.project(spec)/'runs'/spec['attempt_id']/'_control'/'run.log').as_posix()

    def call(self,action,payload=None,timeout=120):
        if self.offline:
            raise OSError('host offline')
        spec=payload or {}
        if action.startswith('content_'):
            from scheduler import object_agent
            base=self.project(spec)
            if not base.is_dir():
                if action in ('content_replica','content_export'):return {'files':{},'exists':False,'skipped':[]}
                base.mkdir(parents=True,exist_ok=True)
            return object_agent.dispatch(action,spec,base,lambda value:self.call('status',value))
        if action=='conda_envs':
            return {'executable':'/opt/conda/bin/conda','environments':[{'name':'base','prefix':'/opt/conda'},
                    {'name':'pytorch','prefix':'/opt/conda/envs/pytorch'},
                    {'name':'research','prefix':'/opt/conda/envs/research'}]}
        if action=='storage_marker':
            base=self.work_base if spec['scope']=='work' else self.data_base
            p=base/('.compute-share-'+spec['token'])
            if spec.get('write'):p.write_text(spec['token'])
            if spec.get('remove'):p.unlink(missing_ok=True)
            return {'visible':p.exists() and p.read_text()==spec['token'],'root':str(base)}
        if action=='probe':
            return {'sampled_at':time.time(),'work_free_gb':100,'data_free_gb':100,'data_root':str(self.data_base),'gpus':[{'index':0,'uuid':'GPU-'+self.server['id'],'name':'Test GPU','is_idle':True,'memory_total_mb':24000,'memory_used_mb':0,'memory_free_mb':24000,'utilization_gpu':0}]}
        if action=='benchmark':
            return {'ok':True,'tensor_tflops':50}
        if action.startswith('dataset_'):
            if action=='dataset_marker':
                p=self.data_base/('.compute-share-'+spec['token'])
                if spec.get('write'):p.write_text(spec['token'])
                if spec.get('remove'):p.unlink(missing_ok=True)
                return {'visible':p.exists() and p.read_text()==spec['token']}
            ds=self.data_base/spec['name']
            if action=='dataset_exists':return {'exists':ds.is_dir(),'path':str(ds)}
            if action=='dataset_manifest':return {'files':manifest(ds),'exists':ds.exists(),'skipped':[]}
            if action in ('dataset_verify_snapshot','dataset_shared_verify','dataset_verify_clear'):
                marker=ds/('.compute-verify-'+spec['token'])
                if action=='dataset_verify_clear':marker.unlink(missing_ok=True);return {'removed':True}
                if action=='dataset_shared_verify' and (not marker.is_file() or marker.read_text()!=spec['token']):raise ValueError('shared marker not visible')
                metadata={p.relative_to(ds).as_posix():{'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in ds.rglob('*') if p.is_file() and not p.name.startswith('.compute-verify-')}
                result={'files':manifest(ds) if action=='dataset_verify_snapshot' else {},'metadata':metadata,'exists':ds.exists(),'skipped':[]}
                if action=='dataset_verify_snapshot':marker.write_text(spec['token'])
                return result
            if action=='dataset_prepare':
                p=confined(ds,spec['relative']);p.parent.mkdir(parents=True,exist_ok=True)
                return {'path':str(p)}
            if action=='dataset_commit':
                temp=confined(ds,spec['temporary']);target=confined(ds,spec['relative'])
                assert digest(temp)==spec['sha256'];os.replace(temp,target)
                return {'ok':True}
        base=self.project(spec)
        if action=='project':base.mkdir(parents=True,exist_ok=True);return {'path':str(base)}
        if action=='manifest':
            p=base/spec.get('relative','')
            return {'files':manifest(p),'exists':p.exists(),'skipped':[]}
        if action=='prepare':
            p=confined(base,spec['relative']);p.parent.mkdir(parents=True,exist_ok=True)
            return {'path':str(p)}
        if action=='commit':
            temp=confined(base,spec['temporary']);target=confined(base,spec['relative'])
            assert digest(temp)==spec['sha256'];os.replace(temp,target)
            return {'ok':True}
        if action=='clean':
            for control in base.glob('runs/*/_control/status.json'):
                if json.loads(control.read_text())['state'] in ('running','paused','lost','starting'):
                    raise ValueError('remote active')
            if base.exists():shutil.rmtree(base)
            return {'removed':True}
        run=base/'runs'/spec['attempt_id']
        ctrl=run/'_control'
        status=ctrl/'status.json'
        if action=='status':
            return json.loads(status.read_text()) if status.exists() else {'state':'absent'}
        if action=='interruption_status':
            value=json.loads(status.read_text()) if status.exists() else {'state':'absent'}
            return {**value,'confirmed_stopped':bool(value.get('confirmed_stopped') or value.get('interrupted'))}
        if action=='seal_interrupted':
            value=json.loads(status.read_text())
            if not value.get('confirmed_stopped') and not value.get('interrupted'):raise ValueError('not confirmed stopped')
            value.update(state='finished',interrupted=True,exit_code=None)
            status.write_text(json.dumps(value));(ctrl/'exit.json').write_text(json.dumps(value));return value
        if action=='discard_failed_workspace':
            value=json.loads(status.read_text())
            if value['state']!='finished' or not (value.get('interrupted') or value.get('exit_code')):raise ValueError('not a failed completed run')
            workspace=run/'workspace';size=sum(p.stat().st_size for p in workspace.rglob('*') if p.is_file()) if workspace.is_dir() else 0
            if workspace.is_dir():shutil.rmtree(workspace)
            return {'cleaned_at':time.time(),'removed_workspace_bytes':size,'shared_objects_preserved':True}
        if action=='launch':
            if status.exists():return json.loads(status.read_text())
            ctrl.mkdir(parents=True,exist_ok=True)
            self.launches.append((self.server['id'],spec['attempt_id']))
            if self.auto_execute:
                env=os.environ.copy();env.update(spec['env'])
                result=subprocess.run(spec['command'],shell=True,cwd=run/'workspace',env=env,capture_output=True)
                (ctrl/'run.log').write_bytes(result.stdout+result.stderr)
                value={'state':'finished','exit_code':result.returncode,'finished_at':time.time()}
            else:
                (ctrl/'run.log').write_text('experiment started')
                value={'state':'running'}
            status.write_text(json.dumps(value))
            return value
        if action=='log':return {'text':(ctrl/'run.log').read_text() if (ctrl/'run.log').exists() else ''}
        if action=='cancel':
            status.write_text(json.dumps({'state':'finished','exit_code':-15,'canceled':True,'finished_at':time.time()}))
            return {'state':'running'}
        if action in ('pause','resume'):
            value=json.loads(status.read_text());value.update(state='paused' if action=='pause' else 'running',paused=action=='pause');status.write_text(json.dumps(value));return value
        raise ValueError(action)

    def complete(self,task,exit_code=0,output='weights',log='done'):
        run=self.project(task)/'runs'/task['attempt_id']
        (run/'workspace'/'output.txt').write_text(output)
        (run/'_control'/'run.log').write_text(log)
        (run/'_control'/'status.json').write_text(json.dumps({'state':'finished','exit_code':exit_code,'finished_at':time.time()}))

    def download(self,*args,**kwargs):
        if self.fail_download:raise OSError('download disconnected')
        return super().download(*args,**kwargs)


@pytest.fixture
def engine(tmp_path):
    FakeRemote.launches=[];FakeRemote.roots={};FakeRemote.work_roots={};FakeRemote.auto_execute=False
    e=Engine(tmp_path,remote_factory=FakeRemote)
    e.settings['content_store_enabled']=False
    e.store.put('settings',e.settings)
    yield e
    e.close()


@pytest.fixture
def setup_project(engine):
    servers=[engine.save_server({'name':'server-'+str(i),'host':'fake-'+str(i),'username':'tester','work_root':'/work/compute','data_root':'/data/datasets'}) for i in range(2)]
    p=engine.create_project({'name':'Research'})
    (engine.local_project(p['id'])/'code'/'model.py').write_text('VERSION=1\n')
    return p,servers
