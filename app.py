from __future__ import annotations

import argparse
import atexit
import hmac
import hashlib
import io
import json
import os
import secrets
import shutil
import socket
import tempfile
import time
import threading
import zipfile
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, session, stream_with_context, url_for
from werkzeug.exceptions import HTTPException
import paramiko

from scheduler.engine import Engine
from scheduler.files import confined, extract_zip, file_metadata, manifest, name, relative
from scheduler.task_imports import BatchImports,save_task,SCRIPT_LIMIT
from scheduler.file_process import run as file_process
from scheduler.policy import ACTIVE
from scheduler.public_files import PublicFiles
from scheduler.remote import read_ssh_config
from scheduler.storage import uid
from scheduler.graph import outputs, node
from scheduler.notes import Notes
from scheduler.uploads import Uploads, CHUNK_SIZE, BUSY
from scheduler.local_storage import LocalStorage, saved_root, root_lock
from scheduler.logs import read_local, CHUNK_BYTES
from flask import Request


def create_app(root=None, start=False, remote_factory=None, config_path=None, runtime=False, initial_lock=None):
    app=Flask(__name__)
    app.config['TEMPLATES_AUTO_RELOAD']=True
    asset_versions={}
    asset_versions_lock=threading.Lock()

    def asset_url(filename):
        # Hash only changed frontend assets; never scan project files or datasets.
        path=Path(app.static_folder)/filename
        stat=path.stat()
        signature=(stat.st_mtime_ns,stat.st_ctime_ns,stat.st_size)
        with asset_versions_lock:
            cached=asset_versions.get(filename)
            if cached is None or cached[0]!=signature:
                cached=(signature,hashlib.sha256(path.read_bytes()).hexdigest()[:16])
                asset_versions[filename]=cached
        return url_for('static',filename=filename,v=cached[1])

    app.jinja_env.globals['asset_url']=asset_url
    install=Path(__file__).parent.resolve()
    config_path=Path(config_path or (Path(root).parent/(Path(root).name+'.location.json') if root else install/'storage-location.json'))
    root=Path(root or os.environ.get('COMPUTE_DATA_DIR') or saved_root(config_path,install/'data')).resolve()
    engine=Engine(root, **({'remote_factory':remote_factory} if remote_factory else {}))
    app.extensions['engine']=engine
    notes=Notes(engine.store)
    app.extensions['notes']=notes
    batch_imports=BatchImports(engine)
    app.extensions['batch_imports']=batch_imports
    local_storage=LocalStorage(engine,config_path,install,runtime=runtime,initial_lock=initial_lock)
    app.extensions['local_storage']=local_storage
    uploads=Uploads(engine,int(os.environ.get('COMPUTE_UPLOAD_LIMIT',200*1024**3)))
    engine.uploads=uploads
    app.extensions['uploads']=uploads
    public_files=PublicFiles(engine)
    app.extensions['public_files']=public_files
    class ManagedRequest(Request):
        def _get_file_stream(self,total_content_length,content_type,filename=None,content_length=None):
            return tempfile.TemporaryFile(dir=str(engine.root/'tmp'))
    app.request_class=ManagedRequest
    secret=root/'session.key'
    token=root/'access-token.txt'
    if not secret.exists():
        secret.write_text(secrets.token_hex(32),encoding='utf-8')
    if not token.exists():
        token.write_text(secrets.token_urlsafe(32),encoding='utf-8')
    app.secret_key=secret.read_text(encoding='utf-8').strip()
    cookie_id=local_storage.cookie_id
    app.config.update(MAX_CONTENT_LENGTH=int(os.environ.get('COMPUTE_UPLOAD_LIMIT',200*1024**3)), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_NAME='compute_session_'+cookie_id)

    @app.before_request
    def authorize():
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('Origin')
            if origin and origin.rstrip('/')!=request.host_url.rstrip('/'):
                return jsonify(error='拒绝跨站请求'),403
        if request.path.startswith('/api/'):
            if not session.get('authorized'):
                return jsonify(error='请先使用本机访问令牌登录'),401
            if request.method not in ('GET','HEAD') and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session.get('csrf','') or 'missing'):
                return jsonify(error='CSRF 校验失败，请刷新页面'),403
            if request.method not in ('GET','HEAD') and engine.storage_moving and not request.path.endswith('/interrupt'):
                return jsonify(error='本机保存目录正在迁移，请稍后操作；进度可继续查看'),409

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/') or request.path=='/':
            response.headers['Cache-Control']='no-store'
        elif request.path.startswith('/static/'):
            response.headers['Cache-Control']='no-cache, must-revalidate'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc,HTTPException):
            return jsonify(error=exc.description),exc.code
        if isinstance(exc,(ValueError,KeyError,TypeError)):
            return jsonify(error=str(exc)),400
        engine.store.event('API 异常：'+str(exc),'error')
        return jsonify(error=str(exc)),500

    @app.get('/')
    def index():
        session.setdefault('csrf',secrets.token_urlsafe(24))
        return render_template('index.html',authorized=session.get('authorized',False),csrf=session['csrf'],token_path=str(engine.root/'access-token.txt'))

    @app.post('/login')
    def login():
        payload=request.get_json() or {}
        if not hmac.compare_digest(str(payload.get('token','')),(engine.root/'access-token.txt').read_text(encoding='utf-8').strip()):
            time.sleep(0.3)
            return jsonify(error='令牌不正确；读取 '+str(engine.root/'access-token.txt')),403
        session.clear()
        session.update(authorized=True,csrf=secrets.token_urlsafe(24))
        return jsonify(ok=True)

    @app.get('/api/state')
    def state():
        servers=engine.store.all('servers')
        for server in servers:
            server.pop('history',None)
            server['conda']=engine.current_conda(server)
        tasks=engine.store.summaries('tasks')
        datasets=engine.store.summaries('datasets')
        dataset_notes={n['id']:n for n in engine.store.summaries('dataset_notes')}
        for dataset in datasets:dataset['note']=dataset_notes.get(dataset['id'])
        all_uploads=engine.store.all('uploads');all_jobs=engine.store.recent_summaries('jobs',50,('queued','running'))
        active_uploads=BUSY
        visible_uploads=[uploads.public(u) for i,u in enumerate(all_uploads) if i>=len(all_uploads)-100 or u['status'] in active_uploads]
        visible_jobs=[public_job(j) for j in all_jobs]
        return jsonify(runtime_features={'async_runtime':True,'task_environments':True,'content_store':True,'batch_states':True,'batch_queue':True,'import_configuration':True,'project_archive':True,'task_archive':True,'result_tables':True,'result_dataset_tables':True,'dataset_notes':True,'code_sync':True},code_sources=engine.store.all('code_sources'),servers=servers,projects=engine.store.all('projects'),tasks=tasks,notes=engine.store.summaries('notes'),storage=[engine.current_sharing(scope) for scope in ('work','data')],local_storage=local_storage.info(),uploads=visible_uploads,datasets=datasets,replicas=engine.store.summaries('replicas'),jobs=visible_jobs,settings=engine.settings,events=engine.store.events())

    @app.get('/api/experiment-guide')
    def experiment_guide():
        return send_file(install/'docs'/'实验脚本与批量配置指导.md',as_attachment=True)

    @app.get('/api/projects/<project_id>/source')
    def code_source_read(project_id):return jsonify(engine.code_sources.get(project_id))

    @app.post('/api/projects/<project_id>/source')
    def code_source_config(project_id):
        value=engine.code_sources.configure(project_id,request.get_json() or {})
        job=engine.enqueue('同步项目源码目录',engine.code_sources.synchronize,project_id) if value.get('path') and value.get('enabled') else None
        return jsonify(source=value,job=job)

    @app.post('/api/projects/<project_id>/source/sync')
    def code_source_sync(project_id):
        return jsonify(engine.enqueue('立即同步项目源码',engine.code_sources.synchronize_now,project_id)),202

    @app.get('/api/projects/<project_id>/source/history')
    def code_source_history(project_id):
        folder=engine.code_sources.folder(project_id)/'history'
        rows=[]
        for path in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:30]:
            rows.append(json.loads(path.read_text(encoding='utf-8')))
        return jsonify(versions=rows)

    @app.get('/api/projects/<project_id>/source/history/<revision>')
    def code_source_change_detail(project_id,revision):
        if len(revision)!=32 or any(c not in '0123456789abcdef' for c in revision):raise ValueError('无效的代码版本')
        value=json.loads(confined(engine.code_sources.folder(project_id),'changes/'+revision+'.json').read_text(encoding='utf-8'))
        offset=int(request.args.get('offset',0));limit=int(request.args.get('limit',100))
        if offset<0 or not 1<=limit<=200:raise ValueError('分页参数无效')
        changes=value.pop('changes');return jsonify(**value,changes=changes[offset:offset+limit],total=len(changes),offset=offset,limit=limit)

    def public_job(job):
        value=dict(job)
        if (value.get('operation','').startswith(('upload_dataset:','verify_dataset:')) and isinstance(value.get('result'),dict)):
            value['result']={k:v for k,v in value['result'].items() if k!='manifest'}
        return value

    def transfer_page(job_id):
        return engine.transfers.page(job_id,offset=int(request.args.get('offset',0)),limit=int(request.args.get('limit',100)),
                                     query=request.args.get('query','').strip(),server=request.args.get('server',''),status=request.args.get('status',''))

    @app.get('/api/transfers')
    def transfers_list():
        offset=int(request.args.get('offset',0));limit=int(request.args.get('limit',30))
        if offset<0 or not 1<=limit<=100:raise ValueError('分页参数无效')
        all_uploads=engine.store.all('uploads');all_jobs=engine.store.summaries('jobs')
        upload_ids={u['id'] for u in all_uploads};jobs={j['id']:j for j in all_jobs};entries=[]
        for u in all_uploads:entries.append({'kind':'upload','value':uploads.public(u),'time':max(u['created_at'],jobs.get(u.get('job_id'),{}).get('created_at',0))})
        for j in all_jobs:
            op=j.get('operation','')
            if op.startswith('upload_dataset:') or op.startswith('process:') and json.loads(op.split(':',1)[1])[0] not in upload_ids:
                entries.append({'kind':'job','value':public_job(j),'time':j['created_at']})
        entries.sort(key=lambda e:(e['time'],e['value']['id']),reverse=True)
        active=BUSY|{'running'}
        counts={'active':sum(e['value']['status'] in active for e in entries),'success':sum(e['value']['status']=='success' for e in entries),
                'stopped':sum(e['value']['status'] in ('failed','canceled','interrupted') for e in entries)}
        selected=request.args.get('status','all')
        if selected=='active':entries=[e for e in entries if e['value']['status'] in active]
        elif selected=='success':entries=[e for e in entries if e['value']['status']=='success']
        elif selected=='stopped':entries=[e for e in entries if e['value']['status'] in ('failed','canceled','interrupted')]
        elif selected!='all':raise ValueError('记录状态无效')
        return jsonify(entries=entries[offset:offset+limit],total_count=len(entries),offset=offset,limit=limit,counts=counts)

    @app.get('/api/jobs/<job_id>/transfer-details')
    def job_transfer_details(job_id):
        job=engine.store.get('jobs',job_id)
        if not job.get('operation','').startswith(('upload_dataset:','verify_dataset:','process:')):raise ValueError('此操作不是文件传输或核验')
        return jsonify(job=public_job(job),**transfer_page(job_id))

    @app.get('/api/jobs/<job_id>')
    def job_read(job_id):
        return jsonify(job=public_job(engine.store.get('jobs',job_id)))

    @app.get('/api/uploads/<upload_id>/details')
    def upload_details(upload_id):
        upload=engine.store.get('uploads',upload_id)
        offset=int(request.args.get('receipt_offset',0));limit=int(request.args.get('limit',100))
        if offset<0 or not 1<=limit<=200:raise ValueError('分页参数无效')
        query=request.args.get('query','').strip().casefold()
        files,matched=uploads.files.page(upload_id,offset,limit,query)
        jobs=sorted((public_job(j) for j in engine.store.summaries('jobs') if j.get('operation')=='process:'+json.dumps([upload_id],ensure_ascii=False)),key=lambda j:j['created_at'],reverse=True)
        job_id=request.args.get('job_id') or upload.get('job_id')
        if job_id and not any(j['id']==job_id for j in jobs):raise ValueError('操作不属于此上传')
        details=transfer_page(job_id) if job_id else {'files':[],'total_count':0,'matched_count':0,'offset':0,'limit':limit,'summary':[]}
        return jsonify(upload=uploads.public(upload),receipts=files,receipt_total=upload.get('file_count',0),receipt_matched=matched,receipt_offset=offset,
                       jobs=jobs,job=next((j for j in jobs if j['id']==job_id),None),**details)

    @app.get('/api/local-storage')
    def local_storage_info():
        return jsonify(local_storage.info())

    @app.post('/api/local-storage/validate')
    def local_storage_validate():
        return jsonify(local_storage.validate((request.get_json() or {})['path']))

    @app.post('/api/local-storage/migrate')
    def local_storage_migrate():
        return jsonify(engine.enqueue('迁移本机保存目录',local_storage.migrate,(request.get_json() or {})['path'])),202

    @app.post('/api/uploads')
    def uploads_begin():
        return jsonify(uploads.begin(request.get_json() or {})),201

    @app.post('/api/uploads/local')
    def uploads_local():
        return jsonify(uploads.begin_local(request.get_json() or {})),202

    @app.get('/api/uploads/<upload_id>')
    def upload_info(upload_id):
        upload=engine.store.get('uploads',upload_id)
        return jsonify(uploads.public(upload) if request.args.get('summary') else {**uploads.public(upload),'files':uploads.files.all(upload_id)})

    @app.put('/api/uploads/<upload_id>/files/<int:index>')
    def upload_chunk(upload_id,index):
        return jsonify(uploads.chunk(upload_id,index,int(request.args.get('offset',0)),request.stream.read(CHUNK_SIZE+1)))

    @app.post('/api/uploads/<upload_id>/batch')
    def upload_batch(upload_id):
        parts=json.loads(request.headers.get('X-Upload-Batch','[]'));raw=request.stream.read(CHUNK_SIZE+1)
        if not isinstance(parts,list) or not parts or len(parts)>128 or len(raw)>CHUNK_SIZE:raise ValueError('上传批次无效')
        blocks=[];offset=0
        for p in parts:
            length=p['length']
            if not isinstance(length,int) or length<1 or offset+length>len(raw):raise ValueError('上传批次长度无效')
            blocks.append((int(p['index']),int(p['offset']),raw[offset:offset+length]));offset+=length
        if offset!=len(raw):raise ValueError('上传批次长度不匹配')
        return jsonify(uploads.batch(upload_id,blocks))

    @app.post('/api/uploads/<upload_id>/finish')
    def upload_finish(upload_id):
        return jsonify(uploads.finish(upload_id)),202

    @app.post('/api/uploads/<upload_id>/cancel')
    def upload_cancel(upload_id):
        return jsonify(uploads.cancel(upload_id))

    @app.post('/api/uploads/<upload_id>/interrupt')
    def upload_interrupt(upload_id):
        return jsonify(uploads.interrupt(upload_id))

    @app.post('/api/uploads/<upload_id>/resume')
    def upload_resume(upload_id):
        return jsonify(uploads.resume(upload_id,(request.get_json() or {})['files']))

    @app.post('/api/jobs/<job_id>/interrupt')
    def job_interrupt(job_id):
        linked=next((u for u in engine.store.all('uploads') if u.get('job_id')==job_id),None)
        if linked:
            uploads.interrupt(linked['id'])
            return jsonify(engine.store.get('jobs',job_id))
        return jsonify(engine.cancel_job(job_id))

    def receive_legacy(data,files):
        metadata=[]
        for file,path in files:
            file.stream.seek(0,2);length=file.stream.tell();file.stream.seek(0)
            metadata.append({'path':path,'size':length})
        upload=uploads.begin({**data,'files':metadata})
        try:
            for index,(file,path) in enumerate(files):
                offset=0
                for block in iter(lambda:file.stream.read(CHUNK_SIZE),b''):
                    uploads.chunk(upload['id'],index,offset,block);offset+=len(block)
            result=uploads.finish(upload['id'])
            return result,engine.store.get('jobs',result['job_id'])
        except Exception:
            uploads.cancel(upload['id']);raise

    @app.get('/api/ssh-config')
    def ssh_config():
        _,hosts=read_ssh_config()
        return jsonify(hosts=hosts)

    @app.post('/api/servers')
    def servers():
        return jsonify(engine.save_server(request.get_json() or {}))

    @app.post('/api/servers/<server_id>/action')
    def server_action(server_id):
        data=request.get_json() or {}
        action=data['action']
        if action=='benchmark':
            return jsonify(engine.enqueue('GPU 基准测试',engine.benchmark,server_id,int(data['gpu_index'])))
        if action=='connect':
            def connect(job=None):
                server=engine.store.get('servers',server_id)
                key,snapshot,error=engine.poll_one(server)
                with engine.lock:
                    server=engine.store.get('servers',key)
                    server.update(connected=not error,error=error or '')
                    if snapshot:
                        server['snapshot']=snapshot
                    engine.store.put('servers',server)
                if error:
                    raise ValueError(error)
                return {'connected':True}
            return jsonify(engine.enqueue('连接服务器',connect))
        with engine.lock:
            server=engine.store.get('servers',server_id)
            if action=='pause':
                server['paused']=not server.get('paused',False)
            elif action=='enable':
                server['enabled']=not server.get('enabled',True)
            elif action=='gpu':
                key=str(data['gpu_uuid'])
                blocked=server.setdefault('blocked_gpus',[])
                if key in blocked:
                    blocked.remove(key)
                else:
                    blocked.append(key)
            else:
                raise ValueError('未知服务器操作')
            engine.store.put('servers',server)
            return jsonify(server)

    @app.get('/api/servers/<server_id>/environments')
    def server_environments(server_id):
        return jsonify(engine.current_conda(engine.store.get('servers',server_id)))

    @app.post('/api/servers/<server_id>/environments/refresh')
    def server_environments_refresh(server_id):
        engine.store.get('servers',server_id)
        return jsonify(engine.enqueue('读取服务器 Conda 环境',engine.discover_conda,server_id)),202

    @app.post('/api/projects')
    def projects():
        return jsonify(engine.create_project(request.get_json() or {}))

    @app.post('/api/projects/<project_id>/archive')
    def project_archive(project_id):
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交项目归档配置')
        return jsonify(engine.archive_project(project_id,data.get('archived')))

    @app.get('/api/projects/<project_id>/results')
    def project_results_read(project_id):
        from scheduler.result_tables import read,summarize
        value=read(engine,project_id,request.args.get('dataset',''))
        return jsonify(**value,summary=summarize(value))

    @app.get('/api/projects/<project_id>/results/datasets')
    def project_results_datasets(project_id):
        from scheduler.result_tables import scopes
        return jsonify(scopes(engine,project_id))

    @app.post('/api/projects/<project_id>/results/assign-dataset')
    def project_results_assign_dataset(project_id):
        from scheduler.result_tables import assign_dataset
        return jsonify(assign_dataset(engine,project_id,request.get_json()))

    @app.post('/api/projects/<project_id>/results')
    def project_results_save(project_id):
        from scheduler.result_tables import save
        if request.content_length and request.content_length>12*1024**2:abort(413,description='结果表最多 12 MB，请减少长备注或记录数')
        return jsonify(save(engine,project_id,request.get_json(),request.args.get('dataset','')))

    @app.get('/api/projects/<project_id>/results/export')
    def project_results_export(project_id):
        from scheduler.result_tables import read,export_csv
        view=request.args.get('view','rows')
        if view not in ('rows','summary'):raise ValueError('未知导出范围')
        return Response(export_csv(read(engine,project_id,request.args.get('dataset','')),view=='summary'),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename="project-results-'+view+'.csv"'})

    @app.post('/api/projects/<project_id>/tasks')
    def tasks(project_id):
        data,script=task_request()
        return jsonify(save_task(engine,project_id,data,script=script))

    def task_request():
        if request.is_json:return request.get_json() or {},None
        try:data=json.loads(request.form.get('config','{}'))
        except ValueError:raise ValueError('实验配置须为 JSON 对象')
        if not isinstance(data,dict):raise ValueError('实验配置须为 JSON 对象')
        script=request.files.get('script')
        return data,(script.filename,script.read(SCRIPT_LIMIT+1)) if script and script.filename else None

    @app.get('/api/projects/<project_id>/scripts')
    def project_scripts(project_id):
        project=engine.local_project(project_id);roots=[('project',str(project/'code'))]
        task_id=request.args.get('task_id')
        if task_id:
            task=engine.store.get('tasks',task_id)
            if task['project_id']!=project_id:raise ValueError('任务不属于此项目')
            roots.append(('task',str(project/'tasks'/task_id/'files')))
        return jsonify(scripts=file_process(engine.root,'scripts',roots=roots))

    @app.post('/api/projects/<project_id>/batch-import/preview')
    def batch_preview(project_id):
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交批量导入配置对象')
        engine.store.get('projects',project_id)
        source=Path(str(data.get('source_path','')))
        if not source.is_absolute() or not source.is_dir() or source.is_symlink():raise ValueError('请填写已存在的本机文件夹绝对地址')
        from scheduler.import_policy import validate
        changes=validate(engine,data.get('changes',{}))
        return jsonify(engine.enqueue('读取批量实验配置',batch_imports.preview,project_id,str(source),changes)),202

    @app.get('/api/batch-imports/<batch_id>')
    def batch_read(batch_id):
        return jsonify(batch_imports.public(engine.store.get('batch_imports',batch_id)))

    @app.post('/api/batch-imports/<batch_id>/configure')
    def batch_configure(batch_id):
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交批量导入配置对象')
        return jsonify(batch_imports.configure(batch_id,data.get('changes',{}),data.get('configuration_revision')))

    @app.post('/api/batch-imports/<batch_id>/commit')
    def batch_commit(batch_id):
        plan=engine.store.get('batch_imports',batch_id)
        if plan['status']=='success':return jsonify(batch_imports.public(plan))
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交批量导入配置对象')
        return jsonify(engine.enqueue('批量创建实验草稿',batch_imports.commit,batch_id,data.get('configuration_revision'))),202

    @app.post('/api/projects/<project_id>/environments')
    def project_environments(project_id):
        return jsonify(engine.project_environments(project_id,request.get_json() or {}))

    @app.get('/api/projects/<project_id>/graph')
    def project_graph(project_id):
        engine.store.get('projects',project_id)
        note_list=engine.store.summaries('notes')
        nodes = [node(t,note_list) for t in sorted(engine.store.all('tasks'),key=lambda t:t['seq']) if t['project_id']==project_id]
        return jsonify(nodes=nodes,edges=[{'source':d,'target':t['id']} for t in nodes for d in t['depends_on']])

    @app.get('/api/tasks/<task_id>/inspection')
    def inspection(task_id):
        return jsonify(node(engine.store.get('tasks',task_id),engine.store.summaries('notes')))

    @app.post('/api/tasks/<task_id>/notes')
    def task_notes(task_id):
        task=engine.store.get('tasks',task_id)
        data=request.get_json() or {}
        note_id=data.get('id')
        if data.get('delete'):
            if not note_id:raise ValueError('缺少记录 ID')
            notes.delete(task_id,note_id)
            return jsonify(ok=True)
        return jsonify(notes.save(task,data.get('body',''),note_id))

    @app.post('/api/tasks/<task_id>/notes/file')
    def note_file_import(task_id):
        task=engine.store.get('tasks',task_id)
        upload=request.files.get('file')
        if not upload:raise ValueError('请选择 Markdown 文件')
        return jsonify(notes.save_stream(task,upload.stream,request.form.get('id') or None))

    @app.get('/api/tasks/<task_id>/notes/<note_id>/text')
    def note_text(task_id,note_id):
        value=notes.get(task_id,note_id)
        download=bool(request.args.get('download'))
        if value.get('body_file'):
            path=notes.path(value)
            if not path.is_file():abort(404,description='Notes 源文件不存在')
            return send_file(path,mimetype='text/plain; charset=utf-8',as_attachment=download,download_name='note-'+note_id+'.md')
        response=Response(value['body'],content_type='text/plain; charset=utf-8')
        if download:response.headers['Content-Disposition']='attachment; filename="note-'+uid()+'.md"'
        return response

    @app.post('/api/storage/scan')
    def storage_scan():
        scope=(request.get_json() or {}).get('scope','work')
        if scope not in ('work','data'):
            raise ValueError('未知存储类型')
        return jsonify(engine.enqueue('核验'+('工作' if scope=='work' else '数据')+'目录共享',engine.detect_storage,scope))

    @app.post('/api/tasks/<task_id>/action')
    def task_action(task_id):
        action=(request.get_json() or {})['action']
        current=engine.store.get('tasks',task_id)
        if action in ('cancel','pause','resume') and current['status'] in ('staging','launching','running','suspended','canceling'):
            label={'cancel':'请求中断整个实验','sync':'重试实验回传','pause':'暂停实验','resume':'继续实验运行'}[action]
            intent=engine.prepare_control(task_id,action)
            result=engine.enqueue(label,engine.perform_control_request,task_id,action,intent.get('control_request_id'))
            return jsonify({**result,'task':engine.store.summary('tasks',engine.store.get('tasks',task_id))})
        return jsonify(engine.store.summary('tasks',engine.task_action(task_id,action)))

    @app.post('/api/tasks/<task_id>/update')
    def update_task(task_id):
        data,script=task_request();task=engine.store.get('tasks',task_id)
        return jsonify(save_task(engine,task['project_id'],data,script=script,task_id=task_id))

    @app.post('/api/tasks/scheduling')
    def batch_scheduling():
        from scheduler.scheduling_edits import update_policies
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('请提交实验列表和调度配置')
        return jsonify(update_policies(engine,data.get('task_ids'),data.get('changes')))

    @app.post('/api/tasks/batch-action/preview')
    def batch_action_preview():
        from scheduler.batch_states import preview
        data=request.get_json() or {}
        return jsonify(preview(engine,data.get('task_ids'),data.get('action')))

    @app.post('/api/tasks/archive/preview')
    def task_archive_preview():
        from scheduler.task_archive import preview
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交实验列表和归档状态')
        return jsonify(preview(engine,data.get('task_ids'),data.get('archived')))

    @app.post('/api/tasks/archive')
    def task_archive_apply():
        from scheduler.task_archive import apply
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交实验列表和归档状态')
        return jsonify(apply(engine,data.get('task_ids'),data.get('archived')))

    @app.post('/api/tasks/batch-action')
    def batch_action():
        from scheduler.batch_states import preview
        data=request.get_json() or {};plan=preview(engine,data.get('task_ids'),data.get('action'))
        if not plan['eligible']:raise ValueError('没有可执行此操作的实验，请查看预览中的原因')
        label='批量入队' if data['action']=='queue' else '批量实验状态：'+data['action']
        return jsonify(engine.enqueue(label,engine.batch_state_action,data['task_ids'],data['action'])),202

    @app.get('/api/projects/<project_id>/storage')
    def content_storage(project_id):
        engine.store.get('projects',project_id)
        try:value=engine.store.get('content_reports',project_id)
        except ValueError:value=None
        return jsonify(report=value,caches=[v for v in engine.store.all('content_caches') if v['project_id']==project_id])

    @app.post('/api/projects/<project_id>/storage/refresh')
    def content_storage_refresh(project_id):
        engine.store.get('projects',project_id)
        return jsonify(engine.enqueue('统计项目存储',engine.report_content_storage,project_id)),202

    @app.get('/api/projects/<project_id>/runs/<attempt_id>/files')
    def content_run_files(project_id,attempt_id):
        from scheduler.content import Content
        pool=Content(engine,project_id);scope=request.args.get('scope','outputs');files=pool.run_files(attempt_id,scope)
        if request.args.get('path'):
            path=relative(request.args['path']);item=files.get(path)
            if not item:raise ValueError('文件不存在')
            if item.get('sha256'):
                if not pool.contains(item['sha256'],item['size']):raise ValueError('文件内容已缺失或损坏')
                source=pool.path(item['sha256'])
            else:source=confined(pool.project/'runs'/attempt_id/('input' if scope=='inputs' else 'result'),path)
            return send_file(source,as_attachment=True,download_name=Path(path).name)
        offset=int(request.args.get('offset',0));limit=int(request.args.get('limit',100))
        if offset<0 or not 1<=limit<=200:raise ValueError('分页参数无效')
        query=request.args.get('query','').casefold();rows=[{'path':p,**v} for p,v in sorted(files.items()) if query in p.casefold()]
        return jsonify(files=rows[offset:offset+limit],total=len(rows),offset=offset,scope=scope)

    @app.post('/api/projects/<project_id>/storage/compact')
    def content_compact(project_id):
        engine.store.get('projects',project_id);data=request.get_json() or {};ids=data.get('server_ids',[])
        if not isinstance(ids,list):raise ValueError('服务器须为列表')
        for sid in ids:engine.store.get('servers',sid)
        return jsonify(engine.enqueue('整理项目历史重复文件',engine.compact_content,project_id,ids)),202

    @app.post('/api/projects/<project_id>/storage/scan')
    def content_scan(project_id):
        engine.store.get('projects',project_id);data=request.get_json() or {};sid=data.get('server_id');engine.store.get('servers',sid)
        return jsonify(engine.enqueue('核验服务器内容索引',engine.scan_content_cache,project_id,sid)),202

    @app.post('/api/tasks/<task_id>/input-plan')
    def content_plan(task_id):
        engine.store.get('tasks',task_id)
        return jsonify(engine.enqueue('计算实验增量传输计划',engine.report_input_plan,task_id)),202

    @app.get('/api/tasks/<task_id>/input-plan')
    def content_plan_read(task_id):
        engine.store.get('tasks',task_id);value=engine.store.get('input_plans',task_id)
        return jsonify(value)

    @app.post('/api/settings')
    def settings():
        data=request.get_json() or {}
        if not isinstance(data,dict):raise ValueError('请提交有效的设置对象')
        with engine.store.lock:
            updated=dict(engine.settings)
            for field,low,high in [('poll_seconds',2,300),('history_samples',1,120),('stale_seconds',5,600),('zero_memory_reservation_mb',1,100000),('cleanup_check_seconds',60,86400),('cache_scan_hours',1,8760),('interruption_retry_limit',0,20),('interruption_retry_delay',0,3600)]:
                if field in data:
                    updated[field]=engine.numeric(data[field],low,high,True)
            if 'paused' in data:
                if not isinstance(data['paused'],bool):raise ValueError('暂停调度必须为布尔值')
                updated['paused']=data['paused']
            if 'auto_requeue_interrupted' in data:
                if not isinstance(data['auto_requeue_interrupted'],bool):raise ValueError('异常自动入队必须为布尔值')
                updated['auto_requeue_interrupted']=data['auto_requeue_interrupted']
            engine.store.put('settings',updated)
            engine.settings=updated
            engine.wake_event.set()
        return jsonify(engine.settings)

    @app.post('/api/tasks/failed-workspaces/cleanup')
    def failed_workspace_cleanup():
        return jsonify(engine.enqueue('清理已结束的失败运行副本',engine.cleanup_failed_workspaces))

    def scope_root(project_id,task_id=None,editing=False):
        project=engine.local_project(project_id)
        if task_id:
            task=engine.store.get('tasks',task_id)
            if task['project_id']!=project_id:
                raise ValueError('任务不属于该项目')
            if editing and task['status'] in ACTIVE:
                raise ValueError('任务执行或回传中，不能修改输入')
            if editing and uploads.pending(project_id,task_id):
                raise ValueError('文件正在后台导入，请完成后再编辑')
            return project/'tasks'/task_id/'files'
        if editing and uploads.pending(project_id):
            raise ValueError('项目文件正在后台导入，请完成后再编辑')
        return project/'code'

    @app.get('/api/projects/<project_id>/files')
    def files_list(project_id):
        target=scope_root(project_id,request.args.get('task_id'))
        return jsonify(files=manifest(target),local_path=str(target))

    @app.get('/api/projects/<project_id>/file-index')
    def project_file_index(project_id):
        target=scope_root(project_id,request.args.get('task_id'))
        offset=int(request.args.get('offset',0));limit=int(request.args.get('limit',100))
        if offset<0 or not 1<=limit<=200:raise ValueError('分页参数无效')
        inventory=file_metadata(target)
        query=request.args.get('query','').strip().casefold()
        selected=[v for v in inventory if query in v['path'].casefold()]
        return jsonify(files=selected[offset:offset+limit],total_count=len(inventory),matched_count=len(selected),
                       total_bytes=sum(v['size'] for v in inventory),offset=offset,limit=limit,local_path=str(target))

    @app.post('/api/projects/<project_id>/files')
    def files_upload(project_id):
        files=request.files.getlist('files')
        paths=request.form.getlist('paths')
        data={'kind':'project','project_id':project_id,'task_id':request.form.get('task_id'),'mode':request.form.get('mode','files')}
        upload,job=receive_legacy(data,[(f,paths[i] if i<len(paths) else f.filename) for i,f in enumerate(files)])
        return jsonify(upload=upload,job=job),202

    @app.post('/api/projects/<project_id>/files/delete')
    def public_files_delete(project_id):
        data=request.get_json() or {}
        if data.get('task_id'):
            raise ValueError('此操作仅用于项目公共文件')
        paths,all_files=data.get('paths',[]),data.get('all',False)
        with engine.lock:
            _,paths=public_files.validate(project_id,paths,all_files)
            job=engine.enqueue('清除公共文件 · '+engine.store.get('projects',project_id)['name'],public_files.clear_public_files,project_id,paths,all_files)
        return jsonify(job=job),202

    @app.get('/api/projects/<project_id>/file')
    def file_read(project_id):
        target=scope_root(project_id,request.args.get('task_id'))
        path=confined(target,request.args['path'])
        if not path.is_file():
            raise ValueError('文件不存在')
        if request.args.get('download'):
            return send_file(path,as_attachment=True)
        if path.stat().st_size>2*1024**2:
            raise ValueError('文本预览最多 2 MB，请下载文件')
        return jsonify(text=path.read_text(encoding='utf-8'),path=request.args['path'])

    @app.post('/api/projects/<project_id>/file')
    def file_write(project_id):
        data=request.get_json() or {}
        with engine.lock:
            if not data.get('task_id') and engine.code_sources.bound(project_id):raise ValueError('已绑定源码目录，请在原目录修改，或先解绑自动同步')
            target=scope_root(project_id,data.get('task_id'),editing=True)
            rel=relative(data['path'])
            if rel=='deps' or rel.startswith('deps/'):
                raise ValueError('deps 是保留目录')
            path=confined(target,rel)
            if path.exists():
                backup=confined(engine.local_project(project_id)/'history'/uid(),rel)
                backup.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,backup)
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('w',encoding='utf-8',newline='\n') as output:
                output.write(str(data.get('text','')))
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/clean')
    def clean(project_id):
        data=request.get_json() or {}
        return jsonify(engine.enqueue('清理项目远程副本',engine.clean_confirmed,project_id,data.get('server_ids'),data.get('confirmed_server_ids')))

    @app.post('/api/projects/<project_id>/clean-preview')
    def clean_preview(project_id):
        return jsonify(engine.enqueue('核验清理影响范围',engine.cleanup_plan,project_id,(request.get_json() or {}).get('server_ids')))

    @app.get('/api/projects/<project_id>/replicas/<server_id>')
    def replica(project_id,server_id):
        return jsonify(engine.store.get('replicas',project_id+':'+server_id))

    @app.post('/api/projects/<project_id>/scan')
    def scan(project_id):
        data=request.get_json() or {}
        return jsonify(engine.enqueue('核验远程文件清单',engine.refresh_replica,project_id,data['server_id']))

    log_cache={};log_inflight=set();log_mutex=threading.Lock()

    def log_context(task_id):
        task=engine.store.get('tasks',task_id)
        attempt=request.args.get('attempt_id') or task.get('attempt_id') or next((a['attempt_id'] for a in reversed(task.get('previous_attempts',[]))),None)
        record=task if attempt and attempt==task.get('attempt_id') else next((a for a in task.get('previous_attempts',[]) if a['attempt_id']==attempt),None)
        if attempt and record is None:raise ValueError('运行记录不属于此实验')
        local=confined(engine.local_project(task['project_id']),f'runs/{attempt}/control/run.log') if attempt else None
        current=bool(attempt and attempt==task.get('attempt_id') and task['status'] in ACTIVE)
        return task,attempt,record,local,current

    @app.get('/api/tasks/<task_id>/log')
    def task_log(task_id):
        task,attempt,record,local,current=log_context(task_id)
        mode=request.args.get('mode','tail');offset=int(request.args.get('offset',0));limit=int(request.args.get('limit',CHUNK_BYTES))
        if mode not in ('tail','full') or offset<0 or not 1<=limit<=CHUNK_BYTES:raise ValueError('日志分页参数无效')
        if engine.settings.get('demo'):
            return jsonify(text='[演示日志] 实验数据为模拟值；真实实验的 stdout / stderr 将显示在这里。',size=0,lines=1,has_more=False,offset=0,next_offset=0,source='demo',attempt_id=attempt)
        if not attempt:return jsonify(text=task.get('reason') or '尚无运行日志',size=0,has_more=False,lines=0,attempt_id=None)
        info=local.stat() if local.is_file() else None
        key=(task_id,attempt,mode,offset,limit,current,info.st_size if info else None,info.st_mtime_ns if info else None)
        cached=log_cache.get(key)
        if mode=='tail' and cached and time.monotonic()-cached[0]<2:return jsonify(cached[1])
        flight=(task_id,attempt)
        with log_mutex:
            if flight in log_inflight:return jsonify(pending=True,attempt_id=attempt,updated_at=time.time())
            log_inflight.add(flight)
        try:
            if local.is_file() and not current:
                value=read_local(local,mode,offset,limit)
            else:
                server=engine.store.get('servers',record['server_id'])
                try:value=engine.remote(server).read_log({'project_id':task['project_id'],'attempt_id':attempt},mode,offset,limit)
                except (OSError,EOFError,paramiko.SSHException) as exc:
                    if not local.is_file():raise
                    value=read_local(local,mode,offset,limit);value['warning']='服务器日志暂不可读，显示已回传版本：'+str(exc)
            value.update(attempt_id=attempt,updated_at=time.time())
            if mode=='tail':
                if len(log_cache)>128:log_cache.clear()
                log_cache[key]=(time.monotonic(),value)
            return jsonify(value)
        finally:
            with log_mutex:log_inflight.discard(flight)

    @app.get('/api/tasks/<task_id>/log/download')
    def task_log_download(task_id):
        task,attempt,record,local,current=log_context(task_id)
        if not attempt:raise ValueError('尚无运行日志')
        if engine.settings.get('demo'):return Response('[演示日志] 尚无真实运行日志。',content_type='text/plain; charset=utf-8')
        filename='run-'+attempt+'.log'
        if local.is_file() and not current:return send_file(local,as_attachment=True,download_name=filename)
        server=engine.store.get('servers',record['server_id']);remote=engine.remote(server)
        metadata=remote.read_log({'project_id':task['project_id'],'attempt_id':attempt},'full',0,1)
        if not metadata.get('exists',True):raise ValueError('运行日志尚未生成')
        return Response(stream_with_context(remote.stream_log({'project_id':task['project_id'],'attempt_id':attempt})),content_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="'+filename+'"'})

    @app.get('/api/tasks/<task_id>/artifacts')
    def artifacts(task_id):
        task=engine.store.get('tasks',task_id)
        attempt=request.args.get('attempt_id') or task.get('attempt_id')
        valid=[task.get('attempt_id')]+[a['attempt_id'] for a in task.get('previous_attempts',[])]
        if not attempt or attempt not in valid:
            raise ValueError('无此任务的本机结果')
        result=engine.local_project(task['project_id'])/'runs'/attempt/'result'
        from scheduler.content import Content
        pool=Content(engine,task['project_id']);catalog=pool.read_catalog(attempt)
        if request.args.get('path'):
            path=pool.artifact(attempt,request.args['path']) if catalog else confined(result,request.args['path'])
            return send_file(path,as_attachment=True,download_name=Path(request.args['path']).name)
        return jsonify(files=catalog.get('outputs',catalog.get('artifacts',{})) if catalog else manifest(result),local_path=str(pool.root) if catalog else str(result),attempt_id=attempt)

    def dataset_record(dataset_id):
        try:
            return engine.store.get('datasets',dataset_id)
        except ValueError:
            abort(404,description='数据集不存在')

    @app.get('/api/datasets/<dataset_id>')
    def dataset_detail(dataset_id):
        dataset=dataset_record(dataset_id)
        from scheduler.dataset_notes import read
        dataset['note']=read(engine,dataset_id)
        inventory=dataset.pop('manifest',{})
        dataset.update(file_count=len(inventory),total_bytes=sum(v['size'] for v in inventory.values()),
                       local_path=str(engine.root/'datasets'/dataset_id/'files'))
        # Details read the saved inventory and sharing cache; no SSH or rehash.
        sharing=engine.current_sharing('data')
        servers=[]
        for server in engine.store.all('servers'):
            sid=server['id']
            root=sharing.get('states',{}).get(sid,{}).get('root',server.get('data_root',''))
            servers.append({'id':sid,'name':server['name'],'data_root':root,
                            'dataset_path':root.rstrip('/')+'/'+dataset['name'],
                            'replica':dataset.get('servers',{}).get(sid,{}),
                            'shared_with':next(([other for other in group if other!=sid]
                                                for group in sharing.get('groups',[]) if sid in group),[])})
        tasks=[{'id':t['id'],'name':t['name'],'project_id':t['project_id'],'status':t['status']}
               for t in engine.store.all('tasks') if dataset['name'] in t.get('datasets',[])]
        return jsonify(dataset=dataset,servers=servers,sharing=sharing,tasks=tasks)

    @app.get('/api/datasets/<dataset_id>/note')
    def dataset_note(dataset_id):
        from scheduler.dataset_notes import read
        return jsonify(read(engine,dataset_id))

    @app.post('/api/datasets/<dataset_id>/note')
    def dataset_note_save(dataset_id):
        from scheduler.dataset_notes import save
        if request.content_length and request.content_length>8*1024**2:
            abort(413,description='备注请求过大，请控制在 100 万字符以内')
        return jsonify(save(engine,dataset_id,request.get_json()))

    @app.get('/api/datasets/<dataset_id>/files')
    def dataset_files(dataset_id):
        inventory=dataset_record(dataset_id).get('manifest',{})
        offset=int(request.args.get('offset',0))
        limit=int(request.args.get('limit',100))
        if offset<0 or not 1<=limit<=200:
            raise ValueError('分页参数无效：offset ≥ 0，limit 为 1–200')
        query=request.args.get('query','').strip().casefold()
        paths=sorted((p for p in inventory if query in p.casefold()),key=lambda p:(p.casefold(),p))
        return jsonify(files=[{'path':p,**inventory[p]} for p in paths[offset:offset+limit]],
                       total_count=len(inventory),matched_count=len(paths),offset=offset,limit=limit)

    @app.get('/api/datasets/<dataset_id>/file')
    def dataset_file(dataset_id):
        dataset=dataset_record(dataset_id)
        path=relative(request.args.get('path',''))
        if path not in dataset.get('manifest',{}):
            abort(404,description='文件不在数据集清单中')
        target=confined(engine.root/'datasets'/dataset_id/'files',path)
        if not target.is_file():
            abort(404,description='本机文件已被移除或尚未保存')
        return send_file(target,as_attachment=True)

    @app.post('/api/datasets')
    def dataset_import():
        archive=request.files.get('archive')
        if not archive:
            raise ValueError('ZIP archive required')
        upload,job=receive_legacy({'kind':'dataset','mode':'zip','name':request.form['name']},[(archive,archive.filename)])
        return jsonify(dataset=engine.store.get('datasets',upload['dataset_id']),upload=upload,job=job),202

    @app.post('/api/datasets/<dataset_id>/upload')
    def dataset_upload(dataset_id):
        dataset=engine.store.get('datasets',dataset_id)
        if dataset.get('import_status','ready')!='ready' or not dataset.get('manifest'):
            raise ValueError('请先在上传与进度中完成本机导入')
        if any(u.get('dataset_id')==dataset_id and u['status'] in BUSY for u in engine.store.all('uploads')):
            raise ValueError('此数据集的导入或传输正在进行，可在上传与进度中中断')
        if any(j['status'] in ('queued','running') and j.get('operation')=='verify_dataset:'+json.dumps([dataset_id]) for j in engine.store.summaries('jobs')):raise ValueError('此数据集正在核验，请等待完成后同步')
        return jsonify(engine.enqueue('重试数据集上传',engine.upload_dataset,dataset_id))

    @app.post('/api/datasets/<dataset_id>/verify')
    def dataset_verify(dataset_id):
        dataset=engine.store.get('datasets',dataset_id)
        if dataset.get('import_status','ready')!='ready' or not dataset.get('manifest'):raise ValueError('请先完成本机导入')
        return jsonify(engine.enqueue('仅核验数据集 '+dataset['name'],engine.verify_dataset,dataset_id))

    @app.post('/api/datasets/<dataset_id>/delete')
    def dataset_delete(dataset_id):
        from scheduler.dataset_cleanup import guard
        with engine.lock,uploads.lock:
            dataset=guard(engine,dataset_id)
            return jsonify(engine.enqueue('删除本机数据集 '+dataset['name'],engine.delete_dataset,dataset_id)),202

    if start:
        engine.start()
    return app


def main():
    parser=argparse.ArgumentParser(description='算力工坊（Compute Studio）Web Controller')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8776)
    parser.add_argument('--data-dir')
    parser.add_argument('--demo',action='store_true')
    args=parser.parse_args()
    config=Path(__file__).parent/('storage-location-demo.json' if args.demo else 'storage-location.json')
    root=Path(args.data_dir or os.environ.get('COMPUTE_DATA_DIR') or saved_root(config,Path(__file__).parent/('data-demo' if args.demo else 'data'))).resolve()
    lock=root_lock(root)
    if args.demo:
        from scheduler.demo import seed
        app=create_app(root,config_path=config,runtime=True,initial_lock=lock)
        seed(app.extensions['engine'])
    else:
        app=create_app(root,start=True,config_path=config,runtime=True,initial_lock=lock)
    atexit.register(app.extensions['engine'].close)
    print('算力工坊（Compute Studio）: http://%s:%d' % (args.host,args.port))
    print('登录令牌文件：'+str(root/'access-token.txt'))
    from waitress import serve
    serve(app,host=args.host,port=args.port,threads=12,max_request_body_size=app.config['MAX_CONTENT_LENGTH'],channel_timeout=600)


if __name__=='__main__':
    main()
