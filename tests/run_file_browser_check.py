"""Run the file browser regression suite against a temporary local controller.

Run from the repository root with Playwright available to Node and Edge installed.
"""
import argparse
import hashlib
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app


def run(screenshot_path):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)/'studio'
        app=create_app(root)
        engine=app.extensions['engine']
        project=engine.create_project({'name':'Folder-fixture'})
        pid=project['id']
        now=time.time()
        task=engine.create_task(pid,{'name':'Main-task','command':'echo hello'})
        settings=engine.settings
        settings.update(demo=True,paused=True)
        engine.store.put('settings',settings)
        for index in range(10):
            name='Long_experiment_name_for_layout_regression_'*2 if index==0 else 'Task-'+str(index)
            extra=engine.create_task(pid,{'name':name,'command':'echo fixture'})
            if index<2:
                extra.update(datasets=['Image folders'],status='failed' if index==0 else 'draft')
                engine.store.put('tasks',extra)
        attempt='a'*32
        result=engine.local_project(pid)/'runs'/attempt/'result'/'outputs'
        result.mkdir(parents=True)
        score=b'42'
        (result/'score.txt').write_bytes(score)
        task.update(status='success',datasets=['Image folders'],synced_at=now,attempt_id=attempt,result_manifest={'outputs/score.txt':{'size':2,'sha256':hashlib.sha256(score).hexdigest()}})
        engine.store.put('tasks',task)
        code=engine.local_project(pid)/'code'
        (code/'src').mkdir()
        (code/'src'/'train.py').write_text('print(42)')
        server=engine.save_server({'name':'Local fixture','host':'fixture','username':'test','work_root':'/srv/experiments','data_root':'/mnt/datasets','enabled':False})
        sid=server['id']
        engine.store.put('replicas',{'id':pid+':'+sid,'project_id':pid,'server_id':sid,'exists':True,'checked_at':now,
            'files':{'src/train.py':{'sha256':'b'*64,'size':9}},'differences':[{'path':'src/train.py','state':'modified'}]})
        engine.report_content_storage(pid)
        files={f'train/images/image_{index:04}.txt':{'size':16,'sha256':'a'*64} for index in range(305)}
        files.update({f'{folder}/example.txt':{'size':3,'sha256':'a'*64}
                      for folder in ('annotations','reports','validation','样本说明')})
        files['root.txt']={'size':3,'sha256':'a'*64}
        target=root/'datasets'/'folders-fixture'/'files'/'train'/'images'
        target.mkdir(parents=True)
        (target/'image_0209.txt').write_text('verified content')
        engine.store.put('datasets',{'id':'folders-fixture','name':'Image folders','created_at':now,'servers':{},'manifest':files})
        upload={'id':'folders-upload','label':'Upload fixture','status':'success','created_at':now,'kind':'dataset','file_count':len(files),'job_id':'folder-job'}
        engine.store.put('uploads',upload)
        engine.uploads.files.replace(upload['id'],[{'path':path,**item} for path,item in files.items()])
        job={'id':'folder-job','status':'success','operation':'process:["folders-upload"]','created_at':now,'label':'Fixture'}
        engine.store.put('jobs',job)
        engine.transfers.prepare(job,[server],files)
        server_instance=make_server('127.0.0.1',0,app,threaded=True)
        thread=threading.Thread(target=server_instance.serve_forever)
        thread.start()
        try:
            code=subprocess.call(['node',str(ROOT/'tests'/'verify_file_browser.js'),f'http://127.0.0.1:{server_instance.server_port}/',str(root),pid,task['id'],sid,attempt])
            screenshot=root/'dataset-folders.png'
            if screenshot_path and screenshot.exists():
                screenshot_path.write_bytes(screenshot.read_bytes())
        finally:
            server_instance.shutdown()
            thread.join()
            server_instance.server_close()
            engine.close()
        return code

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path)
    raise SystemExit(run(parser.parse_args().screenshot))

