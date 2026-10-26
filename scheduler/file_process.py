"""Isolated filesystem work. Children never open the controller database."""
import hashlib
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .files import confined, extract_zip, manifest, relative, digest


def run(root, action, update=None, check=None, **options):
    folder=Path(root)/'workers';folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='file-',dir=folder) as scratch:
        scratch=Path(scratch)
        spec=scratch/'request.json'
        spec.write_text(json.dumps(dict(action=action,scratch=str(scratch),parent_pid=os.getpid(),**options),ensure_ascii=False),encoding='utf-8')
        events=queue.Queue()
        with (scratch/'stderr.log').open('w',encoding='utf-8') as errors:
            process=subprocess.Popen([sys.executable,'-X','utf8','-u','-m','scheduler.file_process',str(spec)],
                cwd=str(Path(__file__).resolve().parent.parent),stdout=subprocess.PIPE,stderr=errors,
                text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0))
            def read():
                try:
                    for line in process.stdout:events.put(json.loads(line))
                except Exception as exc:events.put({'type':'error','message':str(exc)})
                finally:events.put(None)
            reader=threading.Thread(target=read,daemon=True);reader.start()
            error=None
            try:
                while True:
                    if check:check()
                    try:event=events.get(timeout=.1)
                    except queue.Empty:continue
                    if event is None:break
                    if event['type']=='error':error=event['message']
                    elif update:update(event['type'],event,scratch,process.pid)
                process.wait()
                if check:check()
                if error or process.returncode:
                    raise ValueError(error or '文件处理进程退出：'+str(process.returncode))
                return json.loads((scratch/'result.json').read_text(encoding='utf-8'))
            finally:
                if process.poll() is None:
                    # Publication rolls back in the child on this signal. Do
                    # not terminate it between moving a file and its backup.
                    (scratch/'stop').touch()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.terminate();process.wait()
                reader.join(timeout=2);process.stdout.close()


def work(spec):
    scratch=Path(spec['scratch']);last=[0,None]
    def check():
        if (scratch/'stop').exists():raise ValueError('文件处理已停止')
    def emit(kind,**data):
        print(json.dumps(dict(type=kind,**data),ensure_ascii=False),flush=True)
    def progress(phase):
        def report(done,total,path='',count=0,files=0,**details):
            check()
            now=time.monotonic()
            if phase==last[1] and now-last[0]<.25:return
            last[:]=[now,phase]
            emit('progress',phase=phase,bytes=done,total=total,path=path,files_done=count,files_total=files,**details)
        return report
    action=spec['action']
    if action=='source-capture':
        from .source_files import capture
        return capture(spec,check,progress('source_sync'))
    if action=='manifest':
        return manifest(spec['path'],progress=progress('hashing'))
    if action=='existing':
        result={};paths=spec['paths'];root=Path(spec['path'])
        for index,rel in enumerate(paths):
            p=confined(root,rel)
            if p.is_file():result[rel]={'size':p.stat().st_size,'sha256':digest(p,lambda n:progress('hashing')(n,p.stat().st_size,rel,index,len(paths)))}
            progress('hashing')(0,0,rel,index+1,len(paths))
        return result
    if action=='remove':
        if spec.get('report_removal'):progress('clearing')(0,0,'项目公共文件')
        if Path(spec['path']).exists():shutil.rmtree(spec['path'],ignore_errors=spec.get('ignore_errors',False))
        return {}
    if action=='batch-verify':
        for index,item in enumerate(spec['fingerprints']):
            p=confined(spec['source'],item['path'])
            if not p.is_file() or p.stat().st_size>16*1024**2 or digest(p,lambda n:progress('verifying')(n,p.stat().st_size,item['path'],index,len(spec['fingerprints'])))!=item['sha256']:
                raise ValueError('预览后来源文件已变化或被移除，请重新预览：'+item['path'])
        return {}
    if action=='batch-rollback':
        for task in spec['tasks']:
            p=confined(spec['target'],task['id'])
            if p.exists():shutil.rmtree(p)
        return {}
    stage=Path(spec.get('stage',spec['scratch']))
    if action=='batch-read':
        from .task_imports import read_folder
        return read_folder(spec['source'],stage,progress)
    if action=='batch-publish':
        created=[];tasks=spec['tasks'];target=Path(spec['target']);target.mkdir(parents=True,exist_ok=True)
        for task in tasks:
            if confined(target,task['id']).exists():raise ValueError('批量实验目标目录已存在，拒绝覆盖')
        try:
            for index,(task,item) in enumerate(zip(tasks,spec['entries'])):
                dest=confined(target,task['id']);dest.mkdir();created.append(dest);(dest/'files').mkdir()
                for rel,expected in item['input_files'].items():
                    source=confined(stage,str(index)+'/files/'+rel)
                    if not source.is_file() or digest(source)!=expected['sha256']:raise ValueError('批量预览暂存文件已变化，请重新预览')
                    output=confined(dest/'files',rel);output.parent.mkdir(parents=True,exist_ok=True)
                    with source.open('rb') as inp,output.open('wb') as out:
                        for block in iter(lambda:inp.read(1024**2),b''):
                            check();out.write(block)
                    progress('publishing')(0,0,task['name'],index,len(tasks))
                progress('publishing')(0,0,task['name'],index+1,len(tasks))
        except Exception:
            for dest in reversed(created):shutil.rmtree(dest)
            raise
        return {}
    if action=='scripts':
        result=[]
        for scope,path in spec['roots']:
            root=Path(path)
            if root.is_symlink():raise ValueError('不支持符号链接目录')
            if not root.exists():continue
            for current,dirs,files in os.walk(root,followlinks=False):
                check();dirs[:]=sorted(d for d in dirs if d not in ('.git','.venv','node_modules','__pycache__'))
                for d in dirs:confined(root,(Path(current)/d).relative_to(root).as_posix())
                for f in sorted(files):
                    if not f.lower().endswith('.sh'):continue
                    rel=(Path(current)/f).relative_to(root).as_posix();p=confined(root,rel)
                    if p.is_file():result.append({'path':rel,'scope':scope,'size':p.stat().st_size})
                    if len(result)>2000:raise ValueError('脚本数量超过 2000，请缩小项目代码目录')
        return result
    if action in ('copy','prepare'):
        if stage.exists():shutil.rmtree(stage)
        stage.mkdir(parents=True)
    if action=='copy':
        source=Path(spec['source']);entries=[]
        if spec['mode']=='zip':entries=[(source.name,source,source.stat())]
        else:
            for current,dirs,files in os.walk(source,followlinks=False):
                check()
                dirs[:]=[d for d in dirs if d not in ('.git','.venv','__pycache__','node_modules')]
                for item in dirs+files:
                    if (Path(current)/item).is_symlink():raise ValueError('不支持符号链接：'+str(Path(current)/item))
                for item in files:
                    p=Path(current)/item;info=p.stat()
                    if not stat.S_ISREG(info.st_mode):raise ValueError('来源含特殊文件：'+str(p))
                    entries.append((relative(p.relative_to(source).as_posix()),p,info))
                    if len(entries)>200000:raise ValueError('文件数量超过 200000')
                    progress('scanning')(0,0,entries[-1][0],len(entries),0)
        if not entries:raise ValueError('来源文件夹为空或已被移除')
        total=sum(info.st_size for _,_,info in entries)
        if total>spec['limit']:raise ValueError('选择的文件超过上传大小限制')
        if shutil.disk_usage(stage).free<total+64*1024**2:raise ValueError('本机保存目录可用空间不足')
        receipts=[{'path':rel,'size':info.st_size,'last_modified':info.st_mtime_ns,'received':0} for rel,_,info in entries]
        (scratch/'receipts.json').write_text(json.dumps(receipts,ensure_ascii=False),encoding='utf-8')
        emit('receipts',count=len(entries),total=total)
        result={};done=0
        for index,(rel,p,before) in enumerate(entries):
            check()
            target=Path(spec['payload'])/'0' if spec['mode']=='zip' else confined(stage,rel)
            target.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256();start=done
            with p.open('rb') as inp,target.open('wb') as out:
                for block in iter(lambda:inp.read(1024**2),b''):
                    out.write(block);h.update(block);done+=len(block)
                    progress('copying')(done,total,rel,index,len(entries),file_bytes=done-start,file_total=before.st_size)
            after=p.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('导入期间来源文件变化：'+rel)
            result[rel]={'size':before.st_size,'sha256':h.hexdigest()}
            progress('copying')(done,total,rel,index+1,len(entries))
        emit('progress',phase='copying',bytes=done,total=total,files_done=len(entries),files_total=len(entries),path=entries[-1][0])
        return result
    if action=='prepare':
        if spec['mode']=='zip':return extract_zip(Path(spec['payload'])/'0',stage,limit=spec['limit'],progress=progress('extracting'),hash_progress=progress('hashing'))
        entries=spec['files']
        for index,item in enumerate(entries):
            dest=confined(stage,item['path']);dest.parent.mkdir(parents=True,exist_ok=True)
            if item['size']==0:dest.touch()
            else:
                source=Path(spec['payload'])/str(index)
                try:os.link(source,dest)
                except OSError:
                    with source.open('rb') as inp,dest.open('wb') as out:
                        for block in iter(lambda:inp.read(1024**2),b''):
                            out.write(block);progress('extracting')(out.tell(),item['size'],item['path'],index,len(entries))
            progress('extracting')(0,0,item['path'],index+1,len(entries))
        return manifest(stage,progress=progress('hashing'))
    if action=='zip':return extract_zip(spec['archive'],stage,limit=spec['limit'],progress=progress('extracting'),hash_progress=progress('hashing'))
    if action=='publish':
        inventory=spec['inventory'];before=spec['before'];target=Path(spec['target']);backup=Path(spec['backup']);published=[];done=0;total=sum(v['size'] for v in inventory.values())
        try:
            for index,(rel,item) in enumerate(inventory.items()):
                if before.get(rel,{}).get('sha256')!=item['sha256']:
                    dest=confined(target,rel);keep=confined(backup,rel)
                    if dest.exists():
                        if not dest.is_file():raise ValueError('目标包含同名目录：'+rel)
                        keep.parent.mkdir(parents=True,exist_ok=True);os.replace(dest,keep)
                    published.append((dest,keep));dest.parent.mkdir(parents=True,exist_ok=True);os.replace(confined(stage,rel),dest)
                done+=item['size'];progress('publishing')(done,total,rel,index+1,len(inventory))
        except Exception:
            for dest,keep in reversed(published):
                if dest.is_file():dest.unlink()
                if keep.exists():os.replace(keep,dest)
            raise
        return {}
    raise ValueError('未知文件处理操作')


def watch_parent(spec):
    """Do not leave a file publisher running after its controller exits."""
    parent=spec['parent_pid'];stop=Path(spec['scratch'])/'stop'
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,parent)
        if not handle:return
        try:
            if kernel.WaitForSingleObject(handle,0xFFFFFFFF)==0:stop.touch()
        finally:kernel.CloseHandle(handle)
    else:
        while os.getppid()==parent:time.sleep(.1)
        stop.touch()


if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if request.get('parent_pid'):
        threading.Thread(target=watch_parent,args=(request,),daemon=True).start()
    try:
        result=work(request)
        (Path(request['scratch'])/'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    except Exception as exc:
        print(json.dumps({'type':'error','message':str(exc)},ensure_ascii=False),flush=True)
        sys.exit(1)
