import io
import zipfile

import pytest

from app import create_app
from scheduler.files import extract_zip, manifest, relative
from conftest import FakeRemote


def archive(entries):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        for path,value in entries.items():z.writestr(path,value)
    raw=buffer.getvalue()
    # ZipInfo normalizes Windows separators when writing; construct the raw
    # attacker-controlled entry names that an incoming archive could contain.
    for path in entries:
        if '\\' in path:
            raw=raw.replace(path.replace('\\','/').encode(),path.encode())
    return io.BytesIO(raw)


@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path,remote_factory=FakeRemote)
    app.extensions['engine'].settings['content_store_enabled']=False
    app.extensions['engine'].store.put('settings',app.extensions['engine'].settings)
    app.config['TESTING']=True
    client=app.test_client()
    yield client,app
    app.extensions['engine'].close()


def login(client,app):
    token=(app.extensions['engine'].root/'access-token.txt').read_text()
    assert client.post('/login',json={'token':token}).status_code==200
    with client.session_transaction() as session:return {'X-CSRF-Token':session['csrf']}


def test_frontend_assets_change_version_without_controller_restart(client,tmp_path):
    import hashlib
    import re
    from pathlib import Path
    from urllib.parse import parse_qs, urlsplit
    c,app=client
    # Keep changes in the isolated test folder, never edit production assets.
    template=(Path(app.template_folder) if Path(app.template_folder).is_absolute() else Path(app.root_path)/app.template_folder)/'index.html'
    isolated=tmp_path/'ui-assets'
    for filename in re.findall(r"asset_url\('([^']+)'\)",template.read_text(encoding='utf-8')):
        target=isolated/filename
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b'first asset version')
    app.static_folder=str(isolated)

    def script_url(response):
        return re.search(r'src="([^"]*?/app.js\?v=[^"]+)"',response.data.decode()).group(1)

    first=c.get('/')
    assert first.headers['Cache-Control']=='no-store'
    first_url=script_url(first)
    assert script_url(c.get('/'))==first_url
    static=c.get(first_url)
    assert static.headers['Cache-Control']=='no-cache, must-revalidate'
    assert static.data==b'first asset version'
    (isolated/'app.js').write_bytes(b'updated asset version')
    second_url=script_url(c.get('/'))
    assert second_url!=first_url
    assert parse_qs(urlsplit(second_url).query)['v']==[hashlib.sha256(b'updated asset version').hexdigest()[:16]]
    assert c.get(second_url).data==b'updated asset version'
    login(c,app)
    assert 'title="刷新页面"' in c.get('/').data.decode()
    assert c.get('/api/state').headers['Cache-Control']=='no-store'


def test_experiment_guide_requires_login_and_downloads_complete_document(client):
    c,app=client
    assert c.get('/api/experiment-guide').status_code==401
    login(c,app);response=c.get('/api/experiment-guide')
    assert response.status_code==200 and 'attachment' in response.headers['Content-Disposition']
    text=response.data.decode('utf-8')
    assert 'SIGTERM' in text and 'max_retries' in text and 'DEPENDENCIES_JSON' in text


def test_authentication_and_csrf(client):
    c,app=client
    assert c.get('/api/state').status_code==401
    assert c.post('/login',json={'token':'wrong'}).status_code==403
    h=login(c,app)
    assert c.get('/api/state').status_code==200
    assert c.post('/api/projects',json={'name':'safe'}).status_code==403
    assert c.post('/api/projects',json={'name':'safe'},headers={**h,'Origin':'https://evil.example'}).status_code==403
    assert c.post('/api/projects',json={'name':'safe'},headers=h).status_code==200


def test_project_files_edit_upload_and_history(client):
    c,app=client;h=login(c,app)
    p=c.post('/api/projects',json={'name':'Project'},headers=h).get_json()
    url='/api/projects/'+p['id']
    assert c.post(url+'/file',json={'path':'src/model.py','text':'VERSION=1\n'},headers=h).status_code==200
    assert c.post(url+'/file',json={'path':'src/model.py','text':'VERSION=2\n'},headers=h).status_code==200
    e=app.extensions['engine']
    assert len(list((e.local_project(p['id'])/'history').glob('*/src/model.py')))==1
    assert c.get(url+'/file?path=src/model.py').get_json()['text']=='VERSION=2\n'
    r=c.post(url+'/files',data={'mode':'zip','files':(archive({'train.py':'print(1)'}),'code.zip')},headers=h)
    assert r.status_code==202
    import time
    for _ in range(100):
        job=e.store.get('jobs',r.get_json()['job']['id'])
        if job['status'] in ('success','failed'):break
        time.sleep(.02)
    assert job['status']=='success',job
    assert 'train.py' in c.get(url+'/files').get_json()['files']
    assert c.post(url+'/file',json={'path':'../escaped','text':'x'},headers=h).status_code==400


def test_task_draft_dependencies_and_input_lock(client):
    c,app=client;h=login(c,app)
    p=c.post('/api/projects',json={'name':'A'},headers=h).get_json()
    p2=c.post('/api/projects',json={'name':'B'},headers=h).get_json()
    t=c.post('/api/projects/'+p['id']+'/tasks',json={'name':'train','command':'python train.py'},headers=h).get_json()
    assert t['status']=='draft'
    assert c.post('/api/projects/'+p2['id']+'/tasks',json={'name':'bad','command':'echo bad','depends_on':[t['id']]},headers=h).status_code==400
    e=app.extensions['engine'];t['status']='running';e.store.put('tasks',t)
    assert c.post('/api/projects/'+p['id']+'/file',json={'task_id':t['id'],'path':'a','text':'x'},headers=h).status_code==400
    assert c.get('/api/projects/'+p['id']+'/files?task_id='+t['id']).status_code==200


@pytest.mark.parametrize('path',['../escape','/absolute','C:/evil','a\\b','a/../../evil','CON','folder/NUL.txt','a.'])
def test_zip_traversal_and_reserved_names(tmp_path,path):
    with pytest.raises(ValueError):extract_zip(archive({path:'x'}),tmp_path/'output')
    assert not (tmp_path/'escape').exists()


def test_zip_symlinks_duplicates_and_size(tmp_path):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        info=zipfile.ZipInfo('symlink');info.create_system=3;info.external_attr=(0o120777<<16);z.writestr(info,'../target')
    b.seek(0)
    with pytest.raises(ValueError,match='符号链接'):extract_zip(b,tmp_path/'output')
    with pytest.raises(ValueError,match='大小写冲突'):extract_zip(archive({'A.txt':'a','a.txt':'b'}),tmp_path/'output')
    with pytest.raises(ValueError,match='超过限制'):extract_zip(archive({'big':'1234'}),tmp_path/'output',limit=3)


def test_no_plaintext_credentials_in_database(client):
    c,app=client;h=login(c,app)
    secret='this-must-not-be-saved'
    r=c.post('/api/servers',json={'host':'fake','username':'tester','work_root':'/srv/work','data_root':'/srv/data','password':secret},headers=h)
    assert r.status_code==200
    assert secret not in str(r.get_json())
    assert secret not in str(c.get('/api/state').get_json())
    assert secret.encode() not in (app.extensions['engine'].root/'compute.sqlite3').read_bytes()


def test_numeric_validation_rejects_nan(client):
    c,app=client;h=login(c,app)
    assert c.post('/api/settings',json={'poll_seconds':'NaN'},headers=h).status_code==400
    assert c.post('/api/settings',json={'history_samples':0},headers=h).status_code==400


def test_dataset_web_import_and_background_distribution(client):
    import time
    c,app=client;h=login(c,app)
    server=c.post('/api/servers',json={'host':'fake','username':'tester','work_root':'/srv/work','data_root':'/srv/data','enabled':False},headers=h).get_json()
    response=c.post('/api/datasets',data={'name':'web-data','archive':(archive({'images/one.txt':'image bytes'}),'data.zip')},headers=h)
    assert response.status_code==202
    job_id=response.get_json()['job']['id']
    for _ in range(100):
        state=c.get('/api/state').get_json()
        job=next(j for j in state['jobs'] if j['id']==job_id)
        if job['status'] in ('success','failed'):break
        time.sleep(.02)
    assert job['status']=='success',job
    dataset=state['datasets'][0]
    assert dataset['file_count']==1 and dataset['servers'][server['id']]['status']=='success'
    assert 'manifest' not in dataset


def test_independent_workspaces_do_not_overwrite_login_cookie(client,tmp_path):
    c,app=client
    other=create_app(tmp_path/'another-workspace',remote_factory=FakeRemote)
    try:
        assert app.config['SESSION_COOKIE_NAME']!=other.config['SESSION_COOKIE_NAME']
    finally:other.extensions['engine'].close()
