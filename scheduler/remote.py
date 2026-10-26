from __future__ import annotations

import glob
import json
import os
import re
import stat
import posixpath
import shlex
import threading
import time
from contextlib import nullcontext,contextmanager
from pathlib import Path

import keyring
import paramiko

from .files import confined, digest, manifest
from .operations import OperationCanceled
from .logs import tail_stream, chunk_stream

HOST_KEY_LOCK = threading.RLock()
CALL_CONTEXT = threading.local()
LOG_OPEN_TIMEOUT = 10


def read_ssh_config(path=None):
    path = Path(path or Path.home()/'.ssh'/'config').expanduser()
    def expand(current, stack):
        current = current.resolve()
        if current in stack or len(stack) > 12:
            raise ValueError('SSH Include 循环或过深')
        if not current.is_file():
            return ''
        lines = []
        for line in current.read_text(encoding='utf-8-sig').splitlines():
            fields = line.strip().split(None, 1)
            if fields and fields[0].lower() == 'include':
                for pattern in shlex.split(fields[1], posix=True):
                    p = Path(pattern).expanduser()
                    if not p.is_absolute():
                        p = current.parent / p
                    for entry in sorted(glob.glob(str(p))):
                        lines.append(expand(Path(entry), stack + [current]))
            else:
                lines.append(line)
        return '\n'.join(lines)
    config = paramiko.SSHConfig()
    import io
    config.parse(io.StringIO(expand(path, [])))
    hosts = []
    for alias in sorted(config.get_hostnames()):
        if any(c in alias for c in '*?!'):
            continue
        value = config.lookup(alias)
        hosts.append({'alias': alias, 'host': value.get('hostname', alias), 'username': value.get('user', ''), 'port': int(value.get('port',22)), 'identity_file': (value.get('identityfile') or [''])[0], 'proxy_jump': value.get('proxyjump',''), 'unsupported_proxy_command': bool(value.get('proxycommand'))})
    return config, hosts


class TrustFirst(paramiko.MissingHostKeyPolicy):
    def __init__(self, path, accept):
        self.path, self.accept = path, accept

    def missing_host_key(self, client, hostname, key):
        if not self.accept:
            import base64, hashlib
            fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
            raise ValueError('未知 SSH 主机指纹 SHA256:' + fingerprint + '；核实后启用首次信任或加入 known_hosts')
        with HOST_KEY_LOCK:
            # Merge keys saved by other concurrently connected hosts.
            client.load_host_keys(str(self.path))
            existing=client.get_host_keys().lookup(hostname)
            if existing and key.get_name() in existing and existing[key.get_name()]!=key:
                raise paramiko.BadHostKeyException(hostname,key,existing[key.get_name()])
            client.get_host_keys().add(hostname, key.get_name(), key)
            client.save_host_keys(str(self.path))


class Remote:
    def __init__(self, server, root, password=None):
        self.server, self.root = server, Path(root)
        self.lock = threading.RLock()
        self.clients = []
        self.sftp = None
        self.client = None
        self.password = password

    def connect(self,publish_helpers=True):
        self.close()
        config, _ = read_ssh_config()
        sock = None
        chain = [s.strip() for s in self.server.get('proxy_jump', '').split(',') if s.strip() and s != 'none']
        if len(chain) > 8:
            raise ValueError('跳板链过长')
        targets = []
        for alias in chain:
            user, _, hostport = alias.rpartition('@')
            hostport = hostport or alias
            host, sep, port = hostport.rpartition(':')
            host = host if sep else hostport
            value = config.lookup(host)
            if value.get('proxycommand'):
                raise ValueError('暂不执行 ProxyCommand，请使用 ProxyJump')
            targets.append({'host': value.get('hostname',host), 'username': user or value.get('user') or self.server['username'], 'port': int(port if sep else value.get('port',22)), 'identity_file': (value.get('identityfile') or [''])[0]})
        targets.append(self.server)
        password = self.password
        if password is None:
            try:
                password = keyring.get_password('project-compute', self.server['id'])
            except Exception:
                password = None
        known = self.root/'known_hosts'
        known.touch(exist_ok=True)
        try:
            for i, target in enumerate(targets):
                client = paramiko.SSHClient()
                client.load_system_host_keys()
                client.load_host_keys(str(known))
                client.set_missing_host_key_policy(TrustFirst(known, self.server.get('trust_new_host',False)))
                if i:
                    sock = self.clients[-1].get_transport().open_channel('direct-tcpip', (target['host'],target['port']), ('127.0.0.1',0), timeout=15)
                self.clients.append(client)
                client.connect(hostname=target['host'], port=target['port'], username=target['username'], key_filename=str(Path(target['identity_file']).expanduser()) if target.get('identity_file') else None, password=password if i==len(targets)-1 else None, passphrase=password if i==len(targets)-1 else None, sock=sock, timeout=15, banner_timeout=15, auth_timeout=15, allow_agent=True, look_for_keys=True)
                client.get_transport().set_keepalive(20)
            self.client = self.clients[-1]
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(120)
            home = self.sftp.normalize('.')
            self.agent_root = home+'/.project-compute'
            try:
                self.sftp.mkdir(self.agent_root, mode=0o700)
            except OSError:
                pass
            if publish_helpers:
                for file in ('agent.py','probe.py','object_agent.py'):
                    self.sftp.put(str(Path(__file__).parent/file), self.agent_root+'/'+file)
            self.server['work_root'] = self.server['work_root'].replace('~/', home+'/',1) if self.server['work_root'].startswith('~/') else self.server['work_root']
            self.server['data_root'] = self.server['data_root'].replace('~/', home+'/',1) if self.server['data_root'].startswith('~/') else self.server['data_root']
        except Exception:
            self.close()
            raise

    def close(self):
        if self.sftp:
            self.sftp.close()
        for client in reversed(self.clients):
            client.close()
        self.clients, self.client, self.sftp = [], None, None

    def call(self, action, payload=None, timeout=120):
        try:
            return self._call(action,payload,timeout)
        except (paramiko.SSHException,OSError,EOFError):
            with self.lock:
                transport=self.client.get_transport() if self.client else None
                if transport is None or not transport.is_active():self.close()
            raise

    def cancellable_call(self,action,payload=None,timeout=120,check=None):
        import uuid
        token=uuid.uuid4().hex if check and action.startswith('content_') and action!='content_stop' else None
        if token:payload={**(payload or {}),'content_token':token}
        previous=getattr(CALL_CONTEXT,'check',None)
        CALL_CONTEXT.check=check
        try:
            if check:check()
            result=self.call(action,payload,timeout)
            if check:check()
            return result
        except OperationCanceled:
            if token:
                CALL_CONTEXT.check=None
                try:self.call('content_stop',{'project_id':payload['project_id'],'content_token':token},timeout=10)
                except Exception:pass
            raise
        finally:
            CALL_CONTEXT.check=previous

    def log_path(self,spec):
        for field in ('project_id','attempt_id'):
            if not re.fullmatch(r'[a-f0-9]{32}',spec.get(field,'')):raise ValueError('日志运行标识无效')
        return posixpath.join(self.server['work_root'],'.compute-projects',spec['project_id'],'runs',spec['attempt_id'],'_control','run.log')

    def log_channel(self):
        # Open a dedicated SFTP channel without waiting for a long dataset hash
        # request holding the monitor/command lock on an existing connection.
        client=getattr(self,'client',None)
        if client is None and getattr(self,'sftp',None) is not None:return nullcontext(self.sftp)
        transport=client.get_transport() if client is not None else None
        if transport is None or not transport.is_active():
            if not self.lock.acquire(timeout=1):raise TimeoutError('SSH 正在连接或重连，日志将在下一轮重试')
            try:
                # Another request may have restored the transport while this
                # one waited. Do not close that fresh connection and its work.
                current=self.client.get_transport() if self.client else None
                if current is None or not current.is_active():self.connect()
            finally:self.lock.release()
            client=self.client
        session=client.get_transport().open_session(timeout=min(5,LOG_OPEN_TIMEOUT));session.settimeout(LOG_OPEN_TIMEOUT)
        guard=threading.Timer(LOG_OPEN_TIMEOUT,session.close);guard.daemon=True;guard.start()
        try:
            session.invoke_subsystem('sftp')
            return paramiko.SFTPClient(session)
        except Exception:
            session.close();raise
        finally:guard.cancel()

    @contextmanager
    def result_channel(self):
        if not getattr(self,'client',None):
            yield self.sftp;return
        # Each immutable object gets a private bulk transport. Monitor/launch
        # reconnects and another result's broken SSH session cannot abort it.
        transfer=Remote(self.server.copy(),self.root,self.password)
        try:
            transfer.connect(publish_helpers=False)
            transfer.sftp.get_channel().settimeout(60)
            yield transfer.sftp
        finally:transfer.close()

    def checked_log(self,channel,spec):
        path=self.log_path(spec)
        # Reject run/control/file symlinks; logs never follow an experiment's
        # link into files outside its managed run directory.
        targets=[path]
        for _ in range(4):targets.append(posixpath.dirname(targets[-1]))
        for target in targets:
            entry=channel.lstat(target)
            if stat.S_ISLNK(entry.st_mode):raise ValueError('日志路径含符号链接')
        entry=channel.lstat(path)
        if not stat.S_ISREG(entry.st_mode):raise ValueError('日志必须是普通文件')
        return path,entry

    def read_log(self,spec,mode='tail',offset=0,limit=128*1024):
        with self.log_channel() as channel:
            try:path,info=self.checked_log(channel,spec)
            except FileNotFoundError:return {'text':'','size':0,'lines':0,'has_more':False,'offset':0,'next_offset':0,'source':'server','exists':False}
            with channel.file(path,'rb') as stream:
                value=chunk_stream(stream,info.st_size,offset,limit) if mode=='full' else tail_stream(stream,info.st_size)
        return {**value,'modified_at':info.st_mtime,'source':'server','exists':True}

    def stream_log(self,spec):
        with self.log_channel() as channel:
            path,info=self.checked_log(channel,spec)
            with channel.file(path,'rb') as stream:
                left=info.st_size
                while left:
                    block=stream.read(min(128*1024,left))
                    if not block:break
                    left-=len(block);yield block

    def _call(self, action, payload=None, timeout=120):
        with self.lock:
            transport=self.client.get_transport() if self.client else None
            if transport is None or not transport.is_active():
                self.connect()
            spec = {k:self.server.get(k,'') for k in ('work_root','data_root','conda_executable','benchmark_env')}
            spec.update(payload or {})
            command = 'python3 '+shlex.quote(self.agent_root+'/agent.py')+' '+shlex.quote(action)
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            stdin.write(json.dumps(spec, ensure_ascii=False))
            stdin.flush()
            stdin.channel.shutdown_write()
        # SSH requests have independent channels. Release the connection lock
        # before waiting for a slow hash so resource probes can run concurrently.
        # Drain both streams while checking cancellation. Close this channel
        # only; other experiments and the shared SSH connection keep running.
        check=getattr(CALL_CONTEXT,'check',None)
        if check:
            channel=stdout.channel;chunks=[];errors=[];deadline=time.monotonic()+timeout
            try:
                while True:
                    check()
                    if time.monotonic()>deadline:raise TimeoutError('远程请求超时')
                    if channel.recv_ready():chunks.append(channel.recv(65536))
                    if channel.recv_stderr_ready():errors.append(channel.recv_stderr(65536))
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():break
                    time.sleep(.03)
                output=b''.join(chunks).decode('utf-8','replace');error=b''.join(errors).decode('utf-8','replace')
                channel.recv_exit_status()
            finally:
                channel.close()
        else:
            output = stdout.read().decode('utf-8','replace')
            error = stderr.read().decode('utf-8','replace')
            stdout.channel.recv_exit_status()
        try:
            value = json.loads(output)
        except ValueError:
            raise RuntimeError('远程响应无效：'+(error or output)[-1500:])
        if not value.get('ok'):
            raise RuntimeError(value.get('error','远程操作失败'))
        return value['result']

    def upload(self, local, spec, prefix, dataset=False, progress=None, inventory=None, cancel_check=None,file_event=None,existing_inventory=None,verify_after=True):
        # A dedicated SFTP channel lets a stalled transfer be interrupted without
        # disconnecting monitors or SSH requests belonging to other experiments.
        if not cancel_check or not getattr(self,'client',None):
            return self._upload(local,spec,prefix,dataset,progress,inventory,cancel_check=cancel_check,file_event=file_event,existing_inventory=existing_inventory,verify_after=verify_after)
        cancel_check()
        with self.lock:
            channel=self.client.open_sftp()
        channel.get_channel().settimeout(120)
        stopped=threading.Event()
        def watch():
            while not stopped.wait(.1):
                try:cancel_check()
                except OperationCanceled:
                    channel.get_channel().close()
                    return
        watcher=threading.Thread(target=watch,name='transfer-interrupt',daemon=True);watcher.start()
        try:
            return self._upload(local,spec,prefix,dataset,progress,inventory,transfer_sftp=channel,cancel_check=cancel_check,file_event=file_event,existing_inventory=existing_inventory,verify_after=verify_after)
        except Exception:
            cancel_check()
            raise
        finally:
            stopped.set();channel.close();watcher.join(timeout=1)

    def _upload(self, local, spec, prefix, dataset=False, progress=None, inventory=None, transfer_sftp=None, cancel_check=None,file_event=None,existing_inventory=None,verify_after=True):
        action = 'dataset_' if dataset else ''
        inventory = manifest(local) if inventory is None else inventory
        total = sum(v['size'] for v in inventory.values())
        done = 0
        def check():
            if cancel_check:cancel_check()
            if progress:progress(done,total)
        before = existing_inventory if existing_inventory is not None else self.cancellable_call(action+'manifest', {**spec, **({} if dataset else {'relative':prefix})},check=check)['files']
        for rel, item in inventory.items():
            if check:check()
            if before.get(rel,{}).get('sha256') != item['sha256']:
                remote_rel = rel if dataset else prefix+'/'+rel
                # Deterministic temporary names allow a retried immutable upload
                # to replace and finally remove the previous incomplete file.
                tmp_rel = remote_rel+'.upload-'+item['sha256']
                count=[0]
                def file_progress(amount,length):
                    if cancel_check:cancel_check()
                    count[0]=amount
                    if file_event:file_event(rel,'uploading',amount)
                    if progress:progress(done+amount,total)
                try:
                    if file_event:file_event(rel,'uploading',0)
                    dest = self.cancellable_call(action+'prepare', {**spec, 'relative':tmp_rel},check=check)['path']
                    with nullcontext() if transfer_sftp else self.lock:
                        (transfer_sftp or self.sftp).put(str(Path(local)/rel), dest, callback=file_progress if progress or file_event or cancel_check else None)
                    if file_event:file_event(rel,'verifying',item['size'])
                    self.cancellable_call(action+'commit', {**spec, 'relative':remote_rel, 'temporary':tmp_rel, 'sha256':item['sha256']},check=check)
                    if file_event:file_event(rel,'success',item['size'])
                except Exception as exc:
                    try:
                        if cancel_check:cancel_check()
                    except OperationCanceled as stopped:
                        if file_event:file_event(rel,'interrupted',count[0],str(stopped))
                        raise
                    if file_event:file_event(rel,'failed',count[0],str(exc))
                    if isinstance(exc,(paramiko.SSHException,OSError,EOFError)):self.close()
                    raise
            elif file_event:file_event(rel,'skipped',item['size'])
            done += item['size']
            if progress:
                progress(done, total)
        # Engine performs the final dataset snapshot once per shared group.
        if not verify_after:return {'verification_pending':True}
        after = self.cancellable_call(action+'manifest', {**spec, **({} if dataset else {'relative':prefix})},check=check)
        if any(after['files'].get(k,{}).get('sha256') != v['sha256'] for k,v in inventory.items()):
            raise RuntimeError('上传后校验失败')
        return after

    def download(self, target, spec, prefix):
        if not getattr(self,'client',None):return self._download(target,spec,prefix)
        # Like uploads and logs, result downloads use their own SFTP channel;
        # a large file must not hold up probes or experiment control signals.
        with self.log_channel() as channel:
            channel.get_channel().settimeout(120)
            return self._download(target,spec,prefix,transfer_sftp=channel)

    def _download(self, target, spec, prefix, transfer_sftp=None):
        before = self.call('manifest', {**spec, 'relative':prefix})
        if not before['exists']:
            raise RuntimeError('远程运行文件已被删除，禁止标记回传成功')
        if before['skipped']:
            raise RuntimeError('运行结果含符号链接或特殊文件，需处理后重试回传：'+', '.join(before['skipped'][:5]))
        base = self.call('project', spec)['path']+'/'+prefix
        current = manifest(target)
        import shutil, uuid
        backup = Path(target).parent / ('preserved-'+uuid.uuid4().hex)
        for rel in set(current)-set(before['files']):
            source = confined(target, rel)
            dest = confined(backup, rel)
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(source),str(dest))
        for rel,item in before['files'].items():
            dest = confined(target, rel)
            if current.get(rel,{}).get('sha256') != item['sha256']:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name+'.download')
                with nullcontext() if transfer_sftp else self.lock:
                    try:
                        (transfer_sftp or self.sftp).get(base+'/'+rel, str(tmp))
                    except (paramiko.SSHException,OSError,EOFError):
                        client=getattr(self,'client',None)
                        transport=client.get_transport() if client else None
                        if not transfer_sftp or transport is None or not transport.is_active():self.close()
                        raise
                if digest(tmp) != item['sha256']:
                    raise RuntimeError('回传 hash 不一致：'+rel)
                if dest.exists():
                    keep=confined(backup,rel)
                    keep.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(dest,keep)
                os.replace(tmp, dest)
        after = self.call('manifest', {**spec, 'relative':prefix})
        if before['files'] != after['files']:
            raise RuntimeError('回传期间远程文件发生变化，稍后重试')
        if manifest(target)!=before['files']:
            raise RuntimeError('本机回传目录校验不一致')
        return before

    def ensure_objects(self,pool,spec,inventory,check=None,progress=None,public=None):
        unique={v['sha256']:v for v in [*inventory.values(),*(public or {}).values()]}
        report=self.cancellable_call('content_need',{**spec,'objects':unique},check=check,timeout=86400)
        missing=report['missing'];total=sum(unique[h]['size'] for h in missing);done=0
        capacity=self.call('probe')
        mutable=sum(v['size'] for p,v in inventory.items() if not p.startswith('deps/'))+sum(v['size'] for v in (public or {}).values())
        if capacity['work_free_gb']*1024**3<total+2*mutable+self.server.get('min_disk_gb',5)*1024**3:
            raise ValueError('工作目录空间不足：新增缓存与独立代码工作区需要额外空间')
        if progress:progress(0,total,len(unique)-len(missing),len(unique))
        context=self.log_channel() if getattr(self,'client',None) else nullcontext(self.sftp)
        with context as channel:
            for sha in missing:
                if check:check()
                item=unique[sha];dest=self.cancellable_call('content_prepare',{**spec,'sha256':sha},check=check)['path']
                def callback(amount,length):
                    if check:check()
                    if progress:progress(done+amount,total,len(unique)-len(missing),len(unique))
                channel.put(str(pool.path(sha)),dest,callback=callback)
                if progress:progress(done+item['size'],total,len(unique)-len(missing),len(unique),'verifying')
                self.cancellable_call('content_commit',{**spec,'sha256':sha,'size':item['size']},check=check,timeout=86400)
                done+=item['size']
                if progress:progress(done,total,len(unique)-len(missing),len(unique))
        return {'uploaded_bytes':total,'reused_bytes':sum(v['size'] for v in unique.values())-total,'objects':len(unique),'missing_objects':len(missing)}

    def download_object(self,spec,sha,target,check=None,progress=None):
        path=self.cancellable_call('content_read',{**spec,'sha256':sha},check=check)['path']
        with self.result_channel() as channel:
            # A bulk file may take minutes. Limit outstanding prefetch requests
            # and preserve its immutable-hash partial across network failures.
            if getattr(self,'client',None):channel.get_channel().settimeout(60)
            target=Path(target)
            if target.is_symlink():raise ValueError('回传临时文件为链接')
            with channel.file(path,'rb') as source:
                size=channel.stat(path).st_size if hasattr(channel,'stat') else Path(path).stat().st_size
                offset=target.stat().st_size if target.exists() else 0
                if offset>size:offset=0
                if offset:
                    with target.open('rb') as partial:head=partial.read(min(offset,65536))
                    if source.read(len(head))!=head:offset=0
                resumed=offset
                source.seek(offset)
                if hasattr(source,'prefetch') and offset<size:source.prefetch(size,max_concurrent_requests=64)
                if progress:progress(offset,size)
                with target.open('ab' if offset else 'wb') as destination:
                    while offset<size:
                        if check:check()
                        block=source.read(min(256*1024,size-offset))
                        if not block:raise OSError('远程文件提前结束，已保留可继续的回传片段')
                        destination.write(block);offset+=len(block)
                        if progress:progress(offset,size)
                return {'downloaded_bytes':size-resumed,'resumed_bytes':resumed}
