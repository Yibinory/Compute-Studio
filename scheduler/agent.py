#!/usr/bin/env python3
"""Uploaded SSH helper. No service, root privileges or inbound ports required."""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import probe
try:import object_agent
except ModuleNotFoundError:
    from scheduler import object_agent


def atomic(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value), encoding='utf-8')
    os.replace(tmp, path)


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def conda_binary(spec):
    configured=spec.get('conda_executable','').strip()
    candidates=[configured,os.environ.get('CONDA_EXE',''),shutil.which('conda') or '']
    candidates += [str(Path.home()/folder/'bin'/'conda') for folder in ('miniconda3','anaconda3','miniforge3','mambaforge')]
    candidates += ['/opt/conda/bin/conda','/opt/miniconda3/bin/conda','/opt/anaconda3/bin/conda']
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            return str(Path(candidate).expanduser())
    raise ValueError('未找到 Conda，请在服务器配置指定 conda 可执行文件路径')


def conda_target(value):
    if value.startswith('/'):
        return ['-p',value]
    if value.startswith('~/'):
        return ['-p',str(Path(value).expanduser())]
    return ['-n',value]


def conda_environments(spec):
    binary=conda_binary(spec)
    proc=subprocess.run([binary,'env','list','--json'],capture_output=True,text=True,timeout=25)
    if proc.returncode:
        raise ValueError((proc.stderr or proc.stdout)[-1500:] or '读取 Conda 环境失败')
    data=json.loads(proc.stdout)
    base=PurePosixPath(binary).parent.parent
    entries=[]
    for prefix in dict.fromkeys(data.get('envs',[])):
        path=PurePosixPath(prefix)
        if path.is_absolute():
            entries.append({'name':'base' if path==base else path.name,'prefix':str(path)})
    return {'executable':binary,'environments':sorted(entries,key=lambda e:(e['name']!='base',e['name'].casefold(),e['prefix']))}


def root_path(raw):
    path = Path(raw).expanduser()
    if not path.is_absolute() or str(path.resolve()) in ('/', '/home', '/tmp', '/usr', '/var', '/etc', '/root'):
        raise ValueError('请配置具体的绝对目录，禁止系统根目录')
    return path.resolve()


def project_path(spec, create=False):
    root = root_path(spec['work_root'])
    project_id = spec['project_id']
    if len(project_id) != 32 or any(c not in '0123456789abcdef' for c in project_id):
        raise ValueError('非法项目 ID')
    base = root / '.compute-projects'
    path = base / project_id
    if base.is_symlink() or path.is_symlink() or path.resolve().parent != base.resolve():
        raise ValueError('项目路径越界或为符号链接')
    marker = path / '.owner.json'
    if path.exists():
        if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != {'project_id': project_id}:
            raise ValueError('目录所有权不匹配，拒绝操作')
    elif create:
        path.mkdir(parents=True)
        atomic(marker, {'project_id': project_id})
    return path


def safe_child(base, rel):
    p = Path(rel)
    if p.is_absolute() or '..' in p.parts or '\\' in rel or '\x00' in rel or not rel:
        raise ValueError('非法相对路径')
    dest = base / p
    if base.resolve() not in dest.resolve().parents:
        raise ValueError('路径越界')
    for part in (dest, *dest.parents):
        if part == base:
            break
        if part.is_symlink():
            raise ValueError('符号链接不允许写入')
    return dest


def tree_manifest(base, hashes=True, snapshot=False):
    out, skipped, metadata = {}, [], {}
    if not base.exists():
        return {'files': out, 'skipped': skipped, 'exists': False}
    for current, dirs, files in os.walk(base, followlinks=False):
        for item in list(dirs):
            p = Path(current) / item
            if p.is_symlink():
                skipped.append(str(p.relative_to(base)))
                dirs.remove(item)
            elif item in ('.git', '.venv', '__pycache__', 'node_modules'):
                dirs.remove(item)
        for item in files:
            p = Path(current) / item
            if re.fullmatch(r'\.compute-verify-[a-f0-9]{32}',item) and not p.is_symlink() and p.stat().st_size==32 and p.read_text()==item[-32:]:
                continue
            if p.is_symlink():
                skipped.append(str(p.relative_to(base)))
                continue
            if not p.is_file():
                skipped.append(str(p.relative_to(base)))
                continue
            before=p.stat();key=p.relative_to(base).as_posix()
            value={'size':before.st_size}
            if hashes:
                h = hashlib.sha256()
                with p.open('rb') as f:
                    for chunk in iter(lambda: f.read(1024*1024), b''):
                        h.update(chunk)
                after=p.stat()
                if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise ValueError('核验期间文件发生变化：'+key)
                value['sha256']=h.hexdigest()
            out[key]=value
            metadata[key]={'size':before.st_size,'mtime_ns':before.st_mtime_ns}
    result={'files': out, 'skipped': skipped, 'exists': True}
    if snapshot:result['metadata']=metadata
    return result


def process_token(pid):
    try:
        raw = Path('/proc/%d/stat' % pid).read_text()
        fields = raw[raw.rfind(')')+2:].split()
        if fields[0] == 'Z':
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def process_table():
    """Linux start-time identities; zombies keep ancestry but are not live."""
    result={}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            raw=(path/'stat').read_text();fields=raw[raw.rfind(')')+2:].split()
            result[int(path.name)]={'state':fields[0],'ppid':int(fields[1]),'pgid':int(fields[2]),'sid':int(fields[3]),'token':fields[19]}
        except (OSError,IndexError,ValueError):continue
    return result


def family_processes(identity,tracked=None,table=None):
    table=process_table() if table is None else table
    tracked={} if tracked is None else tracked
    roots=set()
    for pid_key,token_key in (('pid','token'),('supervisor_pid','supervisor_token')):
        pid=identity.get(pid_key);token=identity.get(token_key)
        if token and table.get(pid,{}).get('token')==token:roots.add(pid)
    members={pid for pid,token in tracked.items() if token and table.get(pid,{}).get('token')==token}
    leader=identity.get('pid');leader_row=table.get(leader)
    # A reused session leader PID must not bind an unrelated new session.
    if roots and (leader_row is None or leader_row['token']==identity.get('token')):
        members.update(pid for pid,row in table.items() if row['sid']==leader)
    ancestors=roots|members
    while True:
        descendants={pid for pid,row in table.items() if row['ppid'] in ancestors}
        added=descendants-ancestors
        if not added:break
        ancestors.update(added);members.update(added)
    members.discard(identity.get('supervisor_pid'));members.discard(os.getpid())
    tracked.update({pid:table[pid]['token'] for pid in members})
    return {pid:table[pid] for pid in members if table[pid]['state']!='Z'}


def signal_family(members,sig):
    """Signal every owned group, including job-control/setsid children."""
    groups={row['pgid'] for row in members.values()}
    for pgid in groups:
        if pgid<=1 or pgid==os.getpgrp():continue
        valid=any(row['pgid']==pgid and process_token(pid)==row['token'] for pid,row in members.items())
        if valid:
            try:os.killpg(pgid,sig)
            except ProcessLookupError:pass
    # A child could move groups between the snapshot and killpg. Verify each
    # PID's start time again and signal it as well; never signal the supervisor.
    for pid,row in members.items():
        if pid>1 and process_token(pid)==row['token']:
            try:os.kill(pid,sig)
            except ProcessLookupError:pass


def enable_subreaper():
    if sys.platform!='linux':return False
    try:
        return ctypes.CDLL(None,use_errno=True).prctl(36,ctypes.c_ulong(1),0,0,0)==0
    except (AttributeError,OSError,TypeError):return False


def run_dir(spec):
    project = project_path(spec)
    attempt = spec['attempt_id']
    if len(attempt) != 32 or any(c not in '0123456789abcdef' for c in attempt):
        raise ValueError('非法运行 ID')
    return safe_child(project, 'runs/' + attempt)


def run_status(spec):
    run = run_dir(spec)
    ctrl = run / '_control'
    if ctrl.is_symlink():
        raise ValueError('控制目录异常')
    if (ctrl / 'exit.json').exists():
        return {'state': 'finished', **json.loads((ctrl/'exit.json').read_text())}
    if (ctrl / 'process.json').exists():
        value = json.loads((ctrl/'process.json').read_text())
        alive = bool(value['supervisor_token']) and process_token(value['supervisor_pid']) == value['supervisor_token']
        child_alive = bool(value['token']) and process_token(value['pid']) == value['token']
        state=('paused' if value.get('paused') else 'running') if alive or child_alive else 'lost'
        return {**value,'state':state}
    if (ctrl / 'launch.lock').exists():
        return {'state': 'starting' if time.time()-(ctrl/'launch.lock').stat().st_mtime < 60 else 'lost'}
    return {'state': 'absent'}


def supervise(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    run = run_dir(spec)
    ctrl, cwd = run / '_control', run / 'workspace'
    env = os.environ.copy()
    env.update(spec['env'])
    proc=None;identity=None;tracked={}
    try:
        subreaper=enable_subreaper()
        if (ctrl/'cancel').exists():
            atomic(ctrl/'exit.json',{'exit_code':143,'finished_at':time.time(),'canceled':True})
            return
        command = ['bash', '-lc', spec['command']]
        if spec.get('conda_env'):
            # A login shell can reset Conda's PATH; retain the environment conda run supplies.
            command = [conda_binary(spec), 'run', '--no-capture-output', *conda_target(spec['conda_env']), 'bash', '-c', spec['command']]
        with (ctrl/'run.log').open('ab', buffering=0) as log:
            proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=log, stderr=log, start_new_session=True)
            identity={'pid':proc.pid,'token':process_token(proc.pid),'supervisor_pid':os.getpid(),'supervisor_token':process_token(os.getpid()),'started_at':time.time(),'control_version':2,'paused':False,'subreaper':subreaper}
            atomic(ctrl/'process.json',identity)
            cancel_at=None;paused=False;terminated={}
            while True:
                proc.poll()
                table=process_table();members=family_processes(identity,tracked,table)
                # Reap adopted grandchildren without consuming Popen's child.
                for pid,row in table.items():
                    if pid!=proc.pid and row['ppid']==os.getpid() and row['state']=='Z':
                        try:os.waitpid(pid,os.WNOHANG)
                        except ChildProcessError:pass
                if (ctrl/'cancel').exists():
                    if cancel_at is None:
                        cancel_at=time.monotonic()
                    # Continue stopped processes so pending TERM can be handled.
                    if time.monotonic()-cancel_at<10:
                        fresh={pid:row for pid,row in members.items() if terminated.get(pid)!=row['token']}
                        signal_family(fresh,signal.SIGTERM)
                        terminated.update({pid:row['token'] for pid,row in fresh.items()})
                    else:signal_family(members,signal.SIGKILL)
                    signal_family(members,signal.SIGCONT);paused=False
                else:
                    desired=(ctrl/'pause').exists()
                    if desired:
                        signal_family(members,signal.SIGSTOP)
                    elif paused:
                        signal_family(members,signal.SIGCONT)
                    paused=desired
                if identity['paused']!=paused:
                    identity.update(paused=paused,control_updated_at=time.time());atomic(ctrl/'process.json',identity)
                # Parent exit never proves that its workers have stopped.
                if proc.returncode is not None and not members:break
                time.sleep(.25)
            atomic(ctrl/'exit.json', {'exit_code':proc.returncode,'finished_at':time.time(),'canceled':cancel_at is not None or (ctrl/'cancel').exists()})
    except Exception as exc:
        # Fail closed: do not publish completion while owned workers survive.
        if identity:
            signal_family(family_processes(identity,tracked),signal.SIGKILL)
            if proc:proc.wait(timeout=5)
            until=time.monotonic()+5
            while family_processes(identity,tracked) and time.monotonic()<until:time.sleep(.1)
            if family_processes(identity,tracked):
                atomic(ctrl/'supervisor-error.json',{'error':str(exc),'time':time.time()});return
        atomic(ctrl/'exit.json', {'exit_code':111,'finished_at':time.time(),'error':str(exc),'canceled':(ctrl/'cancel').exists()})


def control_run(action,spec):
    status=run_status(spec);ctrl=run_dir(spec)/'_control'
    if status['state']=='lost':raise ValueError('进程状态丢失，请人工核实后处理')
    if status['state'] in ('absent','finished'):return status
    if action=='cancel':
        (ctrl/'cancel').touch()
        if status.get('control_version')==2 and process_token(status.get('supervisor_pid',0))==status.get('supervisor_token'):
            return run_status(spec)
        # Upgrade compatibility or a dead supervisor: enforce TERM/KILL here,
        # keeping the captured identities after the root shell exits.
        tracked={};terminated={};members=family_processes(status,tracked);start=time.monotonic()
        while members:
            if time.monotonic()-start<10:
                fresh={pid:row for pid,row in members.items() if terminated.get(pid)!=row['token']}
                signal_family(fresh,signal.SIGTERM);terminated.update({pid:row['token'] for pid,row in fresh.items()})
            else:signal_family(members,signal.SIGKILL)
            signal_family(members,signal.SIGCONT)
            if time.monotonic()-start>15:raise ValueError('仍有实验子进程未终止，请继续核验；不会标记完成')
            time.sleep(.25);members=family_processes(status,tracked)
        previous=json.loads((ctrl/'exit.json').read_text()) if (ctrl/'exit.json').exists() else {'exit_code':143}
        atomic(ctrl/'exit.json',{**previous,'finished_at':time.time(),'canceled':True})
        return run_status(spec)
    if (ctrl/'cancel').exists():raise ValueError('实验正在中断，不能暂停或继续')
    desired=action=='pause'
    if desired:(ctrl/'pause').touch()
    else:(ctrl/'pause').unlink(missing_ok=True)
    if status.get('control_version')!=2:
        signal_family(family_processes(status),signal.SIGSTOP if desired else signal.SIGCONT)
        identity=json.loads((ctrl/'process.json').read_text());identity.update(paused=desired)
        atomic(ctrl/'process.json',identity)
        return run_status(spec)
    until=time.monotonic()+4
    while time.monotonic()<until:
        current=run_status(spec)
        if current['state']=='finished' or current.get('paused')==desired:return current
        time.sleep(.1)
    raise ValueError('暂停/继续尚未确认，请稍后查看状态')


def dispatch(action, spec):
    if action == 'conda_envs':
        return conda_environments(spec)
    if action == 'storage_marker':
        scope = spec.get('scope')
        token = spec.get('token', '')
        if scope not in ('work', 'data') or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise ValueError('存储探测参数无效')
        base = root_path(spec[scope+'_root'])
        base.mkdir(parents=True, exist_ok=True)
        marker = safe_child(base, '.compute-share-'+token)
        if spec.get('write'):
            with marker.open('x', encoding='utf-8') as stream:
                stream.write(token)
        elif spec.get('remove'):
            marker.unlink(missing_ok=True)
        return {'visible': marker.is_file() and marker.read_text() == token, 'root': str(base)}
    if action == 'probe':
        value = probe.status()
        work = root_path(spec['work_root'])
        work.mkdir(parents=True, exist_ok=True)
        data = root_path(spec['data_root'])
        data.mkdir(parents=True, exist_ok=True)
        value.update(work_free_gb=shutil.disk_usage(work).free/1024**3, data_free_gb=shutil.disk_usage(data).free/1024**3, sampled_at=time.time(), work_root=str(work), data_root=str(data))
        for key, path in [('work_disk', work), ('data_disk', data)]:
            disk = shutil.disk_usage(path)
            value[key] = {'path': str(path), 'total_bytes': disk.total, 'used_bytes': disk.used, 'free_bytes': disk.free, 'used_percent': round(disk.used/disk.total*100, 1) if disk.total else 0}
        return value
    if action == 'benchmark':
        benchmark_index = spec['gpu_index']
        if spec.get('gpu_uuid'):
            os.environ['CUDA_VISIBLE_DEVICES'] = spec['gpu_uuid']
            benchmark_index = 0
        if spec.get('benchmark_env'):
            proc=subprocess.run([conda_binary(spec),'run','--no-capture-output',*conda_target(spec['benchmark_env']),'python',str(Path(__file__).parent/'probe.py'),'benchmark','--gpu-index',str(benchmark_index),'--seconds','5'],capture_output=True,text=True,timeout=150)
            if proc.returncode!=0:
                raise ValueError(proc.stderr[-1500:] or proc.stdout[-1500:])
            return json.loads(proc.stdout.strip().splitlines()[-1])
        return probe.benchmark(benchmark_index, 5)
    if action == 'project':
        return {'path': str(project_path(spec, True))}
    if action.startswith('content_'):
        base=project_path(spec,action in ('content_need','content_prepare','content_commit','content_assemble') and not spec.get('dry_run'))
        if not base.is_dir():
            if action=='content_need' and spec.get('dry_run'):return {'missing':list(spec['objects']),'available':[]}
            if action in ('content_replica','content_export'):return {'files':{},'exists':False,'skipped':[]}
            if action in ('content_scan','content_stats'):return {'objects':0,'bytes':0,'invalid':[],'checked_at':time.time()}
            raise ValueError('项目目录不存在')
        return object_agent.dispatch(action,spec,base,run_status)
    if action == 'manifest':
        project = project_path(spec)
        base = project
        if spec.get('relative'):
            base = safe_child(base, spec['relative'])
        if (project/'objects').is_dir():return object_agent.manifest_cached(project,base)
        return tree_manifest(base)
    if action == 'prepare':
        base = project_path(spec, True)
        path = safe_child(base, spec['relative'])
        path.parent.mkdir(parents=True, exist_ok=True)
        return {'path': str(path)}
    if action == 'commit':
        base = project_path(spec)
        tmp = safe_child(base, spec['temporary'])
        dest = safe_child(base, spec['relative'])
        if file_hash(tmp) != spec['sha256']:
            raise ValueError('上传 hash 不一致')
        os.replace(tmp, dest)
        return {'ok': True}
    if action == 'launch':
        run = run_dir(spec)
        ctrl = run / '_control'
        ctrl.mkdir(parents=True, exist_ok=True)
        if run_status(spec)['state'] != 'absent':
            return run_status(spec)
        # Idempotent launch prevents duplicate jobs after an ambiguous SSH response.
        fd = os.open(str(ctrl/'launch.lock'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        atomic(ctrl/'spec.json', spec)
        with open(os.devnull, 'wb') as null:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'supervise', str(ctrl/'spec.json')], stdin=subprocess.DEVNULL, stdout=null, stderr=null, start_new_session=True)
        return {'state': 'starting'}
    if action == 'status':
        return run_status(spec)
    if action in ('cancel','pause','resume'):
        return control_run(action,spec)
    if action == 'log':
        path = run_dir(spec)/'_control'/'run.log'
        if not path.exists():
            return {'text': ''}
        with path.open('rb') as f:
            f.seek(max(0, path.stat().st_size - min(spec.get('bytes', 100000), 2000000)))
            return {'text': f.read().decode('utf-8', 'replace')}
    if action == 'clean':
        path = project_path(spec)
        if not path.exists():
            return {'removed': False}
        # Remote guard is independent of the controller's database.
        for run in (path/'runs').glob('*'):
            if run.is_symlink():
                raise ValueError('运行目录含符号链接')
            status = run_status({**spec, 'attempt_id': run.name})
            if status['state'] in ('running','paused','starting','lost'):
                raise ValueError('远程仍有运行或未知状态任务，拒绝清理')
        # Managed dependency views are read-only during execution. Make only
        # owned directories writable for removal; never follow directory links.
        for current,dirs,files in os.walk(path,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(current)/d).is_symlink()]
            os.chmod(current,0o755)
        shutil.rmtree(path)
        return {'removed': True}
    if action.startswith('dataset_'):
        base = root_path(spec['data_root'])
        base.mkdir(parents=True, exist_ok=True)
        if action == 'dataset_marker':
            marker = safe_child(base, '.compute-share-' + spec['token'])
            if spec.get('write'):
                marker.write_text(spec['token'])
            elif spec.get('remove'):
                marker.unlink(missing_ok=True)
            return {'visible': marker.is_file() and marker.read_text() == spec['token']}
        dataset = safe_child(base, spec['name'])
        if action == 'dataset_exists':
            return {'exists': dataset.is_dir(), 'path':str(dataset)}
        if action == 'dataset_manifest':
            return tree_manifest(dataset)
        if action in ('dataset_verify_snapshot','dataset_shared_verify','dataset_verify_clear'):
            token=spec.get('token','')
            if not re.fullmatch(r'[a-f0-9]{32}',token):raise ValueError('核验标识无效')
            marker=safe_child(dataset,'.compute-verify-'+token)
            if action=='dataset_verify_clear':
                if marker.is_file() and marker.read_text()==token:marker.unlink()
                return {'removed':not marker.exists()}
            if action=='dataset_verify_snapshot':
                result=tree_manifest(dataset,snapshot=True)
                if result['exists']:
                    with marker.open('x') as stream:stream.write(token)
                return result
            if not marker.is_file() or marker.read_text()!=token:
                raise ValueError('共享数据目录未能看到本次核验标记，请检查挂载或重新探测共享关系')
            result=tree_manifest(dataset,hashes=False,snapshot=True)
            if not marker.is_file() or marker.read_text()!=token:raise ValueError('共享数据目录在核验期间发生变化')
            return result
        if action == 'dataset_prepare':
            path = safe_child(base, spec['name']+'/'+spec['relative'])
            path.parent.mkdir(parents=True, exist_ok=True)
            return {'path': str(path)}
        if action == 'dataset_commit':
            tmp = safe_child(base, spec['name']+'/'+spec['temporary'])
            dest = safe_child(base, spec['name']+'/'+spec['relative'])
            if file_hash(tmp) != spec['sha256']:
                raise ValueError('数据集上传 hash 不一致')
            os.replace(tmp, dest)
            return {'ok': True}
    raise ValueError('未知操作')


if __name__ == '__main__':
    if sys.argv[1] == 'supervise':
        supervise(sys.argv[2])
    else:
        try:
            result = dispatch(sys.argv[1], json.load(sys.stdin))
            print(json.dumps({'ok': True, 'result': result}))
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}))
            sys.exit(1)
