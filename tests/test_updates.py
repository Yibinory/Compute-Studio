import hashlib
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from zipfile import ZipFile

import pytest

from scheduler import updates
from studio_launch import UPDATE_DIR, program_version, update_state
from test_api import client, login


def release_zip(tmp_path):
    program=tmp_path/'program'
    program.mkdir()
    (program/'VERSION').write_text('1.0.0\n')
    (program/'requirements.txt').write_text('Flask\n')
    data=io.BytesIO()
    with ZipFile(data,'w') as archive:
        for name in sorted(updates.REQUIRED):
            contents={'VERSION':'1.1.0\n','requirements.txt':'Flask\n'}.get(name,'')
            archive.writestr(name,contents)
    return program,data.getvalue()


def test_release_metadata_requires_an_official_asset(monkeypatch):
    tag='v1.1.0'
    asset={'name':'compute-studio-v1.1.0.zip','size':10,'digest':'sha256:'+'a'*64,
           'browser_download_url':updates.RELEASES+'/download/'+tag+'/compute-studio-v1.1.0.zip'}
    report={'tag_name':tag,'assets':[asset],'body':'Release notes'}
    monkeypatch.setattr(updates,'fetch',lambda url,limit:json.dumps(report).encode())
    assert updates.official_release()['asset']['sha256']=='a'*64
    asset['browser_download_url']='https://example.com/compute-studio-v1.1.0.zip'
    assert updates.official_release()['asset'] is None
    report['prerelease']=True
    assert updates.official_release() is None


def test_check_distinguishes_no_release_from_network_errors(monkeypatch):
    def unavailable(code):
        def fetch(url,limit):
            raise HTTPError(url,code,'unavailable',None,None)
        return fetch
    monkeypatch.setattr(updates,'fetch',unavailable(404))
    assert updates.official_release() is None
    monkeypatch.setattr(updates,'fetch',unavailable(403))
    with pytest.raises(ValueError,match='限制'):
        updates.official_release()


def test_update_dialog_endpoints_require_login_and_csrf(client,monkeypatch):
    c,app=client
    monkeypatch.setattr(updates,'official_release',lambda:None)
    assert c.get('/api/update/check').status_code==401
    headers=login(c,app)
    assert b'data-action="update-open"' in c.get('/').data
    value=c.get('/api/update/check').get_json()
    assert value['current']==program_version() and value['latest'] is None
    assert not value['available']
    assert c.post('/api/update/stage',json={'version':'1.1.0'}).status_code==403
    assert c.post('/api/update/stage',json={'version':'1.1.0'},headers=headers).status_code==400


def test_staging_requires_hash_and_preflight_before_marking_pending(tmp_path,monkeypatch):
    install,payload=release_zip(tmp_path)
    service=updates.Updates(True,home=install,source=install)
    asset={'url':'official test fixture','size':len(payload),'sha256':'0'*64}
    monkeypatch.setattr(updates,'official_release',lambda:{'version':'1.1.0','asset':asset})
    monkeypatch.setattr(updates,'fetch',lambda url,limit:payload)
    with pytest.raises(ValueError,match='SHA-256'):
        service.stage('1.1.0')
    assert update_state(install)=={}

    asset['sha256']=hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updates.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1))
    with pytest.raises(ValueError,match='启动检查'):
        service.stage('1.1.0')
    assert update_state(install)=={}

    monkeypatch.setattr(updates.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    assert service.stage('1.1.0')['pending']=='1.1.0'
    assert program_version(install)=='1.0.0'
    assert program_version(install/UPDATE_DIR/'versions'/'1.1.0')=='1.1.0'
    assert service.stage('1.1.0')['pending']=='1.1.0'


@pytest.mark.parametrize('name',['../escape','static/../../escape','static/access-token.txt','storage-location.json'])
def test_update_zip_rejects_non_program_paths(tmp_path,name):
    data=io.BytesIO()
    with ZipFile(data,'w') as archive:
        archive.writestr(name,'invalid file')
    target=tmp_path/'target'
    target.mkdir()
    with pytest.raises(ValueError):
        updates.extract_release(data.getvalue(),target)
    assert not list(target.rglob('*'))
