from __future__ import annotations

import json
import math
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import keyring

from .files import copy_tree, manifest, name, relative
from .policy import ACTIVE, choose, dependencies_ready, dependency_reason
from .remote import Remote
from .storage import Store, uid
from .sharing import detect, configuration
from .operations import OperationCanceled, measured
from .environments import environment, signature as conda_signature, resolve as resolve_environment
from .transfer_journal import TransferJournal
from .file_process import run as file_process

DEFAULTS = {'id':'global', 'paused':False, 'poll_seconds':5, 'history_samples':6, 'stale_seconds':30, 'zero_memory_reservation_mb':1024, 'cleanup_check_seconds':3600,'content_store_enabled':True,'cache_scan_hours':24}


class Engine:
    def __init__(self, root, remote_factory=Remote):
        self.store = Store(root)
        self.transfers = TransferJournal(self.store)
        self.root = self.store.root
        self.remote_factory = remote_factory
        self.remotes = {}
        self.remote_lock = threading.RLock()
        self.passwords = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.jobs = ThreadPoolExecutor(max_workers=2, thread_name_prefix='operation')
        self.controls = ThreadPoolExecutor(max_workers=4, thread_name_prefix='experiment-control')
        self.batch_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='bulk-control')
        self.pollers = ThreadPoolExecutor(max_workers=8, thread_name_prefix='monitor')
        self.runtime_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix='experiment-runtime')
        self.runtime_pending = {}
        self.probe_pending = {}
        self.probe_started = {}
        self.probe_configs = {}
        self.control_locks = {}
        self.project_storage_busy=set()
        self.content_locks={}
        self.background_runtime = False
        self.wake_event = threading.Event()
        self.last_cleanup = 0
        self.storage_moving = False
        self.uploads = None
        self.close_handlers = []
        self.cancel_checks = {}
        self.cancel_signals = {}
        self.thread = None
        try:
            self.settings = {**DEFAULTS, **self.store.get('settings','global')}
        except ValueError:
            self.settings = dict(DEFAULTS)
        self.store.put('settings',self.settings)
        from .source_sync import CodeSources
        self.code_sources=CodeSources(self)
        for server in self.store.all('servers'):
            server.update(connected=False, history={})
            self.store.put('servers', server)
        # Keep existing verified groups on upgrade; future connection changes
        # use the expanded signature without forcing another remote probe now.
        for value in self.store.all('storage'):
            scope=value.get('scope')
            if scope not in ('work','data'):
                continue
            current=configuration(self.store.all('servers'),scope)
            previous=value.get('configuration')
            if previous=={sid:fields[:4] for sid,fields in current.items()}:
                value['configuration']=current
                self.store.put('storage',value)
        for item in self.store.active_summaries('jobs'):
            job=self.store.get('jobs',item['id'])
            if job['status'] in ('running','queued'):
                job.update(status='failed', error='控制器重启中断了此操作，请重试；已上传文件会按 hash 跳过')
                self.store.put('jobs',job)

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self.loop, name='scheduler', daemon=True)
        self.background_runtime = True
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        self.runtime_workers.shutdown(wait=True)
        self.code_sources.close()
        self.jobs.shutdown(wait=True)
        self.batch_workers.shutdown(wait=True)
        self.controls.shutdown(wait=True)
        self.pollers.shutdown(wait=True)
        for remote in self.remotes.values():
            remote.close()
        for handler in self.close_handlers:
            handler()
        self.store.close()

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.store.event('调度循环异常：'+str(exc), 'error')
            self.wake_event.wait(min(self.settings['poll_seconds'],1) if self.background_runtime else self.settings['poll_seconds'])
            self.wake_event.clear()

    def enqueue(self, label, function, *args):
        if self.storage_moving:
            raise ValueError('本机存储正在迁移，请稍后操作')
        operation=function.__name__+':'+json.dumps(args,ensure_ascii=False)
        for existing in self.store.active_summaries('jobs'):
            if existing.get('operation')==operation and existing['status'] in ('queued','running') and not existing.get('cancel_requested'):
                return existing
        job = {'id':uid(), 'label':label, 'operation':operation, 'status':'queued', 'created_at':time.time(), 'progress':{}, 'cancelable':function.__name__ in ('process','upload_dataset','verify_dataset','migrate','preview','compact_content','scan_content_cache','batch_state_action','synchronize','synchronize_now')}
        self.store.put('jobs',job)
        self.cancel_signals[job['id']]=threading.Event()
        def work():
            try:
                with self.store.lock:
                    self.check_cancel(job)
                    job.update(status='running')
                    self.store.put('jobs',job)
                result = function(*args, job=job)
                with self.store.lock:
                    self.check_cancel(job)
                    job.update(status='success', result=result, finished_at=time.time())
                    self.store.put('jobs',job)
            except OperationCanceled as exc:
                self.transfers.finish_pending(job,'interrupted',str(exc))
                job.update(status='canceled', error=str(exc), finished_at=time.time())
                self.store.put('jobs',job)
            except Exception as exc:
                self.transfers.finish_pending(job,'failed',str(exc))
                job.update(status='failed', error=str(exc), finished_at=time.time())
                self.store.event(label+'：'+str(exc),'error')
                self.store.put('jobs',job)
            finally:
                self.cancel_signals.pop(job['id'],None)
                self.cancel_checks.pop(job['id'],None)
        executor=self.code_sources.worker if function.__name__ in ('synchronize','synchronize_now') else self.controls if function.__name__ in ('perform_control','perform_control_request') or function.__name__=='task_action' and len(args)>1 and args[1] in ('cancel','pause','resume') else self.jobs
        if function.__name__=='batch_state_action':executor=self.batch_workers
        executor.submit(work)
        return job

    def batch_state_action(self,task_ids,action,job=None):
        from .batch_states import apply
        return apply(self,task_ids,action,job)

    def check_cancel(self,job):
        if self.stop_event.is_set():raise OperationCanceled('控制器正在关闭，停止后台操作')
        if job:
            signal=self.cancel_signals.get(job['id'])
            if job.get('cancel_requested') or signal and signal.is_set():
                raise OperationCanceled('用户已中断；已接收及已上传文件保留，可以继续传输')
            now=time.monotonic()
            if now-self.cancel_checks.get(job['id'],0)<.2:return
            self.cancel_checks[job['id']]=now
            current=self.store.get('jobs',job['id'])
            if current.get('cancel_requested'):
                job['cancel_requested']=True
                raise OperationCanceled('用户已中断；已接收及已上传文件保留，可以继续传输')

    def cancel_job(self,job_id):
        with self.store.lock:
            job=self.store.get('jobs',job_id)
            if job['status'] not in ('queued','running'):
                return job
            if not job.get('cancelable'):
                raise ValueError('此操作不支持中断')
            if job.get('progress',{}).get('phase') in ('publishing','switching','complete'):
                raise ValueError('正在提交文件或切换目录，请等待此步骤完成')
            job['cancel_requested']=True
            self.store.put('jobs',job)
            signal=self.cancel_signals.get(job_id)
            if signal:signal.set()
            return job

    def progress(self,job,**value):
        if job:
            with self.store.lock:
                self.check_cancel(job)
                job['progress']=measured(job.get('progress',{}),value)
                self.store.put('jobs',job)

    def local_project(self, project_id):
        self.store.get('projects', project_id)
        path = self.root/'projects'/project_id
        path.mkdir(parents=True,exist_ok=True)
        return path

    def remote(self, server):
        key = server['id']
        with self.remote_lock:
            if key not in self.remotes:
                self.remotes[key] = self.remote_factory(server.copy(), self.root, self.passwords.get(key))
            return self.remotes[key]

    def save_server(self, data):
        with self.lock:
            key = data.get('id') or uid()
            old = self.store.get('servers',key) if data.get('id') else {}
            server = {**old, 'id':key}
            for field, default in [('name',''),('host',''),('username',''),('identity_file',''),('proxy_jump',''),('work_root',''),('data_root',''),('conda_executable',''),('benchmark_env','')]:
                server[field] = str(data.get(field,old.get(field,default))).strip()
            server['default_conda_env']=environment(data.get('default_conda_env',old.get('default_conda_env','')))
            server['benchmark_env']=environment(server['benchmark_env'])
            if not server['host'] or not server['username']:
                raise ValueError('主机和用户名必填')
            for field in ('work_root','data_root'):
                path = server[field]
                if not (path.startswith('/') or path.startswith('~/')) or '..' in path.split('/') or path.rstrip('/') in ('','/','/home','/tmp','/usr','/etc','/root','/var'):
                    raise ValueError('运行和数据目录必须是具体的 Linux 绝对目录或 ~/ 子目录')
            server['name'] = server['name'] or server['host']
            server['port'] = int(data.get('port',old.get('port',22)))
            if not 1 <= server['port'] <= 65535:
                raise ValueError('端口范围为 1–65535')
            for field, default in [('enabled',True),('paused',False),('trust_new_host',False)]:
                server[field] = bool(data.get(field,old.get(field,default)))
            server['min_disk_gb'] = self.numeric(data.get('min_disk_gb',old.get('min_disk_gb',5)),0,100000)
            server['retention_days'] = self.numeric(data.get('retention_days',old.get('retention_days',7)),0,36500)
            server.setdefault('blocked_gpus',[])
            server.setdefault('benchmarks',{})
            connection_fields = ('host','port','username','identity_file','proxy_jump','work_root','data_root','trust_new_host','conda_executable','benchmark_env')
            changed = any(old.get(f) != server[f] for f in connection_fields)
            if changed and any(t['status'] in ACTIVE and t.get('server_id')==key for t in self.store.all('tasks')):
                raise ValueError('服务器有运行或尚未回传的任务，不能修改连接和目录')
            secret = data.get('password')
            if secret:
                server['credential_revision']=uid()
                self.passwords[key] = secret
                if data.get('remember_password'):
                    try:
                        keyring.set_password('project-compute',key,secret)
                    except Exception as exc:
                        raise ValueError('系统密钥环不可用；取消“记住密码”后可使用当前会话凭证') from exc
                changed = True
            if changed:
                if key in self.remotes:
                    self.remotes.pop(key).close()
                server.update(connected=False, history={}, error='等待连接')
            self.store.put('servers',server)
            self.store.event('保存服务器：'+server['name'])
            return server

    @staticmethod
    def numeric(value, minimum, maximum, integer=False):
        try:
            if isinstance(value,bool):raise ValueError()
            number = float(value)
        except (TypeError,ValueError,OverflowError):raise ValueError('请输入有效数值') from None
        if not math.isfinite(number) or not minimum <= number <= maximum or integer and not number.is_integer():
            raise ValueError('数值超出允许范围')
        return int(number) if integer else number

    def create_project(self, data):
        with self.lock:
            project = {'id':uid(), 'name':name(data['name']), 'description':str(data.get('description',''))[:4000], 'created_at':time.time(), 'server_conda_envs':{}}
            self.store.put('projects',project)
            (self.local_project(project['id'])/'code').mkdir(parents=True,exist_ok=True)
            return project

    def archive_project(self,project_id,archived):
        if type(archived) is not bool:raise ValueError('归档状态必须为 true 或 false')
        with self.store.lock:
            project=self.store.get('projects',project_id)
            if bool(project.get('archived',False))==archived:return project
            project['archived']=archived
            if archived:project['archived_at']=time.time()
            else:project.pop('archived_at',None)
            self.store.put('projects',project)
            self.store.event(('归档项目：' if archived else '取消项目归档：')+project['name'])
            return project

    def project_environments(self,project_id,data):
        with self.lock:
            project=self.store.get('projects',project_id)
            mapping=data.get('server_conda_envs',{})
            if not isinstance(mapping,dict):raise ValueError('项目环境配置必须按服务器保存')
            values={}
            for sid,value in mapping.items():
                self.store.get('servers',sid)
                value=environment(value)
                if value:values[sid]=value
            project['server_conda_envs']=values
            self.store.put('projects',project)
            self.store.event('更新项目默认环境：'+project['name'])
            return project

    def current_conda(self,server):
        try:value=self.store.get('conda',server['id'])
        except ValueError:return {'status':'unknown','environments':[]}
        if value.get('configuration')!=conda_signature(server):
            return {'status':'unknown','environments':[]}
        return {k:v for k,v in value.items() if k!='configuration'}

    def discover_conda(self,server_id,job=None):
        server=self.store.get('servers',server_id)
        value={'id':server_id,'configuration':conda_signature(server),'checked_at':time.time()}
        try:
            if self.settings.get('demo'):
                result={'executable':'/opt/miniconda3/bin/conda','environments':[
                    {'name':n,'prefix':'/opt/miniconda3'+('' if n=='base' else '/envs/'+n)}
                    for n in ('base','pytorch','research')]}
            else:result=self.remote(server).call('conda_envs',timeout=45)
            value.update(result,status='ready')
        except Exception as exc:
            value.update(status='failed',environments=[],error=str(exc))
            self.store.put('conda',value)
            raise
        self.store.put('conda',value)
        return self.current_conda(self.store.get('servers',server_id))

    def create_task(self, project_id, data, existing=None, persist=True, known_tasks=None, task_id=None):
        with self.lock:
            self.store.get('projects',project_id)
            command = str(data.get('command','')).strip()
            if not command or len(command)>16000:
                raise ValueError('执行命令必填（最长 16000 字符）')
            tasks = self.store.all('tasks') if known_tasks is None else known_tasks
            deps = list(dict.fromkeys(data.get('depends_on',[])))
            for key in deps:
                dep = next((t for t in tasks if t['id']==key),None)
                if dep is None:raise ValueError('前序任务不存在：'+str(key))
                if dep['project_id']!=project_id:
                    raise ValueError('前序任务必须属于同一项目')
                if dep.get('archived') and not (dep['status']=='success' and dep.get('synced_at')):
                    raise ValueError('前序实验 '+dep['name']+' 已归档且未完成，请先恢复归档或重新选择前序')
            if existing:
                index={t['id']:t for t in tasks}
                stack=list(deps)
                visited=set()
                while stack:
                    current=stack.pop()
                    if current==existing['id']:
                        raise ValueError('任务依赖不能成环')
                    if current not in visited:
                        visited.add(current)
                        stack.extend(index[current].get('depends_on',[]))
            servers = list(dict.fromkeys(data.get('server_ids',[])))
            for key in servers:
                self.store.get('servers',key)
            datasets = [name(n) for n in data.get('datasets',[])]
            level = data.get('level','idle_only')
            if level not in ('idle_only','low_interference','emergency'):
                raise ValueError('未知调度级别')
            conda_env=environment(data.get('conda_env',''))
            task = {'id':uid(), 'project_id':project_id, 'name':name(data['name']), 'command':command, 'depends_on':deps, 'server_ids':servers, 'datasets':datasets, 'conda_env':conda_env, 'gpu_name':str(data.get('gpu_name','')).strip(), 'priority':self.numeric(data.get('priority',50),1,999,True), 'memory_mb':self.numeric(data.get('memory_mb',0),0,1000000,True), 'max_utilization':self.numeric(data.get('max_utilization',30),0,100), 'level':level, 'max_retries':self.numeric(data.get('max_retries',2),0,20,True), 'attempts':0, 'status':'draft', 'created_at':time.time(), 'seq':max([t.get('seq',0) for t in tasks]+[0])+1}
            overrides=data.get('server_conda_envs',{})
            if not isinstance(overrides,dict):raise ValueError('实验服务器环境须为映射')
            for sid in overrides:self.store.get('servers',sid)
            task['server_conda_envs']={sid:environment(value) for sid,value in overrides.items()}
            from .content import patterns,object_files
            task['follow_project_files']=patterns(data.get('follow_project_files',[]))
            task['object_files']=object_files(data.get('object_files',{}))
            for field in ('code_files','code_exclude','output_files'):
                if field in data:task[field]=patterns(data[field]) if data[field] is not None else None
            dependencies=data.get('dependency_files',{})
            if not isinstance(dependencies,dict) or set(dependencies)-set(deps):raise ValueError('前序文件选择只能引用该实验的直接依赖')
            task['dependency_files']={key:patterns(value) for key,value in dependencies.items()}
            if existing:
                task.update({k:existing[k] for k in ('id','seq','created_at','attempts','status')})
                task={**existing,**task}
                task['reason']=''
            elif task_id:task['id']=task_id
            if not persist:return task
            with self.store.lock:
                from .task_archive import check_dependencies
                check_dependencies(self,task)
                self.store.put('tasks',task)
            (self.local_project(project_id)/'tasks'/task['id']/'files').mkdir(parents=True,exist_ok=True)
            return task

    def update_task(self, task_id, data):
        with self.lock,self.store.lock:
            existing=self.store.get('tasks',task_id)
            if existing.get('archived'):raise ValueError('实验已归档，请先恢复归档后再编辑')
            if existing['status'] not in ('draft','queued','waiting','blocked','paused'):
                raise ValueError('仅未执行的任务可编辑；已运行任务请新建以保留可追溯性')
            return self.create_task(existing['project_id'],{**existing,**data},existing=existing)

    def task_action(self, task_id, action, job=None):
        with self.store.lock:
            if self.store.get_summary('tasks',task_id).get('archived'):
                raise ValueError('实验已归档，请先恢复归档后再操作')
        if action=='sync':
            with self.store.lock:
                task=self.store.get('tasks',task_id)
                if task['status'] not in ('sync_failed','syncing'):raise ValueError('该任务不需要重试回传')
                task.update(status='syncing',reason='回传重试已提交，后台正在处理')
                self.store.put('tasks',task)
            self.submit_runtime(task,self.finish)
            return self.store.get('tasks',task_id)
        if action in ('pause','cancel'):
            with self.store.lock:
                pending=self.store.get('tasks',task_id)
                if pending.get('archived'):raise ValueError('实验已归档，请先恢复归档后再操作')
                if pending['status'] in ('draft','queued','waiting','blocked','paused','canceled'):
                    if action=='pause':
                        if pending['status']=='canceled':raise ValueError('已中断实验不能暂停，请重新执行')
                        pending.update(status='paused',reason='用户暂停排队')
                    else:pending.update(status='canceled',cancel_requested=True,finished_at=time.time(),reason='用户中断，尚未开始运行')
                    self.store.put('tasks',pending);return pending
        current=self.store.get('tasks',task_id)
        if (action in ('cancel','pause') and current['status'] in ('staging','launching','running','suspended','canceling') or action=='resume' and current['status']=='suspended'):
            return self.control_task(task_id,action)
        with self.store.lock:
            task = self.store.get('tasks',task_id)
            if task.get('archived'):raise ValueError('实验已归档，请先恢复归档后再操作')
            if action in ('queue','resume','retry'):
                if task['status'] in ACTIVE:
                    raise ValueError('任务仍在执行或等待回传，不能重复提交')
                if any(task_id in t.get('depends_on',[]) and (t['status'] in ACTIVE or t['status']=='success') for t in self.store.all('tasks')):
                    raise ValueError('后续任务正在执行或已经成功，请新建任务以保持依赖结果可追溯')
                if task['status']=='success' and action!='retry':
                    raise ValueError('已成功任务请使用重试操作')
                if task.get('attempt_id'):
                    task['previous_attempts']=task.get('previous_attempts',[])+[{'attempt_id':task['attempt_id'],'server_id':task.get('server_id'),'exit_code':task.get('exit_code'),'synced_at':task.get('synced_at'),'resolved_conda_env':task.get('resolved_conda_env'),'conda_env_source':task.get('conda_env_source')}]
                task.update(status='queued', reason='', synced_at=None, server_id=None, attempt_id=None, cancel_requested=False, canceled=False, attempts=0)
                task.pop('resolved_conda_env',None);task.pop('conda_env_source',None)
                for field in ('control_request','control_request_id','control_requested_at','paused_at'):task.pop(field,None)
                task.update(exit_code=None,gpu_uuid=None,gpu_index=None,started_at=None,finished_at=None,remote_state={})
                task.pop('runtime_progress',None)
                for field in ('input_manifest','result_manifest','preparation_retry_at','preparation_failures','retry_snapshot_attempt','input_revision','code_revision'):task.pop(field,None)
            elif action=='pause':
                if task['status'] not in ('draft','queued','waiting','blocked'):
                    raise ValueError('此状态不支持暂停，请刷新实验状态')
                task.update(status='paused',reason='用户暂停')
            elif action=='cancel':
                if task['status'] in ('staging','running','launching','suspended','canceling'):
                    self.wake_event.set()
                    return self.prepare_control(task_id,action)
                elif task['status'] in ACTIVE:
                    raise ValueError('任务正在准备、回传或状态未知，请等待或重试回传')
                else:
                    task.update(status='canceled',reason='用户取消')
            else:
                raise ValueError('未知任务操作')
            self.store.put('tasks',task)
            self.wake_event.set()
            return task

    def control_task(self,task_id,action):
        task=self.prepare_control(task_id,action)
        return self.perform_control(task,action)

    def prepare_control(self,task_id,action):
        # The scheduler can be uploading/recovering under its lock for minutes.
        # Persist intent and send controls without waiting for that lock or the
        # general file-operation pool. Subsequent runtime writes merge intent.
        with self.store.lock:
            task=self.store.get('tasks',task_id)
            allowed={'cancel':('staging','launching','running','suspended','canceling'),'pause':('running','suspended'),'resume':('suspended',)}
            if task['status'] not in allowed[action]:raise ValueError('此状态不支持该操作，请刷新实验状态')
            if action!='cancel' and task.get('cancel_requested'):raise ValueError('实验正在中断，不能暂停或继续')
            if action=='pause' and task['status']=='suspended':return task
            request_id=uid();task.update(control_request=action,control_request_id=request_id,control_requested_at=time.time())
            if action=='cancel':
                task.update(cancel_requested=True,reason='中断请求已保存，正在停止整个实验')
                if task['status']!='staging':task['status']='canceling'
            else:task['reason']='正在'+('暂停整个实验' if action=='pause' else '继续原实验')
            self.store.put('tasks',task)
            return task

    def perform_control(self,task,action,job=None):
        with self.store.lock:
            lock=self.control_locks.setdefault(task['id'],threading.RLock())
        with lock:return self.perform_control_locked(task,action)

    def perform_control_request(self,task_id,action,request_id,job=None):
        task=self.store.get('tasks',task_id)
        if task.get('control_request_id')!=request_id:return self.store.summary('tasks',task)
        return self.store.summary('tasks',self.perform_control(task,action))

    def perform_control_locked(self,task,action):
        if task['status']=='staging' or action=='pause' and task['status']=='suspended':return task
        task_id=task['id'];request_id=task['control_request_id']
        latest=self.store.get('tasks',task_id)
        if latest.get('control_request_id')!=request_id or latest.get('attempt_id')!=task.get('attempt_id'):return latest
        server=self.store.get('servers',task['server_id'])
        try:result=self.remote(server).call(action,self.spec(task),timeout=30)
        except Exception as exc:
            with self.store.lock:
                latest=self.store.get('tasks',task_id)
                if latest.get('control_request_id')==request_id:
                    latest['reason']=('中断请求已保存，远程尚未确认：' if action=='cancel' else '暂停/继续尚未确认：')+str(exc)
                    self.store.put('tasks',latest)
            raise
        with self.store.lock:
            latest=self.store.get('tasks',task_id)
            if latest.get('attempt_id')!=task['attempt_id']:raise ValueError('实验运行记录已改变，请刷新')
            if latest.get('control_request_id')!=request_id or latest['status'] in ('syncing','sync_failed','success','failed','canceled'):return latest
            if action=='cancel':latest.update(status='canceling',reason='正在终止整个实验及子进程，随后回传文件')
            elif result['state']=='paused':latest.update(status='suspended',paused_at=time.time(),reason='运行已暂停；GPU 和显存仍保留，继续会从原位置运行')
            elif result['state']=='running':latest.update(status='running',reason='')
            elif result['state']!='finished':raise ValueError('远程尚未确认操作，请稍后检查状态')
            latest['remote_state']=result;self.store.put('tasks',latest)
            self.store.event(('中断整个实验' if action=='cancel' else '暂停运行' if action=='pause' else '继续运行')+'：'+latest['name'])
            return latest

    def put_runtime_task(self,task,expected_attempt=...):
        with self.store.lock:
            latest=self.store.get('tasks',task['id'])
            expected=task.get('attempt_id') if expected_attempt is ... else expected_attempt
            if latest.get('attempt_id')!=expected:
                raise OperationCanceled('运行记录已更新，忽略旧运行的后台结果')
            if latest.get('attempt_id')==expected:
                request_changed=latest.get('control_request_id')!=task.get('control_request_id')
                for field in ('cancel_requested','control_request','control_request_id','control_requested_at'):
                    if field in latest:task[field]=latest[field]
                if task['status']=='running':
                    if task.get('cancel_requested'):task.update(status='canceling',reason='正在中断整个实验')
                    elif latest['status']=='suspended' and latest.get('control_request')=='pause':task.update(status='suspended',reason=latest['reason'])
                if request_changed and task['status'] in ('running','suspended') and latest['status'] in ('running','suspended','canceling'):
                    task.update(status=latest['status'],reason=latest.get('reason',''))
            return self.store.put('tasks',task)

    def check_task_cancel(self,task):
        if self.stop_event.is_set():raise OperationCanceled('控制器正在关闭，停止实验准备')
        latest=self.store.get_summary('tasks',task['id'])
        if task.get('attempt_id') and latest.get('attempt_id')!=task['attempt_id']:
            raise OperationCanceled('运行记录已更新，停止旧实验准备')
        if latest.get('cancel_requested') or latest['status'] in ('canceled','paused'):
            raise OperationCanceled('用户已中断实验准备；代码与已传文件保留')

    def runtime_progress(self,task,phase,label,**details):
        task.update(reason=label,runtime_progress={'phase':phase,'label':label,'updated_at':time.time(),**details})
        self.put_runtime_task(task)

    def reserve_task(self,task,server,gpu):
        with self.store.lock:
            latest=self.store.get('tasks',task['id'])
            if latest.get('archived') or latest['status'] not in ('draft','queued','waiting','blocked'):raise ValueError('实验状态已改变或已归档，不能启动')
            latest.update(status='staging',server_id=server['id'],gpu_uuid=gpu.get('uuid') or str(gpu['index']),
                          gpu_index=gpu['index'],gpu_name_assigned=gpu.get('name',''),attempt_id=uid(),preparation_started_at=time.time(),reason='准备实验输入')
            for field in ('input_manifest','result_manifest','runtime_progress','preparation_retry_at'):latest.pop(field,None)
            self.store.put('tasks',latest)
            return latest

    def submit_runtime(self,task,function,*args):
        with self.store.lock:
            if task['id'] in self.runtime_pending:return
            if not self.background_runtime:
                # Direct ticks used by CLI/tests retain deterministic completion.
                pass
            else:
                kind='recover_preparation' if function.__name__=='recover_one' and task['status']=='staging' else function.__name__
                self.runtime_pending[task['id']]={'kind':kind,'project_id':task['project_id'],'preparation_order':(task.get('preparation_started_at',task.get('created_at',0)),task.get('seq',0),task['id'])}
                def work():
                    try:function(task,*args)
                    except Exception as exc:self.store.event('实验后台处理：'+task['name']+'：'+str(exc),'error')
                    finally:
                        with self.store.lock:self.runtime_pending.pop(task['id'],None)
                        self.wake_event.set()
                self.runtime_workers.submit(work);return
        function(task,*args)

    def launch_reserved(self,task,server,gpu,resume=False):
        attempt=task['attempt_id']
        try:self.launch(task,server,gpu,resume=resume)
        except Exception as exc:
            with self.store.lock:
                latest=self.store.get('tasks',task['id'])
                if latest.get('attempt_id')!=attempt:return
                if self.stop_event.is_set() and not latest.get('cancel_requested'):
                    latest.update(reason='控制器已停止，准备状态与运行记录保留，重启后恢复')
                elif isinstance(exc,OperationCanceled):latest.update(status='canceled',cancel_requested=True,finished_at=time.time(),reason=str(exc))
                elif latest['status']=='staging':
                    failures=latest.get('preparation_failures',0)+1;delay=min(120,10*2**min(failures-1,4))
                    latest.update(status='waiting',reason='准备失败：'+str(exc)+'；约 '+str(delay)+' 秒后重新检查，也可修改配置或手动重新执行',server_id=None,attempt_id=None,runtime_progress={},preparation_failures=failures,preparation_retry_at=time.time()+delay)
                else:latest.update(reason='启动结果待核实：'+str(exc))
                self.store.put('tasks',latest)
            self.store.event('任务 '+task['name']+'：'+str(exc),'error')

    def spec(self, task):
        return {'project_id':task['project_id'], 'attempt_id':task['attempt_id']}

    def poll_one(self, server):
        try:
            snapshot = self.remote(server).call('probe')
            snapshot['remote_sampled_at']=snapshot.get('sampled_at')
            snapshot['sampled_at']=time.time()
            return server['id'], snapshot, None
        except Exception as exc:
            return server['id'], None, str(exc)

    @staticmethod
    def probe_configuration(server):
        return [server.get(k) for k in ('host','port','username','identity_file','proxy_jump','trust_new_host','credential_revision','work_root','data_root')]

    def tick(self):
        if self.storage_moving or self.stop_event.is_set():return
        servers=[s for s in self.store.all('servers') if s.get('enabled')]
        if self.background_runtime:
            completed=[]
            for server in servers:
                key=server['id'];future=self.probe_pending.get(key)
                if future is None and time.monotonic()-self.probe_started.get(key,-1000)>=self.settings['poll_seconds']:
                    self.probe_started[key]=time.monotonic();self.probe_configs[key]=self.probe_configuration(server);self.probe_pending[key]=self.pollers.submit(self.poll_one,server)
                elif future is not None and future.done():completed.append((*future.result(),self.probe_configs.pop(key,None)));self.probe_pending.pop(key,None)
        else:
            completed=[(*f.result(),self.probe_configuration(server)) for server,f in [(s,self.pollers.submit(self.poll_one,s)) for s in servers]]
        for key,snapshot,error,configuration in completed:
            with self.store.lock:
                server=self.store.get('servers',key);previous_error=server.get('error')
                if self.probe_configuration(server)!=configuration:continue
                server.update(connected=not error,error=error or '')
                if snapshot:
                    server['snapshot']=snapshot;history=server.setdefault('history',{})
                    for gpu in snapshot.get('gpus',[]):
                        gkey=gpu.get('uuid') or str(gpu['index'])
                        history[gkey]=(history.get(gkey,[])+[gpu])[-self.settings['history_samples']:]
                elif error!=previous_error:self.store.event('连接 '+server['name']+'：'+error,'warning')
                self.store.put('servers',server)
        self.recover()
        if self.background_runtime:self.code_sources.schedule()
        if not self.settings.get('paused'):
            queued=sorted([t for t in self.store.summaries('tasks') if not t.get('archived') and t['status'] in ('queued','waiting','blocked')],key=lambda t:(t['priority'],t['seq']))
            for task in queued:
                with self.lock,self.store.lock:
                    if self.settings.get('paused') or self.stop_event.is_set():break
                    task=self.store.get('tasks',task['id'])
                    previous=(task['status'],task.get('reason'))
                    if task.get('archived') or task['status'] not in ('queued','waiting','blocked'):continue
                    if task.get('preparation_retry_at',0)>time.time():continue
                    if task['project_id'] in self.project_storage_busy:
                        task.update(status='blocked',reason='等待项目存储整理完成')
                    elif task['id'] in self.runtime_pending:
                        task.update(reason='等待上次实验后台收尾完成')
                    elif self.uploads and self.uploads.pending(task['project_id'],task['id']):
                        task.update(status='blocked',reason='等待输入文件上传及后台导入完成；导入失败时请重试或取消上传')
                    elif not dependencies_ready(task,self.store.summaries('tasks')):
                        task.update(status='blocked',reason=dependency_reason(task,self.store.summaries('tasks')))
                    else:
                        if self.background_runtime and (len(self.runtime_pending)>=8 or sum(v['kind'] in ('launch_reserved','recover_preparation') for v in self.runtime_pending.values())>=4 or any(v['kind'] in ('launch_reserved','recover_preparation') and v['project_id']==task['project_id'] for v in self.runtime_pending.values())):
                            task.update(status='waiting',reason='等待实验准备工作线程；已有实验正在后台传输或核验')
                        else:
                            assignment=choose(task,self.store.all('servers'),self.store.summaries('tasks'),self.settings)
                            if assignment:
                                reserved=self.reserve_task(task,*assignment)
                                task=reserved
                                if self.background_runtime:self.submit_runtime(reserved,self.launch_reserved,*assignment)
                            else:task.update(status='waiting',reason='等待符合显存、利用率、服务器范围和磁盘门槛的 GPU')
                    if task['status'] in ('queued','waiting','blocked') and previous!=(task['status'],task.get('reason')):self.store.put('tasks',task)
                if 'reserved' in locals():
                    if not self.background_runtime:self.launch_reserved(reserved,*assignment)
                    del reserved
        if self.stop_event.is_set():return
        if time.time()-self.last_cleanup>self.settings['cleanup_check_seconds']:
            self.last_cleanup=time.time()
            if self.background_runtime:self.enqueue('自动清理闲置项目副本',self.auto_cleanup)
            else:self.auto_cleanup()
        if self.thread and any(s.get('connected') for s in self.store.all('servers')):
            for scope in ('work','data'):
                if not self.current_sharing(scope):self.enqueue('自动核验'+('工作' if scope=='work' else '数据')+'目录共享',self.ensure_storage,scope)
            if time.time()-getattr(self,'last_cache_check',0)>60:
                self.last_cache_check=time.time()
                if self.settings.get('content_store_enabled',True):self.schedule_content_scans()

    def launch(self, task, server, gpu, resume=False):
        self.check_task_cancel(task)
        if self.uploads and self.uploads.pending(task['project_id'],task['id']):
            raise ValueError('任务输入正在上传或导入，等待后台完成')
        if task.get('status')!='staging' or not task.get('attempt_id'):
            task=self.reserve_task(task,server,gpu)
        self.runtime_progress(task,'validating','检查数据集与执行环境')
        remote = self.remote(server)
        selected_environment=resolve_environment(task,self.store.get('projects',task['project_id']),server)
        # Validate configured datasets on the selected host before reserving a GPU.
        dataset_paths = {}
        for dataset in task['datasets']:
            ready = remote.call('dataset_exists',{'name':dataset})
            if not ready['exists']:
                raise ValueError('目标服务器缺少数据集：'+dataset)
            local_datasets = [d for d in self.store.all('datasets') if d['name']==dataset and d.get('import_status')!='canceled']
            if local_datasets and local_datasets[0].get('servers',{}).get(server['id'],{}).get('status')!='success':
                raise ValueError('数据集还未在目标服务器完成上传和校验：'+dataset)
            dataset_paths[dataset] = ready['path']
        self.check_task_cancel(task)
        if self.settings.get('content_store_enabled',True) or task.get('storage_version')==2 or task.get('follow_project_files') or self.code_sources.bound(task['project_id']):
            self.prepare_content(task,server,remote,resume)
        else:
            project = self.local_project(task['project_id'])
            stage = project/'runs'/task['attempt_id']/'input'
            check=lambda:self.check_task_cancel(task)
            if resume and 'input_manifest' in task:
                # Keep the immutable input and attempt directory after restart so
                # completed remote files and deterministic partial uploads are reused.
                self.runtime_progress(task,'hashing','核验保留的实验输入，继续未完成的传输')
                if not stage.is_dir() or manifest(stage,check=check)!=task['input_manifest']:
                    raise ValueError('中断前的本机输入快照被修改或删除，请核对后重新执行')
            else:
                self.runtime_progress(task,'assembling','在本机整理公共代码、任务文件和前序结果')
                stage.mkdir(parents=True,exist_ok=True)
                if (project/'code'/'deps').exists():raise ValueError('deps 是保留目录，不能作为项目或任务输入')
                copy_tree(project/'code',stage,check=check)
                self.check_task_cancel(task)
                task_files = project/'tasks'/task['id']/'files'
                if (stage/'deps').exists() or (task_files/'deps').exists():
                    raise ValueError('deps 是保留目录，不能作为项目或任务输入')
                copy_tree(task_files,stage,check=check)
                self.check_task_cancel(task)
                for dep_id in task['depends_on']:
                    dep = self.store.get('tasks',dep_id)
                    artifact = project/'runs'/dep['attempt_id']/'result'
                    expected = dep.get('result_manifest')
                    if not artifact.is_dir() or manifest(artifact,check=check)!=expected:
                        raise ValueError('前序本机结果被修改或删除，请恢复：'+dep['name'])
                    copy_tree(artifact,stage/'deps'/dep_id,check=check)
                    self.check_task_cancel(task)
                self.runtime_progress(task,'hashing','计算本次实验输入文件校验值')
                task['input_manifest'] = manifest(stage,check=check)
            self.put_runtime_task(task)
            self.check_task_cancel(task)
            spec = self.spec(task)
            capacity=remote.call('probe')
            needed=sum(item['size'] for item in task['input_manifest'].values())/1024**3
            if capacity['work_free_gb'] < needed+server.get('min_disk_gb',5):
                raise ValueError('运行目录空间不足以接收本次代码与前序文件')
            remote.call('project',spec)
            code_inventory=remote.call('manifest',{**spec,'relative':'code'})
            local_code=manifest(project/'code',check=check)
            if code_inventory['exists'] and code_inventory['files']!=local_code:
                backup=project/'remote-code-history'/server['id']/'snapshot'
                backup.mkdir(parents=True,exist_ok=True)
                remote.download(backup,spec,'code')
            def upload(source,prefix,description):
                self.runtime_progress(task,'transferring',description,bytes=0,total=0)
                stamp=[0]
                def progress(done,total):
                    now=time.monotonic()
                    if now-stamp[0]<.3 and done!=total:return
                    stamp[0]=now
                    phase='verifying' if done==total else 'transferring'
                    label='文件已发送，正在核验远程内容（字节进度 100% 不代表启动完成）' if phase=='verifying' else description
                    self.runtime_progress(task,phase,label,bytes=done,total=total,percent=round(done/total*100,1) if total else 0)
                remote.upload(source,spec,prefix,progress=progress,cancel_check=lambda:self.check_task_cancel(task))
            upload(project/'code','code','传输项目公共代码')
            upload(stage,'runs/'+task['attempt_id']+'/workspace','传输任务输入及前序结果')
        spec=self.spec(task)
        self.runtime_progress(task,'resource_check','传输与校验已完成，复查 GPU 和磁盘资源')
        self.check_task_cancel(task)
        # Refresh resources after potentially lengthy transfers; reapply policy and mask.
        fresh = remote.call('probe')
        fresh['remote_sampled_at']=fresh.get('sampled_at')
        fresh['sampled_at']=time.time()
        with self.store.lock:
            current = self.store.get('servers',server['id'])
            current.update(snapshot=fresh,connected=True)
            self.store.put('servers',current)
            others = [t for t in self.store.summaries('tasks') if t['id']!=task['id']]
            decision = choose(task,[current],others,self.settings)
            if not decision:
                raise ValueError('传输后资源条件已改变，等待重新调度')
            gpu = decision[1]
            task.update(gpu_uuid=gpu.get('uuid') or str(gpu['index']),gpu_index=gpu['index'],status='launching',reason='远程启动中',attempts=task['attempts']+1,started_at=time.time(),resolved_conda_env=selected_environment['conda_env'],conda_env_source=selected_environment['source'])
            self.put_runtime_task(task)
        self.runtime_progress(task,'launching','传输与校验已完成，等待远程启动确认')
        self.put_runtime_task(task)
        self.check_task_cancel(task)
        env = {'CUDA_DEVICE_ORDER':'PCI_BUS_ID', 'CUDA_VISIBLE_DEVICES':gpu.get('uuid') or str(gpu['index']), 'PROJECT_ID':task['project_id'], 'TASK_ID':task['id'], 'RUN_ID':task['attempt_id'], 'DATA_ROOT':fresh['data_root'], 'DATASETS_JSON':json.dumps(dataset_paths,ensure_ascii=False), 'DEPENDENCIES_JSON':json.dumps({d:'deps/'+d for d in task['depends_on']}), 'PYTHONUNBUFFERED':'1'}
        for i, (dataset,path) in enumerate(dataset_paths.items()):
            env['DATASET_'+str(i)] = path
        result = remote.call('launch',{**spec,'command':task['command'],'conda_env':selected_environment['conda_env'],'env':env})
        task.update(status='running',reason='',remote_state=result)
        task.pop('preparation_retry_at',None);task.pop('preparation_failures',None)
        self.put_runtime_task(task)
        if task.get('cancel_requested'):remote.call('cancel',spec,timeout=30)
        self.runtime_progress(task,'running','实验已启动，正在执行')
        self.enqueue('刷新已启动实验的副本清单',self.refresh_replica,task['project_id'],server['id'])
        self.store.event('启动 '+task['name']+' → '+server['name']+' / GPU '+str(gpu['index']))

    def prepare_content(self,task,server,remote,resume=False):
        from .content import Content
        pool=Content(self,task['project_id']);check=lambda:self.check_task_cancel(task)
        self.runtime_progress(task,'hashing','建立输入引用与内容清单，不复制完整前序目录')
        catalog=pool.read_catalog(task['attempt_id'])
        retry_snapshot=task.get('retry_snapshot_attempt')
        if retry_snapshot and not resume:
            retained=pool.read_catalog(retry_snapshot)
            if retained.get('inputs') is None:raise ValueError('自动重试的原输入版本缺失，未改用新代码')
            pool.save_catalog(task['attempt_id'],**{k:v for k,v in retained.items() if k in ('inputs','public','read_only_files','dependencies','code_revision','input_revision')})
            catalog=pool.read_catalog(task['attempt_id'])
        if (resume or retry_snapshot) and catalog.get('inputs') is not None:
            inventory=catalog['inputs'];public=catalog.get('public',{})
            for item in {v['sha256']:v for v in [*inventory.values(),*public.values()]}.values():
                check()
                if not pool.contains(item['sha256'],item['size'],check):raise ValueError('保留的输入内容被修改或删除')
        else:inventory,public=pool.build(task,check)
        catalog=pool.read_catalog(task['attempt_id'])
        task['code_revision']=catalog.get('code_revision');task['input_revision']=catalog.get('input_revision')
        task.update(input_manifest=inventory,storage_version=2)
        self.put_runtime_task(task);spec=self.spec(task);last=[0]
        def progress(done,total,reused,count,phase=None):
            now=time.monotonic()
            if now-last[0]<.3 and done!=total:return
            last[0]=now
            self.runtime_progress(task,phase or ('transferring' if done<total else 'materializing'),'核验刚上传的新内容' if phase=='verifying' else '仅传输服务器缺失的内容；已存在的 hash 直接复用',bytes=done,total=total,percent=round(done/total*100,1) if total else 100,reused_objects=reused,objects=count)
        self.runtime_progress(task,'cache_check','检查所需 hash 的服务器缓存和文件存在状态')
        transfer=remote.ensure_objects(pool,spec,inventory,check,progress,public)
        self.runtime_progress(task,'materializing','建立独立代码工作区和只读前序引用')
        for retry in range(2):
            read_only_files=pool.read_catalog(task['attempt_id']).get('read_only_files',[])
            assembled=remote.cancellable_call('content_assemble',{**spec,'files':inventory,'public':public,'dependency_ids':task['depends_on'],'read_only_files':read_only_files},check=check,timeout=600)
            if not assembled['missing']:break
            remote.ensure_objects(pool,spec,inventory,check,progress,public)
        else:raise ValueError('服务器内容被删除，无法建立运行目录，请核验存储')
        task['content_transfer']=transfer;self.put_runtime_task(task)

    def recover(self):
        for task in self.store.summaries('tasks'):
            if task['status'] not in ACTIVE or task['id'] in self.runtime_pending:continue
            if self.background_runtime and len(self.runtime_pending)>=8:break
            task=self.store.get('tasks',task['id'])
            self.submit_runtime(task,self.recover_one)

    def recover_one(self,task):
        server = self.store.get('servers',task['server_id'])
        if not server.get('connected'):
            return
        try:
            if task['status'] in ('syncing','sync_failed'):
                if time.time()-task.get('sync_retry_at',0)>=30:
                    self.finish(task)
                return
            result = self.remote(server).call('status',self.spec(task))
            if result['state']=='finished':
                task.update(status='syncing',exit_code=result['exit_code'],remote_finished_at=result.get('finished_at'),reason='实验结束，正在回传全部运行文件',canceled=result.get('canceled',False))
                self.put_runtime_task(task)
                self.finish(task)
            elif result['state'] in ('running','starting','paused'):
                task.update(status='canceling' if task.get('cancel_requested') else 'suspended' if result['state']=='paused' else 'running',reason='运行已暂停；GPU 和显存仍保留' if result['state']=='paused' else '',remote_state=result)
                self.put_runtime_task(task)
                if task.get('cancel_requested'):self.remote(server).call('cancel',self.spec(task),timeout=30)
            elif result['state']=='absent' and task['status']=='staging':
                with self.store.lock:
                    latest=self.store.get('tasks',task['id'])
                    if latest.get('attempt_id')!=task.get('attempt_id'):raise OperationCanceled('运行记录已更新')
                    task=latest
                    assignment=None
                    if task.get('cancel_requested'):
                        task.update(status='canceled',finished_at=time.time(),reason='用户已中断实验准备，脚本未启动')
                    elif self.settings.get('paused') or self.stop_event.is_set():
                        task['reason']='输入和传输目录已保留，等待恢复调度后继续准备'
                    elif any(key!=task['id'] and v['project_id']==task['project_id'] and (v['kind']=='launch_reserved' or v['kind']=='recover_preparation' and v['preparation_order']<(task.get('preparation_started_at',task.get('created_at',0)),task.get('seq',0),task['id'])) for key,v in self.runtime_pending.items()):
                        task['reason']='输入和传输目录已保留，等待同项目准备工作完成'
                    else:
                        assignment=choose(task,[server],[t for t in self.store.summaries('tasks') if t['id']!=task['id']],self.settings)
                        task['reason']='继续中断的实验准备' if assignment else '输入和传输目录已保留，等待原服务器符合调度条件'
                        if assignment and task['id'] in self.runtime_pending:self.runtime_pending[task['id']]['kind']='launch_reserved'
                    self.put_runtime_task(task)
                if assignment:self.launch_reserved(task,*assignment,resume=True)
            else:
                task.update(status='lost',reason='远程进程或目录已消失，保留副本且禁止自动重跑、清理，请人工核实')
                self.put_runtime_task(task)
        except OperationCanceled:
            raise
        except Exception as exc:
            task.update(reason='恢复检查失败：'+str(exc))
            self.put_runtime_task(task)



    def finish(self, task):
        expected_attempt=task.get('attempt_id')
        server = self.store.get('servers',task['server_id'])
        remote = self.remote(server)
        try:
            from .content import Content
            pool=Content(self,task['project_id'])
            path=pool.return_destination(task['attempt_id'],'result')
            if task.get('storage_version')!=2:path.mkdir(parents=True,exist_ok=True)
            task['status']='syncing'
            self.runtime_progress(task,'syncing','核验远程输出清单；后续实验等待回传和校验完成')
            if task.get('storage_version')==2:
                from .content import Content
                pool=Content(self,task['project_id'])
                result=remote.cancellable_call('content_results',{**self.spec(task),'output_files':task.get('output_files')},timeout=3600,check=lambda:self.check_cancel(None))
                last=[0]
                def progress(done,total):
                    now=time.monotonic()
                    if now-last[0]<.4 and done!=total:return
                    last[0]=now
                    self.runtime_progress(task,'syncing','回传缺失输出，已存在的内容跳过；断线后从文件片段继续',bytes=done,total=total,percent=round(done/total*100,1) if total else 100)
                received=pool.receive(remote,self.spec(task),result['files'],check=lambda:self.check_cancel(None),progress=progress)
                pool.save_catalog(task['attempt_id'],outputs=result['files'],artifacts=result['files'],completed_at=time.time())
                task['content_return']=received
            else:result = remote.download(path,self.spec(task),'runs/'+task['attempt_id']+'/workspace')
            self.runtime_progress(task,'syncing','输出已核验，正在保存日志和运行控制记录')
            remote.download(pool.return_destination(task['attempt_id'],'control'),self.spec(task),'runs/'+task['attempt_id']+'/_control')
            code_inventory=remote.call('manifest',{**self.spec(task),'relative':'code'}) if task.get('storage_version')!=2 else {'exists':False}
            if code_inventory['exists']:
                backup=self.local_project(task['project_id'])/'remote-code-history'/server['id']/'snapshot'
                backup.mkdir(parents=True,exist_ok=True)
                remote.download(backup,self.spec(task),'code')
            log = remote.call('log',{**self.spec(task),'bytes':2000000})['text']
            pool.return_destination(task['attempt_id'],'run.log').write_text(log,encoding='utf-8')
            task.update(synced_at=time.time(),result_manifest=result['files'],reason='',finished_at=time.time())
            task.pop('sync_last_error',None);task.pop('sync_retry_at',None)
            code = task['exit_code']
            oom = code in (137,139,-9) or any(s in log.lower() for s in ('cuda out of memory','cuda error: out of memory','out-of-memory'))
            latest=self.store.get('tasks',task['id'])
            if latest.get('cancel_requested'):task['cancel_requested']=True
            if task.get('cancel_requested') or task.get('canceled'):
                task['status']='canceled'
            elif code==0:
                task['status']='success'
            elif oom and task['attempts'] <= task['max_retries']:
                task['retry_snapshot_attempt']=task['attempt_id']
                task.update(status='waiting',memory_mb=max(task['memory_mb']+1024,int(task['memory_mb']*1.25)),reason='检测到 OOM/异常退出，已提高显存需求并保留本次结果；有界重试')
                task['previous_attempts'] = task.get('previous_attempts',[])+[{'attempt_id':task['attempt_id'],'server_id':task['server_id'],'exit_code':code,'synced_at':task['synced_at'],'resolved_conda_env':task.get('resolved_conda_env'),'conda_env_source':task.get('conda_env_source')}]
                task.update(server_id=None,attempt_id=None,synced_at=None)
                task.pop('resolved_conda_env',None);task.pop('conda_env_source',None)
            else:
                task['status']='failed'
            self.put_runtime_task(task,expected_attempt=expected_attempt)
            self.store.event('回传完成：'+task['name']+' / '+task['status'])
        except OperationCanceled:
            raise
        except Exception as exc:
            task.update(status='sync_failed',synced_at=None,sync_retry_at=time.time(),sync_last_error=str(exc),sync_failures=task.get('sync_failures',0)+1,reason='回传失败；将自动重试并保留文件片段：'+str(exc),runtime_progress={'phase':'sync_failed','label':'回传失败，保留文件片段，等待重试','error':str(exc),'updated_at':time.time()})
            self.put_runtime_task(task,expected_attempt=expected_attempt)
            self.store.event('回传失败：'+task['name']+' / '+str(exc),'error')
            raise
        self.enqueue('刷新已回传实验的副本清单',self.refresh_replica,task['project_id'],server['id'])

    def refresh_replica(self, project_id, server_id, job=None):
        server=self.store.get('servers',server_id)
        remote=self.remote(server)
        spec={'project_id':project_id}
        if self.settings.get('content_store_enabled',True):
            from .content import Content
            inventory=remote.call('content_replica',spec)
            local=Content(self,project_id).tree(self.local_project(project_id)/'code')
        else:
            inventory=remote.call('manifest',spec)
            local=manifest(self.local_project(project_id)/'code')
        code={k[5:]:v for k,v in inventory['files'].items() if k.startswith('code/')}
        differences=[{'path':k,'state':'missing_remote' if k not in code else 'remote_only' if k not in local else 'different'} for k in sorted(set(local)|set(code)) if local.get(k)!=code.get(k)]
        replica={'id':project_id+':'+server_id,'project_id':project_id,'server_id':server_id,'checked_at':time.time(),'exists':inventory['exists'],'files':inventory['files'],'skipped':inventory['skipped'],'differences':differences,'last_used_at':time.time()}
        self.store.put('replicas',replica)
        sharing = self.ensure_storage('work')
        for group in sharing.get('groups', []):
            if server_id in group:
                for sid in group:
                    self.store.put('replicas', {**replica, 'id': project_id+':'+sid, 'server_id': sid, 'shared_server_ids': group})
        return replica


    def current_sharing(self, scope):
        try:
            value = self.store.get('storage', scope)
        except ValueError:
            return {}
        return value if value.get('configuration') == configuration(self.store.all('servers'),scope) else {}

    def ensure_storage(self,scope='work',job=None):
        with self.lock:
            return self.current_sharing(scope) or self.detect_storage(scope,job=job)

    def detect_storage(self, scope='work', job=None):
        with self.lock:
            result = detect(self.store.all('servers'), self.remote, scope)
            self.store.put('storage', result)
            return result

    def cleanup_plan(self, project_id, server_ids=None, job=None):
        with self.lock:
            self.store.get('projects', project_id)
            if any(t['project_id']==project_id and t['status'] in ACTIVE for t in self.store.all('tasks')):
                raise ValueError('项目存在运行、回传或状态未知的实验，已拒绝清理')
            servers = self.store.all('servers')
            requested = set(server_ids if server_ids is not None else [s['id'] for s in servers])
            if not requested.issubset({s['id'] for s in servers}):
                raise ValueError('服务器不存在')
            sharing = self.ensure_storage('work')
            unknown = [s['id'] for s in servers if sharing['states'][s['id']]['status'] != 'verified']
            if unknown:
                raise ValueError('无法排除工作目录共享，已拒绝清理。请先连接并核验：'+', '.join(s['name'] for s in servers if s['id'] in unknown))
            groups = [g for g in sharing['groups'] if requested.intersection(g)]
            affected = sorted({sid for g in groups for sid in g})
            return {'project_id':project_id, 'groups':groups, 'requested_server_ids':sorted(requested), 'affected_server_ids':affected, 'shared':set(affected)!=requested, 'checked_at':sharing['checked_at']}

    def clean_project(self, project_id, server_ids=None, job=None, automatic=False, confirmed_server_ids=None):
        with self.lock:
            self.store.get('projects',project_id)
            if any(t['project_id']==project_id and t['status'] in ACTIVE for t in self.store.all('tasks')):
                raise ValueError('项目存在运行、回传或状态未知的实验，已拒绝清理')
            plan = self.cleanup_plan(project_id, server_ids)
            if plan['shared'] and not automatic and set(confirmed_server_ids or []) != set(plan['affected_server_ids']):
                raise ValueError('工作目录共享，清理也会影响其他服务器，请核对并确认全部受影响服务器')
            if confirmed_server_ids is not None and set(confirmed_server_ids) != set(plan['affected_server_ids']):
                raise ValueError('共享关系发生变化，请重新预览清理范围')
            all_servers = {s['id']:s for s in self.store.all('servers')}
            selected=[all_servers[g[0]] for g in plan['groups']]
            if automatic:
                replicas = self.store.all('replicas')
                for group in plan['groups']:
                    members = [all_servers[sid] for sid in group]
                    if any(not s.get('enabled') or not s.get('connected') or s.get('retention_days',7)<=0 for s in members):
                        raise ValueError('共享组有停用、离线或关闭自动清理的服务器')
                    last = max([r.get('last_used_at',time.time()) for r in replicas if r['project_id']==project_id and r['server_id'] in group] or [time.time()])
                    if time.time()-last <= max(s.get('retention_days',7) for s in members)*86400:
                        raise ValueError('共享副本尚未达到整个共享组的保留时间')
            # Preflight every target before deleting any directories.
            for server in selected:
                self.remote(server).call('manifest',{'project_id':project_id})
            results=[]
            for server in selected:
                try:
                    # Preserve manual remote changes before either manual or TTL deletion.
                    archive=self.local_project(project_id)/'remote-archives'/server['id']/'snapshot'
                    remote=self.remote(server)
                    inventory=remote.call('manifest',{'project_id':project_id})
                    if inventory['exists'] and self.settings.get('content_store_enabled',True):
                        from .content import Content,atomic_json
                        pool=Content(self,project_id);export=remote.call('content_export',{'project_id':project_id},timeout=3600)
                        pool.receive(remote,{'project_id':project_id},export['files'])
                        archive.mkdir(parents=True,exist_ok=True);atomic_json(archive/'catalog.json',export['files'])
                    elif inventory['exists']:
                        archive.mkdir(parents=True,exist_ok=True)
                        remote.download(archive,{'project_id':project_id},'')
                    value=self.remote(server).call('clean',{'project_id':project_id})
                    for sid in next(g for g in plan['groups'] if server['id'] in g):
                        self.store.delete('content_caches',project_id+':'+sid)
                    self.refresh_replica(project_id,server['id'])
                    group = next(g for g in plan['groups'] if server['id'] in g)
                    results.append({'server_id':server['id'],'shared_server_ids':group,**value})
                except Exception as exc:
                    results.append({'server_id':server['id'],'error':str(exc)})
            self.store.event(('自动' if automatic else '手动')+'清理项目远程副本：'+project_id)
            if any('error' in r for r in results):
                raise RuntimeError('部分服务器清理失败：'+json.dumps(results,ensure_ascii=False))
            return results

    def compact_content(self,project_id,server_ids=None,job=None):
        from .content import Content
        with self.store.lock:
            if project_id in self.project_storage_busy:raise ValueError('项目正在整理')
            if any(t['project_id']==project_id and t['status'] in ACTIVE for t in self.store.summaries('tasks')):raise ValueError('项目有活动实验，拒绝整理')
            self.project_storage_busy.add(project_id)
        try:
            local=Content(self,project_id).compact(job)
            remote=[];seen=set();sharing=self.current_sharing('work')
            for sid in server_ids or []:
                group=next((g for g in sharing.get('groups',[]) if sid in g),[sid])
                if seen.intersection(group):continue
                seen.update(group);self.check_cancel(job);server=self.store.get('servers',sid)
                self.progress(job,phase='compacting_server',label='整理服务器历史文件：'+server['name'],bytes=0,total=0)
                try:
                    value=self.remote(server).cancellable_call('content_compact',{'project_id':project_id},check=lambda:self.check_cancel(job),timeout=86400)
                    for member in group:
                        key=project_id+':'+member
                        try:prior=self.store.get('content_caches',key)
                        except ValueError:prior={}
                        self.store.put('content_caches',{'id':key,'project_id':project_id,'server_id':member,**prior,'objects':value['objects'],'bytes':value['bytes'],'compacted_at':time.time()})
                    remote.append({'server_ids':group,**value})
                except Exception as exc:remote.append({'server_ids':group,'error':str(exc)})
            return {'local':local,'servers':remote}
        finally:self.project_storage_busy.discard(project_id)

    def scan_content_cache(self,project_id,server_id,job=None):
        server=self.store.get('servers',server_id)
        value=self.remote(server).cancellable_call('content_scan',{'project_id':project_id},check=lambda:self.check_cancel(job),timeout=86400)
        group=next((g for g in self.current_sharing('work').get('groups',[]) if server_id in g),[server_id])
        for sid in group:self.store.put('content_caches',{'id':project_id+':'+sid,'project_id':project_id,'server_id':sid,**value})
        return value

    def schedule_content_scans(self):
        tasks=self.store.summaries('tasks');caches={v['id']:v for v in self.store.all('content_caches')};seen=set()
        for project in self.store.all('projects'):
            pid=project['id']
            if pid in self.project_storage_busy or any(t['project_id']==pid and t['status'] in ACTIVE for t in tasks):continue
            if not (self.local_project(pid)/'objects').is_dir():continue
            for server in self.store.all('servers'):
                if not server.get('connected') or not server.get('enabled'):continue
                group=next((g for g in self.current_sharing('work').get('groups',[]) if server['id'] in g),[server['id']])
                if any((pid,sid) in seen for sid in group):continue
                seen.update((pid,sid) for sid in group)
                cached=caches.get(pid+':'+server['id'],{})
                if time.time()-cached.get('checked_at',0)>=self.settings.get('cache_scan_hours',24)*3600:
                    self.enqueue('定期核验项目内容缓存',self.scan_content_cache,pid,server['id'])

    def report_content_storage(self,project_id,job=None):
        from .content import Content
        self.progress(job,phase='scanning',label='统计项目存储，不重新计算文件 hash',bytes=0,total=0)
        value={'id':project_id,'project_id':project_id,'checked_at':time.time(),**Content(self,project_id).stats()}
        self.store.put('content_reports',value);return value

    def report_input_plan(self,task_id,job=None):
        from .content import Content
        task=self.store.get('tasks',task_id);pool=Content(self,task['project_id'])
        self.progress(job,phase='hashing',label='计算需要的输入与缓存复用计划',bytes=0,total=0)
        inventory,public=pool.build(task,lambda:self.check_cancel(job),save=False)
        unique={v['sha256']:v for v in [*inventory.values(),*public.values()]};servers=[];seen={}
        for server in self.store.all('servers'):
            if task['server_ids'] and server['id'] not in task['server_ids']:continue
            if not server.get('enabled'):continue
            group=next((g for g in self.current_sharing('work').get('groups',[]) if server['id'] in g),[server['id']]);key=tuple(sorted(group))
            if key not in seen:
                try:seen[key]=self.remote(server).cancellable_call('content_need',{'project_id':task['project_id'],'objects':unique,'dry_run':True},check=lambda:self.check_cancel(job))
                except Exception as exc:seen[key]={'error':str(exc)}
            report=seen[key];missing=report.get('missing',[])
            servers.append({'server_id':server['id'],'server_name':server['name'],'shared_server_ids':group,'missing_objects':len(missing),'upload_bytes':sum(unique[h]['size'] for h in missing),'error':report.get('error')})
        value={'id':task_id,'task_id':task_id,'checked_at':time.time(),'files':inventory,'logical_bytes':sum(v['size'] for v in inventory.values()),'unique_bytes':sum(v['size'] for v in unique.values()),'unique_objects':len(unique),'servers':servers}
        self.store.put('input_plans',value);return {k:v for k,v in value.items() if k!='files'}

    def clean_confirmed(self, project_id, server_ids, confirmed_server_ids, job=None):
        return self.clean_project(project_id,server_ids,job=job,confirmed_server_ids=confirmed_server_ids)

    def auto_cleanup(self,job=None):
        cleaned=set()
        for replica in self.store.all('replicas'):
            if (replica['project_id'],replica['server_id']) in cleaned:
                continue
            server=self.store.get('servers',replica['server_id'])
            days=server.get('retention_days',7)
            if days<=0 or not replica.get('exists') or not server.get('enabled') or not server.get('connected'):
                continue
            if time.time()-replica['last_used_at'] > days*86400:
                try:
                    result=self.clean_project(replica['project_id'],[server['id']],automatic=True)
                    for value in result:
                        cleaned.update((replica['project_id'],sid) for sid in value.get('shared_server_ids',[value['server_id']]))
                except Exception as exc:
                    self.store.event('自动清理跳过：'+str(exc),'warning')

    def benchmark(self, server_id, gpu_index, job=None):
        with self.lock:
            server=self.store.get('servers',server_id)
            if any(t['status'] in ACTIVE and t.get('server_id')==server_id and t.get('gpu_index')==gpu_index for t in self.store.all('tasks')):
                raise ValueError('目标 GPU 正在执行任务，不能运行基准测试')
            snapshot=self.remote(server).call('probe')
            gpu=next((g for g in snapshot['gpus'] if g['index']==gpu_index),None)
            if not gpu or not gpu.get('is_idle'):
                raise ValueError('基准测试仅允许使用空闲 GPU')
            if any(t['status'] in ACTIVE and gpu.get('uuid') and t.get('gpu_uuid')==gpu['uuid'] for t in self.store.all('tasks')):
                raise ValueError('该物理 GPU 已通过其它 SSH 连接分配任务，不能基准测试')
            result=self.remote(server).call('benchmark',{'gpu_index':gpu_index,'gpu_uuid':gpu.get('uuid')},timeout=180)
            if not result.get('ok'):
                raise ValueError(result.get('error','基准测试失败'))
            result.update(measured_at=time.time(), gpu_uuid=gpu.get('uuid'), gpu_index=gpu_index)
            server = self.store.get('servers',server_id)
            server.setdefault('benchmarks',{})[gpu.get('uuid') or str(gpu_index)]=result
            self.store.put('servers',server)
            return result

    def upload_dataset(self, dataset_id, job=None):
        dataset=self.store.get('datasets',dataset_id)
        if dataset.get('import_status','ready')!='ready' or not dataset.get('manifest'):
            raise ValueError('数据集尚未完成本机导入，请在上传与进度中继续处理')
        if any(t['status'] in ACTIVE and dataset['name'] in t.get('datasets',[]) for t in self.store.all('tasks')):
            raise ValueError('有正在执行的任务使用该数据集，请在实验完成后上传')
        servers=self.store.all('servers')
        if not servers:
            raise ValueError('请先添加服务器')
        local=self.root/'datasets'/dataset_id/'files'
        last=[0]
        def worker_update(kind,event,scratch,pid):
            if kind=='progress':
                self.progress(job,**{k:v for k,v in event.items() if k!='type'},worker_pid=pid)
        checked=file_process(self.root,'manifest',path=str(local),update=worker_update,check=lambda:self.check_cancel(job))
        if checked!=dataset['manifest']:
            raise ValueError('本机数据集发生变化，请重新导入')
        self.transfers.prepare(job,servers,checked)
        self.check_cancel(job)
        if not self.current_sharing('data'):
            self.progress(job,phase='detecting_storage',bytes=0,total=0)
        sharing=self.ensure_storage('data')
        by_id={s['id']:s for s in servers}
        groups=[[by_id[sid] for sid in group] for group in sharing['groups']]
        failures=[]
        # A multi-GB hash scan is a storage operation, not a short RPC. Keep it
        # cancellable while budgeting for slow shared disks and many files.
        hash_timeout=max(300,min(7200,60+sum(v['size'] for v in checked.values())/(2*1024*1024)+len(checked)*.025))
        metadata_timeout=max(300,min(1800,60+len(checked)*.05))
        for server in servers:
            state=sharing['states'].get(server['id'],{})
            if state.get('status')!='verified':
                dataset.setdefault('servers',{})[server['id']]={'status':'failed','error':'共享关系未核验，请点击“重新探测”：'+state.get('error','缺少探测结果')}
                failures.append(server['name'])
        dataset['shared_groups']=[[s['id'] for s in group] for group in groups]
        self.store.put('datasets',dataset)
        for group in groups:
            source=group[0]
            try:
                for s in group:
                    dataset.setdefault('servers',{})[s['id']]={'status':'uploading','shared_with':source['id']}
                self.store.put('datasets',dataset)
                current_file=[''];last_file_update=[0]
                def progress(done,total):
                    self.check_cancel(job)
                    if time.monotonic()-last[0]>.25 or done==total:
                        last[0]=time.monotonic()
                        self.progress(job,phase='syncing',server=source['name'],bytes=done,total=total,path=current_file[0],groups_total=len(groups),group_index=groups.index(group)+1)
                # Reuse the proven group even if its usual upload host is offline.
                # Reachability and file hashes still need checking for each upload.
                errors=[];remote=None
                for candidate in group:
                    self.check_cancel(job)
                    try:
                        candidate_remote=self.remote(candidate)
                        existing=candidate_remote.cancellable_call('dataset_manifest',{'name':dataset['name']},timeout=hash_timeout,check=lambda:self.check_cancel(job))['files']
                        source,remote=candidate,candidate_remote
                        break
                    except OperationCanceled:
                        raise
                    except Exception as exc:
                        errors.append(candidate['name']+'：'+str(exc))
                if remote is None:
                    raise ValueError('共享组无可连接服务器：'+'；'.join(errors))
                for s in group:
                    dataset['servers'][s['id']]['shared_with']=source['id']
                self.store.put('datasets',dataset)
                self.transfers.plan(job,group,checked,existing,source)
                def file_event(path,status,done=0,error=None):
                    current_file[0]=path
                    now=time.monotonic()
                    if status=='uploading' and now-last_file_update[0]<.25 and done: return
                    last_file_update[0]=now
                    self.transfers.update(job,[s['id'] for s in group],path,status,done,error)
                self.progress(job,phase='syncing',server=source['name'],bytes=0,total=sum(v['size'] for v in checked.values()),groups_total=len(groups),group_index=groups.index(group)+1)
                remote.upload(local,{'name':dataset['name']},'',dataset=True,progress=progress,inventory=checked,cancel_check=lambda:self.check_cancel(job),file_event=file_event,existing_inventory=existing,verify_after=False)
                failures.extend(self._verify_dataset_group(dataset,group,source,checked,hash_timeout,metadata_timeout,job))
            except OperationCanceled as exc:
                for s in group:
                    if dataset.get('servers',{}).get(s['id'],{}).get('status')!='success':
                        dataset.setdefault('servers',{})[s['id']]={'status':'interrupted','error':str(exc)}
                self.store.put('datasets',dataset)
                raise
            except Exception as exc:
                for s in group:
                    dataset.setdefault('servers',{})[s['id']]={'status':'failed','error':str(exc)}
                failures.extend(s['name'] for s in group)
            self.store.put('datasets',dataset)
        if failures:
            pending=[s['name'] for s in servers if dataset.get('servers',{}).get(s['id'],{}).get('status')=='verification_pending']
            if pending and len(pending)==len(set(failures)):
                raise ValueError('数据已上传；以下服务器核验未完成：'+', '.join(pending)+'。重试将跳过已一致文件，不必重新导入数据')
            raise ValueError('部分服务器未完成同步或核验，重试会跳过 hash 一致文件：'+', '.join(failures))
        return dataset

    def verify_dataset(self,dataset_id,job=None):
        dataset=self.store.get('datasets',dataset_id);checked=dataset.get('manifest',{})
        if not checked or dataset.get('import_status','ready')!='ready':raise ValueError('数据集尚未完成导入')
        if any(j['status'] in ('queued','running') and j.get('operation')=='upload_dataset:'+json.dumps([dataset_id]) for j in self.store.summaries('jobs')):raise ValueError('此数据集正在同步，请等待完成后核验')
        sharing=self.current_sharing('data')
        if not sharing:raise ValueError('尚无有效共享关系，请先在服务器页面探测')
        servers=self.store.all('servers');by_id={s['id']:s for s in servers}
        self.transfers.prepare(job,servers,checked,action='verify')
        hash_timeout=max(300,min(7200,60+sum(v['size'] for v in checked.values())/(2*1024*1024)+len(checked)*.025))
        metadata_timeout=max(300,min(1800,60+len(checked)*.05));failures=[]
        for group_ids in sharing['groups']:
            group=[by_id[sid] for sid in group_ids];source=group[0]
            # Prefer the previously verified host, without assuming the peer's
            # mount is still shared. No upload, commit or file replacement.
            source=next((s for s in group if dataset.get('servers',{}).get(s['id'],{}).get('status')=='success'),source)
            failures.extend(self._verify_dataset_group(dataset,group,source,checked,hash_timeout,metadata_timeout,job))
        covered={sid for g in sharing['groups'] for sid in g}
        failures.extend(s['name'] for s in servers if s['id'] not in covered)
        if failures:raise ValueError('以下服务器核验未完成：'+', '.join(failures)+'。本次仅核验，没有传输或覆盖文件')
        return dataset

    def delete_dataset(self,dataset_id,job=None):
        from .dataset_cleanup import delete
        return delete(self,dataset_id,job)

    def _verify_dataset_group(self,dataset,group,source,checked,hash_timeout,metadata_timeout,job=None):
        remote=self.remote(source);failures=[]
        token=uid();verified_source=None
        try:
            for s in [source]+[member for member in group if member['id']!=source['id']]:
                self.progress(job,phase='verifying_remote',server=s['name'],bytes=0,total=0)
                content_mismatch=False
                try:
                    if s['id']==source['id']:
                        verification=remote.cancellable_call('dataset_verify_snapshot',{'name':dataset['name'],'token':token},timeout=hash_timeout,check=lambda:self.check_cancel(job))
                        self.transfers.verify(job,s['id'],verification['files'])
                        content_mismatch=verification['files']!=checked or bool(verification['skipped'])
                        if content_mismatch:raise ValueError('服务器 '+s['name']+' 数据集内容校验不一致')
                        verified_source=verification;method='sha256'
                    else:
                        if verified_source is None:raise TimeoutError('共享来源尚未完成内容核验')
                        view=self.remote(s).cancellable_call('dataset_shared_verify',{'name':dataset['name'],'token':token},timeout=metadata_timeout,check=lambda:self.check_cancel(job))
                        if view['metadata']!=verified_source['metadata'] or view['skipped']:
                            content_mismatch=True
                            raise ValueError('服务器 '+s['name']+' 共享数据文件视图不一致，请检查挂载或文件变化')
                        method='shared_sha256'
                        self.transfers.verify(job,s['id'],verified_source['files'],method,source['id'])
                    dataset['servers'][s['id']]={'status':'success','verified_at':time.time(),'shared_with':source['id'],'verification_method':method}
                except OperationCanceled:
                    raise
                except Exception as exc:
                    status='failed' if content_mismatch else 'verification_pending'
                    error=str(exc) if content_mismatch else '数据已上传，但此服务器核验未完成：'+str(exc)
                    dataset['servers'][s['id']]={'status':status,'error':error,'shared_with':source['id']}
                    if not content_mismatch or s['id']!=source['id']:self.transfers.verification_pending(job,s['id'],error)
                    failures.append(s['name'])
                self.store.put('datasets',dataset)
        finally:
            try:remote.call('dataset_verify_clear',{'name':dataset['name'],'token':token},timeout=15)
            except Exception as exc:self.store.event('共享核验临时标记清理未完成：'+str(exc),'warning')
        return failures
