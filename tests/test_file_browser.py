import hashlib
import json

from scheduler.file_browser import directory_page
from scheduler.upload_files import UploadFiles
from test_api import client, login


def test_manifest_paginates_directories_and_files_together():
    rows = [('a/b/x.txt', {}), ('a/b/y.txt', {}), ('a/c.txt', {}), ('a2/other.txt', {})]
    first = directory_page(rows, 'a', '', 0, 1)
    second = directory_page(rows, 'a', '', 1, 1)
    assert first['folders'] == ['a/b'] and first['files'] == []
    assert second['folders'] == [] and second['files'] == [{'path': 'a/c.txt'}]
    assert first['entry_count'] == second['entry_count'] == 2
    found = directory_page(rows, 'a', 'TXT', 0, 100)
    assert found['folders'] == []
    assert [f['path'] for f in found['files']] == ['a/b/x.txt', 'a/b/y.txt', 'a/c.txt']


def test_project_and_dataset_browsing_is_bounded_and_keeps_flat_api(client, monkeypatch):
    c, app = client
    login(c, app)
    engine = app.extensions['engine']
    project = engine.create_project({'name': 'Directory-browsing'})
    code = engine.local_project(project['id']) / 'code'
    (code / 'src' / 'nested').mkdir(parents=True)
    (code / 'src' / 'nested' / 'train.py').write_text('print(1)')
    (code / 'README.md').write_text('readme')
    url = f"/api/projects/{project['id']}/file-index"
    assert c.get(url).get_json()['total_count'] == 2
    assert c.get(url, query_string={'directory': '../'}).status_code == 400
    assert c.get(url, query_string={'directory': 'src', 'query': 'TRAIN'}).get_json()['files'][0]['path'] == 'src/nested/train.py'

    def reject_tree_scan(*args, **kwargs):
        raise AssertionError('Directory navigation must not scan the full file tree')

    monkeypatch.setattr('app.file_metadata', reject_tree_scan)
    first = c.get(url, query_string={'directory': '', 'limit': 1}).get_json()
    assert first['folders'] == ['src'] and first['files'] == [] and first['entry_count'] == 2
    nested = c.get(url, query_string={'directory': 'src/nested'}).get_json()
    assert [f['path'] for f in nested['files']] == ['src/nested/train.py']
    paths = {f'train/images/{i:03}.png': {'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}
             for i in range(305)}
    engine.store.put('datasets', {'id': 'browse-dataset', 'name': 'Browser fixture', 'manifest': paths})
    api = '/api/datasets/browse-dataset/files'
    assert c.get(api, query_string={'directory': ''}).get_json()['folders'] == ['train']
    page = c.get(api, query_string={'directory': 'train/images', 'offset': 100}).get_json()
    assert len(page['files']) == 100 and page['entry_count'] == 305
    assert page['files'][0]['path'] == 'train/images/100.png'
    assert c.get(api).get_json()['matched_count'] == 305


def test_sql_browsing_groups_receipts_and_transfer_locations(engine, setup_project):
    _, servers = setup_project
    inventory = {path: {'size': 3, 'sha256': 'a'*64} for path in
                 ('测试/deep/a.txt', '测试/b.txt', '测试2/c.txt', 'root.txt')}
    uploads = UploadFiles(engine.store)
    uploads.replace('receipts', [{'path': path, **value} for path, value in inventory.items()])
    root = uploads.browse('receipts', 0, 2, '', '')
    assert root['folders'] == ['测试', '测试2'] and root['entry_count'] == 3
    assert uploads.browse('receipts', 2, 2, '', '')['files'][0]['path'] == 'root.txt'
    receipt = uploads.browse('receipts', 0, 100, 'TXT', '测试')
    assert [f['path'] for f in receipt['files']] == ['测试/b.txt', '测试/deep/a.txt']

    job = {'id': 'browse-journal'}
    engine.transfers.prepare(job, servers, inventory)
    root = engine.transfers.page(job['id'], directory='', limit=1)
    assert root['folders'] == ['测试'] and root['files'] == []
    page = engine.transfers.page(job['id'], directory='测试')
    assert page['folders'] == ['测试/deep']
    assert len(page['files']) == len(servers)
    assert {f['path'] for f in page['files']} == {'测试/b.txt'}
    filtered = engine.transfers.page(job['id'], directory='测试', query='TXT', server=servers[0]['id'])
    assert filtered['folders'] == [] and filtered['entry_count'] == 2


def test_source_changes_and_input_plan_keep_metadata_on_each_page(client):
    c, app = client
    login(c, app)
    engine = app.extensions['engine']
    project = engine.create_project({'name': 'Source-browser'})
    task = engine.create_task(project['id'], {'name': 'Check-plan', 'command': 'echo check'})
    entry = {'sha256': 'f'*64, 'size': 12}
    revision = 'a'*32
    changes = engine.code_sources.folder(project['id']) / 'changes'
    changes.mkdir(parents=True, exist_ok=True)
    (changes / (revision+'.json')).write_text(json.dumps({
        'source': 'fixture', 'at': 25,
        'changes': [{'path': 'src/train.py', 'action': 'modified', 'before': entry, 'after': entry}],
    }))
    url = f"/api/projects/{project['id']}/source/history/{revision}"
    assert c.get(url, query_string={'directory': ''}).get_json()['folders'] == ['src']
    page = c.get(url, query_string={'directory': 'src'}).get_json()
    assert page['total'] == 1 and page['files'][0]['before'] == entry

    engine.store.put('input_plans', {'id': task['id'], 'unique_objects': 1, 'files': {'src/train.py': entry}})
    url = f"/api/tasks/{task['id']}/input-plan"
    assert c.get(url).get_json()['files'] == {'src/train.py': entry}
    page = c.get(url, query_string={'directory': 'src'}).get_json()
    assert page['unique_objects'] == 1 and page['files'] == [{'path': 'src/train.py', **entry}]
