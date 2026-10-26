"""Shell input handling and staged, validated batch experiment imports."""
import hashlib
import json
import os
import shlex
import threading
import time
from pathlib import Path

from .files import confined,relative
from .file_process import run
from .storage import uid

SCRIPT_LIMIT=2*1024**2
FIELDS={'name','command','depends_on','server_ids','datasets','conda_env','server_conda_envs','gpu_name','priority','memory_mb','max_utilization','level','max_retries','dependency_files','code_files','code_exclude','output_files','object_files','follow_project_files'}


def shell_script(filename,content):
    filename=relative(filename)
    if not filename.lower().endswith('.sh'):raise ValueError('请选择 .sh 文件')
    if len(content)>SCRIPT_LIMIT:raise ValueError('Shell 脚本最多 2 MB')
    try:text=content.decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n')
    except UnicodeDecodeError:raise ValueError('Shell 脚本须为 UTF-8 编码')
    if not text.strip() or '\x00' in text:raise ValueError('Shell 脚本为空或含无效内容')
    return filename,text.encode('utf-8')


def script_command(path,args=()):
    if not isinstance(args,list) and args!=():raise ValueError('args 必须是字符串数组')
    if any(not isinstance(a,str) or '\x00' in a for a in args):raise ValueError('args 必须是字符串数组')
    return 'bash -- '+shlex.quote(relative(path))+''.join(' '+shlex.quote(a) for a in args)


def save_task(engine,project_id,data,script=None,task_id=None):
    with engine.lock:
        existing=engine.store.get('tasks',task_id) if task_id else None
        if existing and existing.get('archived'):raise ValueError('实验已归档，请先恢复归档后再编辑')
        if existing and existing['status'] not in ('draft','queued','waiting','blocked','paused'):raise ValueError('已运行任务不能修改；请新建实验')
        if existing and engine.uploads.pending(project_id,task_id):raise ValueError('任务输入正在导入，请等待完成')
        data={**(existing or {}),**data}
        path=None;content=None
        if script:
            path,content=shell_script(*script)
            if '/' in path:raise ValueError('上传的脚本名称不能包含目录')
            data['command']=data.get('command') or script_command(path)
        elif data.get('script_path'):
            path=relative(data['script_path'])
            if not path.lower().endswith('.sh'):raise ValueError('请选择 .sh 文件')
            scope=data.get('script_scope','project')
            if scope not in ('project','task') or scope=='task' and not existing:raise ValueError('脚本来源无效')
            root=engine.local_project(project_id)/('code' if scope=='project' else 'tasks/'+task_id+'/files')
            p=confined(root,path)
            if not p.is_file():raise ValueError('选中的脚本已不存在，请刷新脚本列表')
            path,content=shell_script(path,p.read_bytes() if p.stat().st_size<=SCRIPT_LIMIT else b'x'*(SCRIPT_LIMIT+1))
            data['command']=data.get('command') or script_command(path)
        task=engine.create_task(project_id,data,existing=existing,persist=False)
        target=engine.local_project(project_id)/'tasks'/task['id']/'files';target.mkdir(parents=True,exist_ok=True)
        previous=None;dest=None
        if content is not None:
            dest=confined(target,path)
            dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():
                if not dest.is_file():raise ValueError('任务目录含同名文件夹')
                if dest.stat().st_size>SCRIPT_LIMIT:raise ValueError('同名任务文件过大，拒绝覆盖')
                previous=dest.read_bytes()
            temporary=dest.parent/(uid()+'.tmp')
            try:temporary.write_bytes(content);os.replace(temporary,dest)
            finally:temporary.unlink(missing_ok=True)
            task.update(script_path=path,script_scope='task')
        elif path:task.update(script_path=path,script_scope=data.get('script_scope','project'))
        else:task.pop('script_path',None);task.pop('script_scope',None)
        try:
            with engine.store.lock:
                if existing and engine.store.get_summary('tasks',task_id).get('archived'):raise ValueError('实验已归档，本次修改未保存')
                from .task_archive import check_dependencies
                check_dependencies(engine,task)
                engine.store.put('tasks',task)
        except Exception:
            if dest:
                if previous is None:dest.unlink(missing_ok=True)
                else:dest.write_bytes(previous)
            raise
        return task


def read_folder(source,stage,progress):
    source=Path(source);stage=Path(stage)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():raise ValueError('请提供已存在的本机文件夹绝对地址')
    source=source.resolve();stage.mkdir(parents=True,exist_ok=True)
    collections=[source/f for f in ('experiments.json','tasks.json','batch.json') if (source/f).is_file()]
    if len(collections)>1:raise ValueError('发现多份总配置，请只保留 experiments.json、tasks.json 或 batch.json 中的一份')
    configs=collections;walked=0
    if not configs:
        configs=[]
        for base,dirs,files in os.walk(source,followlinks=False):
            walked+=1
            if walked>20000:raise ValueError('扫描目录过多，请缩小导入文件夹范围')
            dirs[:]=sorted(d for d in dirs if d not in ('.git','.venv','node_modules','__pycache__'))
            for d in dirs:
                if (Path(base)/d).is_symlink():raise ValueError('配置文件夹不支持符号链接')
            for f in files:
                if f=='experiment.json':configs.append(Path(base)/f)
            progress('scanning')(0,0,str(base),len(configs),0)
            if len(configs)>500:raise ValueError('一次最多导入 500 个实验')
    if not configs:raise ValueError('未找到 experiments.json、tasks.json、batch.json 或子目录中的 experiment.json')
    entries=[];fingerprints=[];size=0;file_count=0
    for config in sorted(configs):
        config=confined(source,config.relative_to(source).as_posix())
        if config.stat().st_size>2*1024**2:raise ValueError('配置文件最多 2 MB')
        raw=config.read_bytes();fingerprints.append({'path':config.relative_to(source).as_posix(),'sha256':hashlib.sha256(raw).hexdigest()})
        try:document=json.loads(raw.decode('utf-8-sig'))
        except (ValueError,UnicodeError) as exc:raise ValueError(str(config)+'：JSON 配置无效：'+str(exc))
        if isinstance(document,list):defaults={};rows=document
        elif isinstance(document,dict):
            defaults=document.get('defaults',{});rows=document.get('experiments',document.get('tasks',[document]))
            if document.get('version',1)!=1:raise ValueError('不支持的配置版本')
        else:raise ValueError('配置须为对象或实验数组')
        if not isinstance(defaults,dict) or not isinstance(rows,list):raise ValueError('defaults 须为对象，experiments 须为数组')
        for row in rows:
            if not isinstance(row,dict):raise ValueError('每项实验须为对象')
            item={**defaults,**row};unknown=set(item)-FIELDS-{'key','script','args','files','notes','server_names','version'}
            if unknown:raise ValueError('未知配置字段：'+', '.join(sorted(unknown)))
            index=len(entries)
            if index>=500:raise ValueError('一次最多导入 500 个实验')
            has_script=bool(item.get('script'));has_command=bool(item.get('command'))
            if has_script==has_command:raise ValueError('每项实验须提供 script 或 command 中的一种')
            if has_script:item['script']=relative(item['script'])
            paths=item.get('files',[])
            if not isinstance(paths,list) or any(not isinstance(p,str) for p in paths):raise ValueError('files 须为相对文件路径数组')
            if has_script:paths=[item['script'],*paths]
            snapshots={};seen=set()
            for rel in dict.fromkeys(paths):
                file_count+=1
                if file_count>10000:raise ValueError('整批任务专属输入最多 10000 个文件')
                rel=relative(rel);p=confined(config.parent,rel)
                if rel.casefold() in seen:raise ValueError('任务输入有重复或大小写冲突：'+rel)
                seen.add(rel.casefold())
                if not p.is_file() or p.stat().st_size>16*1024**2:raise ValueError('任务输入须为文件且最多 16 MB：'+rel)
                before=p.stat();content=p.read_bytes();after=p.stat()
                if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise ValueError('读取期间文件变化：'+rel)
                fingerprints.append({'path':p.relative_to(source).as_posix(),'sha256':hashlib.sha256(content).hexdigest()})
                if has_script and rel==item['script']:rel,content=shell_script(rel,content)
                if rel=='deps' or rel.startswith('deps/'):raise ValueError('deps 是保留目录')
                size+=len(content)
                if size>256*1024**2:raise ValueError('整批任务专属输入最多 256 MB；公共代码和大型数据请使用项目、数据集上传')
                dest=confined(stage,str(index)+'/files/'+rel);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content)
                snapshots[rel]={'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
            if has_script:item['command']=script_command(item['script'],item.get('args',[]))
            elif item.get('args'):raise ValueError('args 仅用于 script；command 中请直接写完整命令')
            item.update(key=str(item.get('key') or item.get('name') or ''),config=config.relative_to(source).as_posix(),input_files=snapshots)
            entries.append(item);progress('copying')(size,0,item['key'],len(entries),0)
    if not entries:raise ValueError('配置未包含实验')
    return {'source_path':str(source),'entries':entries,'fingerprints':fingerprints,'input_bytes':size}


def normalize(engine,project_id,entries,task_ids=None):
    existing=engine.store.all('tasks');project_tasks=[t for t in existing if t['project_id']==project_id]
    keys=[e['key'] for e in entries]
    if any(not k for k in keys) or len(set(keys))!=len(keys):raise ValueError('每个实验须有唯一的 key 或 name')
    ids=task_ids or {key:uid() for key in keys};servers=engine.store.all('servers');configs=[]
    for item in entries:
        data={key:item[key] for key in FIELDS if key in item};data['name']=data.get('name') or item['key']
        deps=item.get('depends_on',[])
        if not isinstance(deps,list) or any(not isinstance(d,str) for d in deps):raise ValueError('depends_on 须为实验 key 或已有实验 ID 的数组')
        resolved=[]
        for dep in deps:
            if dep in ids:resolved.append(ids[dep])
            elif any(t['id']==dep for t in project_tasks):resolved.append(dep)
            else:raise ValueError(item['key']+'：未知前序实验 '+str(dep))
        data['depends_on']=resolved
        if 'dependency_files' in data:
            if not isinstance(data['dependency_files'],dict):raise ValueError('dependency_files 须为前序 key 到 glob 数组的映射')
            data['dependency_files']={ids.get(key,key):value for key,value in data['dependency_files'].items()}
        for field in ('server_ids','datasets'):
            if field in data and (not isinstance(data[field],list) or any(not isinstance(v,str) for v in data[field])):raise ValueError(field+' 须为字符串数组')
        if 'server_names' in item:
            if 'server_ids' in item:raise ValueError('server_names 和 server_ids 请只配置一种')
            if not isinstance(item['server_names'],list):raise ValueError('server_names 须为数组')
            data['server_ids']=[]
            for title in item['server_names']:
                matches=[s for s in servers if s['name']==title]
                if len(matches)!=1:raise ValueError('服务器名称不存在或不唯一：'+str(title))
                data['server_ids'].append(matches[0]['id'])
        note=item.get('notes','')
        if not isinstance(note,str):raise ValueError('notes 须为 Markdown 文本')
        configs.append(data)
    proposed=[{'id':ids[e['key']],'project_id':project_id,'depends_on':d['depends_on'],'seq':max([t.get('seq',0) for t in existing]+[0])+i+1} for i,(e,d) in enumerate(zip(entries,configs))]
    graph={t['id']:t['depends_on'] for t in proposed};visited=set();visiting=set()
    def visit(key):
        if key in visiting:raise ValueError('批量实验依赖存在环')
        if key in visited:return
        visiting.add(key)
        for dep in graph.get(key,[]):visit(dep)
        visiting.remove(key);visited.add(key)
    for key in graph:visit(key)
    tasks=[]
    for item,data,planned in zip(entries,configs,proposed):
        task=engine.create_task(project_id,data,persist=False,known_tasks=existing+proposed,task_id=planned['id'])
        task['seq']=planned['seq']
        if item.get('script'):task.update(script_path=item['script'],script_scope='task')
        tasks.append(task)
    return tasks


class BatchImports:
    def __init__(self,engine):
        self.engine=engine
        self.commit_lock=threading.Lock()

    def preview(self,project_id,source_path,changes=None,job=None):
        from .import_policy import validate,apply
        changes=validate(self.engine,{} if changes is None else changes)
        e=self.engine;e.store.get('projects',project_id);key=uid();stage=confined(e.root/'batch-imports',key)
        expired=[]
        with e.lock:
            active={j.get('operation') for j in e.store.summaries('jobs') if j['status'] in ('queued','running')}
            for prior in e.store.all('batch_imports'):
                if prior['status']=='ready' and time.time()-prior['created_at']>86400 and 'commit:'+json.dumps([prior['id']]) not in active:
                    prior['status']='expired';e.store.put('batch_imports',prior);expired.append(prior['id'])
        for prior_id in expired:run(e.root,'remove',path=str(confined(e.root/'batch-imports',prior_id)),ignore_errors=True)
        def update(kind,event,scratch,pid):
            if kind=='progress':e.progress(job,**{k:v for k,v in event.items() if k!='type'},worker_pid=pid)
        try:
            value=run(e.root,'batch-read',source=str(source_path),stage=str(stage),update=update,check=lambda:e.check_cancel(job))
            entries=apply(value['entries'],changes);tasks=normalize(e,project_id,entries)
            plan=dict(value,id=key,project_id=project_id,status='ready',created_at=time.time(),tasks=tasks,source_entries=value['entries'],entries=entries,changes=changes,configuration_revision=0)
            e.store.put('batch_imports',plan)
            return self.public(plan)
        except Exception:
            if stage.exists():run(e.root,'remove',path=str(stage))
            raise

    def public(self,plan):
        return {key:plan.get(key) for key in ('id','project_id','source_path','status','created_at','input_bytes')}|{'changes':plan.get('changes',{}),'configuration_revision':plan.get('configuration_revision',0),'count':len(plan['tasks']),'configs':sorted({e['config'] for e in plan['entries']}),'tasks':[{**t,'key':item['key'],'script':item.get('script'),'input_count':len(item['input_files']),'notes':item.get('notes','')} for item,t in zip(plan['entries'],plan['tasks'])]}

    def configure(self,key,changes,revision):
        from .import_policy import validate,apply
        changes=validate(self.engine,changes)
        if not self.commit_lock.acquire(blocking=False):raise ValueError('批次正在创建，请等待后台完成')
        try:
            e=self.engine;plan=e.store.get('batch_imports',key)
            if plan['status']!='ready':raise ValueError('只有未创建的有效预览可以调整，请重新读取')
            if type(revision)!=int or revision!=plan.get('configuration_revision',0):raise ValueError('预览已更新，请重新打开后调整')
            source=plan.get('source_entries',plan['entries']);entries=apply(source,changes)
            tasks=normalize(e,plan['project_id'],entries,{item['key']:task['id'] for item,task in zip(plan['entries'],plan['tasks'])})
            plan.update(source_entries=source,entries=entries,tasks=tasks,changes=changes,configuration_revision=revision+1)
            e.store.put('batch_imports',plan)
            return self.public(plan)
        finally:self.commit_lock.release()

    def commit(self,key,revision=None,job=None):
        # Concurrent confirmations must not publish or roll back the same task
        # directories. Keep this independent of the scheduler/request lock.
        with self.commit_lock:
            return self._commit(key,revision,job)

    def _commit(self,key,revision=None,job=None):
        e=self.engine;plan=e.store.get('batch_imports',key)
        if plan['status']=='success':return self.public(plan)
        if revision is not None and (type(revision)!=int or revision!=plan.get('configuration_revision',0)):raise ValueError('配置预览已更新，请确认最新配置后再创建')
        if plan['status']=='expired':raise ValueError('批量预览已过期，请重新读取配置')
        stage=confined(e.root/'batch-imports',key)
        e.progress(job,phase='verifying',bytes=0,total=0)
        run(e.root,'batch-verify',source=plan['source_path'],fingerprints=plan['fingerprints'])
        with e.lock:
            plan=e.store.get('batch_imports',key)
            if plan['status']=='success':return self.public(plan)
            if plan['status']=='expired':raise ValueError('批量预览已过期，请重新读取配置')
            if any(t.get('batch_import_id')==key for t in e.store.all('tasks')):raise ValueError('此批次已有创建记录，不能重复创建')
            tasks=normalize(e,plan['project_id'],plan['entries'],{item['key']:task['id'] for item,task in zip(plan['entries'],plan['tasks'])})
            plan.update(status='publishing');e.store.put('batch_imports',plan)
        target=e.local_project(plan['project_id'])/'tasks'
        try:
            run(e.root,'batch-publish',stage=str(stage),target=str(target),entries=plan['entries'],tasks=tasks)
            records=[]
            with e.lock,e.store.lock:
                # Revalidate references and scheduling settings before the one
                # atomic database transaction; no partial batch becomes visible.
                tasks=normalize(e,plan['project_id'],plan['entries'],{item['key']:task['id'] for item,task in zip(plan['entries'],tasks)})
                for task,item in zip(tasks,plan['entries']):
                    task['batch_import_id']=key;records.append(('tasks',task))
                    if item.get('notes'):records.append(('notes',{'id':uid(),'task_id':task['id'],'project_id':plan['project_id'],'body':item['notes'],'created_at':time.time(),'updated_at':time.time()}))
                plan.update(status='success',tasks=tasks,finished_at=time.time());records.append(('batch_imports',plan));e.store.put_many(records)
        except Exception:
            run(e.root,'batch-rollback',target=str(target),tasks=tasks)
            plan.update(status='ready');e.store.put('batch_imports',plan);raise
        e.progress(job,phase='complete',bytes=plan['input_bytes'],total=plan['input_bytes'],files_done=len(tasks),files_total=len(tasks))
        e.store.event('批量创建实验：'+str(len(tasks))+' 个草稿')
        try:run(e.root,'remove',path=str(stage))
        except Exception as exc:e.store.event('实验已创建；批量预览暂存清理失败：'+str(exc),'warning')
        return self.public(plan)
