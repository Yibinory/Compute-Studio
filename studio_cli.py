"""Local JSON command-line client for Compute Studio. Requires Python 3.9+."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (
    HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)


ROOT = Path(__file__).resolve().parent


class StudioError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PageToken(HTMLParser):
    def __init__(self):
        super().__init__()
        self.value = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'csrf-token':
            self.value = attrs.get('content')


def token_file(args):
    directory = args.data_dir or os.environ.get('COMPUTE_DATA_DIR')
    if not directory:
        config = ROOT / ('storage-location-demo.json' if args.demo else 'storage-location.json')
        directory = (json.loads(config.read_text(encoding='utf-8'))['root'] if config.exists()
                     else ROOT / ('data-demo' if args.demo else 'data'))
    return Path(directory).expanduser() / 'access-token.txt'


class Studio:
    def __init__(self, port):
        self.url = f'http://127.0.0.1:{port}'
        self.opener = build_opener(ProxyHandler({}), NoRedirect(),
                                   HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None

    def request(self, path, payload=None):
        headers = {}
        data = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            if self.csrf:
                headers['X-CSRF-Token'] = self.csrf
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as response:
                return response.read().decode('utf-8')
        except HTTPError as exc:
            try:
                error = json.loads(exc.read()).get('error', exc.reason)
            except (ValueError, AttributeError):
                error = exc.reason
            finally:
                exc.close()
            raise StudioError(f'HTTP {exc.code} · {error}') from exc
        except (URLError, OSError) as exc:
            raise StudioError(f'与 {self.url} 通信失败，请检查控制器与端口。'
                              f'若刚提交写入，请先查看网页操作记录再重试。{exc}') from exc

    def json(self, path, payload=None):
        return json.loads(self.request(path, payload))

    def login(self, file):
        self.json('/login', {'token': file.read_text(encoding='utf-8').strip()})
        page = PageToken()
        page.feed(self.request('/'))
        if not page.value:
            raise StudioError('控制器未返回会话校验信息，请检查控制器版本。')
        self.csrf = page.value


def part(value):
    if not value or value in ('.', '..') or '/' in value or '\\' in value:
        raise StudioError('无效的记录 ID')
    return quote(value, safe='')


def select_fields(record, *fields):
    return {key: record[key] for key in fields if key in record}


def project_id(projects, selector):
    for project in projects:
        if project['id'] == selector:
            return project['id']
    matches = [p for p in projects if p['name'] == selector]
    if len(matches) == 1:
        return matches[0]['id']
    raise StudioError('找不到指定项目' if not matches else '有多个同名项目，请使用项目 ID')


def plan_summary(plan):
    return {
        'batch_id': plan['id'],
        **select_fields(plan, 'project_id', 'configuration_revision', 'status', 'count', 'input_bytes'),
        'tasks': [select_fields(t, 'id', 'key', 'name', 'status', 'command', 'depends_on',
                                'input_count', 'level', 'memory_mb', 'server_ids', 'datasets')
                  for t in plan['tasks']],
    }


def output(value, stream=None):
    print(json.dumps(value, ensure_ascii=False), file=stream or sys.stdout, flush=True)


def job_result(client, job_id, wait):
    deadline = time.monotonic() + wait
    while True:
        job = client.json('/api/jobs/' + part(job_id))['job']
        if job['status'] not in ('queued', 'running'):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return select_fields(job, 'id', 'status', 'label', 'progress')
        time.sleep(min(0.5, remaining))
    if job['status'] != 'success':
        raise StudioError(f"后台作业 {job_id} {job['status']} · {job.get('error') or job.get('label')}")
    result = job.get('result')
    if isinstance(result, dict) and 'configuration_revision' in result and 'tasks' in result:
        result = plan_summary(result)
    return {'id': job_id, 'status': job['status'], 'result': result}


def wait_for(client, job_id, wait, **receipt):
    receipt = {'job_id': job_id, **receipt}
    output(receipt, sys.stderr)
    job = job_result(client, job_id, wait)
    if job['status'] != 'success':
        raise StudioError(f'等待时间已到，后台继续处理。用 job {job_id} 查询进度和结果。')
    return job['result']


def commit(client, batch_id, revision, wait):
    output({'batch_id': batch_id, 'configuration_revision': revision}, sys.stderr)
    result = client.json('/api/batch-imports/' + part(batch_id) + '/commit',
                         {'configuration_revision': revision})
    if 'tasks' in result:  # Repeating a completed commit returns its original plan.
        return plan_summary(result)
    return wait_for(client, result['id'], wait, batch_id=batch_id)


def run(client, args):
    cmd = args.command
    if cmd == 'job':
        return job_result(client, args.id, args.wait)
    if cmd == 'commit':
        return commit(client, args.id, args.revision, args.wait)
    if cmd == 'create-project':
        return client.json('/api/projects', {'name': args.name, 'description': args.description})
    if cmd in ('queue', 'log'):
        path = '/api/tasks/' + part(args.id)
        if cmd == 'log':
            return client.json(path + '/log')
        return select_fields(client.json(path + '/action', {'action': 'queue'}),
                             'id', 'project_id', 'name', 'status')

    if cmd == 'import-dataset':
        upload = {'kind': 'dataset', 'name': args.name}
    else:
        state = client.json('/api/state')
        if cmd == 'projects':
            return [select_fields(p, 'id', 'name', 'description', 'archived') for p in state['projects']]
        pid = project_id(state['projects'], args.project) if args.project else None
        if cmd == 'tasks':
            return [select_fields(t, 'id', 'project_id', 'seq', 'name', 'status', 'archived', 'reason')
                    for t in state['tasks'] if not pid or t['project_id'] == pid]
        if cmd == 'import-experiments':
            preview = client.json('/api/projects/' + part(pid) + '/batch-import/preview',
                                  {'source_path': str(args.source.expanduser().absolute())})
            plan = wait_for(client, preview['id'], args.wait, phase='preview')
            if args.preview_only:
                return plan
            return commit(client, plan['batch_id'], plan['configuration_revision'], args.wait)
        upload = {'kind': 'project', 'project_id': pid, 'task_id': args.task}
    upload['source_path'] = str(args.source.expanduser().absolute())
    receipt = client.json('/api/uploads/local', upload)
    return wait_for(client, receipt['job_id'], args.wait, upload_id=receipt['id'])


def parser():
    cli = argparse.ArgumentParser(description='算力工坊本机操作接口 · 输出 JSON')
    cli.add_argument('--demo', action='store_true', help='连接演示工作空间（默认端口 8777）')
    cli.add_argument('--port', type=int, help='控制器本机端口（正式工作空间默认 8776）')
    cli.add_argument('--data-dir', type=Path, help='控制器本次使用的保存目录')
    commands = cli.add_subparsers(dest='command', required=True)
    commands.add_parser('projects', help='列出项目、ID 与归档状态')
    tasks = commands.add_parser('tasks', help='查看实验状态')
    tasks.add_argument('--project', help='按项目名称或 ID 筛选')
    create = commands.add_parser('create-project', help='创建项目')
    create.add_argument('name')
    create.add_argument('--description', default='')
    batch = commands.add_parser('import-experiments', help='从本机目录读取配置并创建实验草稿')
    batch.add_argument('--preview-only', action='store_true', help='只生成预览，返回批次与修订号')
    code = commands.add_parser('import-code', help='导入项目公共代码或实验专属文件')
    code.add_argument('--task', help='目标实验 ID（省略表示项目公共代码）')
    dataset = commands.add_parser('import-dataset', help='按网页规则导入并同步数据集')
    dataset.add_argument('--name', required=True)
    for action in (batch, code):
        action.add_argument('--project', required=True, help='目标项目的名称或 ID')
    for action in (batch, code, dataset):
        action.add_argument('source', type=Path, help='本机来源目录（代码和数据集也支持 ZIP）')
    confirm = commands.add_parser('commit', help='按已有预览创建草稿，重复提交同一批次可恢复结果')
    confirm.add_argument('id', help='批次 ID')
    confirm.add_argument('--revision', required=True, type=int)
    job = commands.add_parser('job', help='查询后台作业与结果')
    job.add_argument('id', help='作业 ID')
    for action in (batch, code, dataset, confirm, job):
        action.add_argument('--wait', type=int, default=0 if action is job else 300,
                            help='每阶段等待秒数（导入默认 300，查询作业默认 0）')
    for cmd, help_text in [('queue', '将指定实验入队'), ('log', '读取指定实验的近期日志')]:
        commands.add_parser(cmd, help=help_text).add_argument('id', help='实验 ID')
    return cli


def main():
    cli = parser()
    args = cli.parse_args()
    port = args.port if args.port is not None else (8777 if args.demo else 8776)
    if not 1 <= port <= 65535 or getattr(args, 'wait', 0) < 0:
        cli.error('端口范围为 1–65535，等待秒数须大于或等于 0')
    try:
        client = Studio(port)
        client.login(token_file(args))
        output(run(client, args))
    except (StudioError, OSError, ValueError, KeyError) as exc:
        output({'error': str(exc)}, sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
