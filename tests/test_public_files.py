import threading
import time

import pytest

from test_api import client, login
from scheduler.policy import ACTIVE


def fixture_files(client):
    c, app = client
    headers = login(c, app)
    e = app.extensions['engine']
    project = e.create_project({'name': 'Delete_inputs'})
    root = e.local_project(project['id'])
    (root / 'code' / 'src').mkdir()
    (root / 'code' / 'src' / 'train.py').write_text('original')
    (root / 'code' / 'run.sh').write_text('echo original')
    (root / 'runs' / 'result').mkdir(parents=True)
    (root / 'runs' / 'result' / 'output.txt').write_text('keep output')
    task = e.create_task(project['id'], {'name': 'Draft', 'command': 'echo hello'})
    (root / 'tasks' / task['id'] / 'files' / 'input.txt').write_text('keep task')
    return c, app, headers, project, root, task


def wait_job(e, job):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        current = e.store.get('jobs', job['id'])
        if current['status'] not in ('queued', 'running'):
            return current
        time.sleep(.02)
    raise AssertionError('File removal did not finish')


@pytest.mark.parametrize('all_files', [False, True])
def test_delete_inputs_background_and_scope(client, all_files):
    c, app, h, p, root, task = fixture_files(client)
    e = app.extensions['engine']
    response = c.post('/api/projects/' + p['id'] + '/files/delete', json={'all': all_files, 'paths': [] if all_files else ['src/train.py']}, headers=h)
    assert response.status_code == 202
    job = wait_job(e, response.get_json()['job'])
    assert job['status'] == 'success', job
    assert not (root / 'code' / 'src' / 'train.py').exists()
    assert (root / 'code').is_dir()
    assert (root / 'code' / 'run.sh').exists() == (not all_files)
    assert (root / 'runs' / 'result' / 'output.txt').read_text() == 'keep output'
    assert (root / 'tasks' / task['id'] / 'files' / 'input.txt').read_text() == 'keep task'
    assert not list((root / '.file-deletions').iterdir())
    assert job['progress']['phase'] == 'complete'
    assert e.store.get('projects', p['id'])['public_files_revision']


@pytest.mark.parametrize('status', sorted(ACTIVE))
def test_active_experiment_blocks_deletion(client, status):
    c, app, h, p, root, task = fixture_files(client)
    task['status'] = status
    app.extensions['engine'].store.put('tasks', task)
    response = c.post('/api/projects/' + p['id'] + '/files/delete', json={'all': True}, headers=h)
    assert response.status_code == 400
    assert (root / 'code' / 'run.sh').exists()


@pytest.mark.parametrize('data', [
    {}, {'paths': ['../runs/result/output.txt']}, {'paths': ['src']},
    {'paths': ['run.sh', 'missing.txt']}, {'paths': ['run.sh', 'RUN.sh']},
    {'all': True, 'paths': ['run.sh']}, {'all': 'true'}, {'paths': 'run.sh'},
    {'paths': ['run.sh'] * 1001}, {'task_id': 'another-task', 'all': True},
])
def test_invalid_request_never_partially_deletes(client, data):
    c, app, h, p, root, task = fixture_files(client)
    response = c.post('/api/projects/' + p['id'] + '/files/delete', json=data, headers=h)
    assert response.status_code == 400
    assert (root / 'code' / 'run.sh').exists()
    assert (root / 'code' / 'src' / 'train.py').exists()


def test_imports_and_queued_deletion_reserve_inputs(client, monkeypatch):
    c, app, h, p, root, task = fixture_files(client)
    e = app.extensions['engine']; uploads = app.extensions['uploads']
    import_job = uploads.begin({'kind': 'project', 'project_id': p['id'], 'files': [{'path': 'new.py', 'size': 1}]})
    url = '/api/projects/' + p['id'] + '/files/delete'
    assert c.post(url, json={'all': True}, headers=h).status_code == 400
    u = e.store.get('uploads', import_job['id']); u['status'] = 'canceled'; e.store.put('uploads', u)
    release = threading.Event(); blockers = [e.jobs.submit(release.wait, 10) for _ in range(2)]
    try:
        response = c.post(url, json={'paths': ['run.sh']}, headers=h)
        assert response.status_code == 202
        assert uploads.pending(p['id'], task['id'])
        assert c.post('/api/projects/' + p['id'] + '/file', json={'path': 'other.py', 'text': 'x'}, headers=h).status_code == 400
        assert c.post(url, json={'all': True}, headers=h).status_code == 400
        with pytest.raises(ValueError):
            uploads.begin({'kind': 'project', 'project_id': p['id'], 'files': [{'path': 'new.py', 'size': 1}]})
        # Recheck at execution time: another experiment may start after the
        # request was queued through a different control path.
        task['status'] = 'running'; e.store.put('tasks', task)
    finally:
        release.set()
    job = wait_job(e, response.get_json()['job'])
    assert job['status'] == 'failed'
    assert (root / 'code' / 'run.sh').exists()


def test_moves_roll_back_if_one_file_cannot_be_removed(client, monkeypatch):
    c, app, h, p, root, task = fixture_files(client)
    e = app.extensions['engine']
    import scheduler.public_files as module
    original = module.os.replace
    def deny_second(source, dest):
        if str(source).endswith('train.py'):
            raise PermissionError('file is open')
        return original(source, dest)
    monkeypatch.setattr(module.os, 'replace', deny_second)
    with pytest.raises(PermissionError):
        app.extensions['public_files'].clear_public_files(p['id'], ['run.sh', 'src/train.py'])
    assert (root / 'code' / 'run.sh').read_text() == 'echo original'
    assert (root / 'code' / 'src' / 'train.py').read_text() == 'original'


def test_large_tree_purges_outside_scheduler_lock(client, monkeypatch):
    c, app, h, p, root, task = fixture_files(client)
    e = app.extensions['engine']; entered = threading.Event(); release = threading.Event()
    import scheduler.public_files as module
    original = module.run
    def slow_purge(*args, **kwargs):
        entered.set(); assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'run', slow_purge)
    response = c.post('/api/projects/' + p['id'] + '/files/delete', json={'all': True}, headers=h)
    try:
        assert entered.wait(3)
        assert e.lock.acquire(timeout=.2)
        e.lock.release()
        assert c.get('/api/state').status_code == 200
        assert c.get('/api/projects/' + p['id'] + '/file-index').get_json()['total_count'] == 0
        assert app.extensions['uploads'].pending(p['id'])
    finally:
        release.set()
    assert wait_job(e, response.get_json()['job'])['status'] == 'success'


def test_delete_requires_csrf(client):
    c, app, h, p, root, task = fixture_files(client)
    assert c.post('/api/projects/' + p['id'] + '/files/delete', json={'all': True}).status_code == 403
