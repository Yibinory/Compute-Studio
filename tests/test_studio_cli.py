import json
import subprocess
import sys
import threading

import pytest
from werkzeug.serving import make_server

from studio_cli import ROOT, project_id, StudioError
from test_api import client, login


@pytest.fixture
def cli(client):
    _, app = client
    root = app.extensions['engine'].root
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def invoke(*args, ok=True, cwd=None):
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'studio_cli.py'), '--port', str(server.server_port),
             '--data-dir', str(root), *map(str, args)],
            capture_output=True, text=True, encoding='utf-8', timeout=40, cwd=cwd)
        assert (completed.returncode == 0) == ok, completed.stderr
        assert (root / 'access-token.txt').read_text() not in completed.stdout + completed.stderr
        return json.loads(completed.stdout if ok else completed.stderr.splitlines()[-1])

    try:
        yield invoke
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_source_cli_checks_binding_and_publishes_code(cli, client, ai_workspace):
    _, app = client
    engine = app.extensions['engine']
    engine.code_sources.settle_seconds = 0
    pid = engine.create_project({'name': 'SourceDemo'})['id']
    assert cli('source-status', '--project', pid)['path'] == ''
    editor = ai_workspace / 'my code'
    editor.mkdir()
    (editor / 'train.py').write_text('version = 1')
    reply = cli('source-init', '--project', 'SourceDemo', '--path', 'my code', cwd=ai_workspace)
    assert reply['outcome'] == 'created'
    assert reply['source']['path'] == str(editor)
    reused = cli('source-init', '--project', pid, '--path', 'other', cwd=ai_workspace)
    assert reused['outcome'] == 'existing' and reused['source']['path'] == str(editor)
    assert not (ai_workspace / 'other').exists()
    synced = cli('source-sync', '--project', pid)
    assert synced['revision'] == cli('source-status', '--project', pid)['revision']
    assert (engine.local_project(pid) / 'code/train.py').read_text() == (editor / 'train.py').read_text()


def test_agent_imports_visible_in_existing_browser_session(cli, client):
    browser, app = client
    headers = login(browser, app)
    assert cli('projects') == []
    project = cli('create-project', '研究项目')
    plan = cli('import-experiments', '--project', project['name'], ROOT / 'examples/batch-experiments')
    assert plan['status'] == 'success' and plan['count'] == 2
    assert cli('commit', plan['batch_id'], '--revision', plan['configuration_revision']) == plan
    tasks = browser.get('/api/state').json['tasks']
    assert {t['id'] for t in tasks} == {t['id'] for t in plan['tasks']}
    assert all(t['status'] == 'draft' for t in tasks)
    assert len(cli('tasks', '--project', project['id'])) == 2
    train = next(t for t in plan['tasks'] if t['key'] == 'train')
    assert next(t for t in plan['tasks'] if t['key'] == 'evaluate')['depends_on'] == [train['id']]
    assert cli('queue', train['id'])['status'] == 'queued'
    assert 'text' in cli('log', train['id'])
    assert browser.post('/api/projects', json={'name': '网页项目'}, headers=headers).status_code == 200
    assert '网页项目' in {p['name'] for p in cli('projects')}


def test_agent_file_and_dataset_imports_and_preview_recovery(cli, client, tmp_path):
    _, app = client
    e = app.extensions['engine']
    p = e.create_project({'name': '资料'})
    source = ROOT / 'examples/batch-experiments'
    preview = cli('import-experiments', '--project', p['id'], source, '--preview-only')
    assert preview['status'] == 'ready' and not e.store.all('tasks')
    assert 'error' in cli('commit', preview['batch_id'], '--revision', 7, ok=False)
    plan = cli('commit', preview['batch_id'], '--revision', preview['configuration_revision'])
    task = plan['tasks'][0]['id']
    result = cli('import-code', '--project', p['name'], source)
    assert result['project_id'] == p['id']
    assert (e.local_project(p['id']) / 'code' / 'train.sh').is_file()
    files = tmp_path.with_name(tmp_path.name + '-inputs')
    files.mkdir()
    (files / '样本.txt').write_text('样本', encoding='utf-8')
    cli('import-code', '--project', p['id'], '--task', task, files)
    assert (e.local_project(p['id']) / 'tasks' / task / 'files' / '样本.txt').is_file()
    result = cli('import-dataset', '--name', '样本集', files)
    upload = e.store.get('uploads', result['upload_id'])
    assert cli('job', upload['job_id'])['result']['dataset_id'] == result['dataset_id']
    assert '样本.txt' in e.store.get('datasets', result['dataset_id'])['manifest']
    assert 'error' in cli('import-dataset', '--name', '样本集', files, ok=False)


def test_ambiguous_project_requires_id():
    projects = [{'id': 'a', 'name': 'same'}, {'id': 'b', 'name': 'same'}]
    with pytest.raises(StudioError, match='同名'):
        project_id(projects, 'same')
    assert project_id(projects, 'a') == 'a'
