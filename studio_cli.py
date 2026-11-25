"""Local JSON command-line client for Compute Studio. Requires Python 3.9+."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import (
    HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)
from studio_launch import INSTALL_ROOT, launch_selected


ROOT = INSTALL_ROOT


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


def resolve_project(client, selector):
    if re.fullmatch('[a-f0-9]{32}', selector):
        return selector
    return project_id(client.json('/api/state')['projects'], selector)


def result_command(client,args):
    pid = resolve_project(client, args.project)
    base='/api/projects/'+part(pid)
    def get(suffix,**params):
        return client.json(base+suffix+'?'+urlencode(params,doseq=True))
    if args.command=='result-runs':
        return get('/results/runs',offset=args.offset,limit=args.limit)
    if args.command=='result-files':
        return get('/runs/'+part(args.attempt)+'/files',scope='outputs',query=args.query,offset=args.offset,limit=args.limit)
    if args.command=='result-evidence':
        selector={'log':1} if args.log else {'path':args.path}
        return get('/results/runs/'+part(args.task)+'/'+part(args.attempt)+'/evidence',offset=args.offset,limit=args.limit,**selector)
    if args.command=='result-tables':
        if args.dataset is not None:return get('/results/tables',dataset=args.dataset)
        scopes=get('/results/datasets')
        by_dataset={d:get('/results/tables',dataset=d)['tables'] for d in ['',*scopes['saved_datasets']]}
        return {'project_id':pid,**scopes,'tables_by_dataset':by_dataset}
    if args.command=='result-show':
        doc=get('/results',dataset=args.dataset,table=args.table)
        if args.row_id:
            wanted=set(args.row_id)
            doc['rows']=[r for r in doc['rows'] if r['id'] in wanted]
            if len(doc['rows'])!=len(wanted):raise StudioError('未找到请求的全部结果行')
            doc.pop('summary',None)
        return doc
    if args.command=='result-chart':
        return get('/results/chart-data',dataset=args.dataset,table=args.table,metric=args.metric,group=args.group,
                   mode=args.mode,offset=args.offset,limit=args.limit)
    raw=sys.stdin.buffer.read() if args.file=='-' else Path(args.file).read_bytes()
    return client.json(base+'/results/merge?'+urlencode({'dataset':args.dataset}),json.loads(raw.decode('utf-8-sig')))


def run(client, args):
    cmd = args.command
    if cmd in ('source-status', 'source-init', 'source-sync'):
        path = '/api/projects/' + part(resolve_project(client, args.project)) + '/source'
        if cmd == 'source-status':
            return client.json(path)
        if cmd == 'source-init':
            # An older controller would silently ignore a path on /initialize.
            return client.json(path + '/initialize-at', {'path': str(args.path.expanduser().absolute())})
        job = client.json(path + '/sync', {})
        return wait_for(client, job['id'], args.wait)
    if cmd.startswith('result-'):
        return result_command(client,args)
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
    for name, help_text in [('source-status', '查询当前源码目录绑定'),
                            ('source-init', '仅为空白且尚未绑定的项目绑定指定工作目录中的源码'),
                            ('source-sync', '立即同步当前绑定的源码目录')]:
        action = commands.add_parser(name, help=help_text)
        action.add_argument('--project', required=True, help='完整项目 ID 或唯一名称')
        if name == 'source-init':
            action.add_argument('--path', type=Path, required=True, help='指定源码目录，可相对于调用 CLI 的当前目录；缺失时创建')
        if name == 'source-sync':
            action.add_argument('--wait', type=int, default=300, help='等待同步的秒数')
    runs=commands.add_parser('result-runs',help='列出项目可整理的成功运行（含历史）')
    files=commands.add_parser('result-files',help='列出指定运行的本机回传文件')
    evidence=commands.add_parser('result-evidence',help='分段读取本机回传的指标文件或日志')
    catalog=commands.add_parser('result-tables',help='查找已有数据集与结果表')
    show=commands.add_parser('result-show',help='读取结果表和分组汇总，或核对指定行')
    merge=commands.add_parser('result-merge',help='按运行和观测键合并实测结果，可重复执行')
    chart=commands.add_parser('result-chart',help='读取筛选后的绘图数据、样本数和最佳均值')
    for action in (runs,files,evidence,catalog,show,merge,chart):
        action.add_argument('--project',required=True,help='完整项目 ID 或唯一名称')
    for action in (runs,files,evidence,chart):
        action.add_argument('--offset',type=int,default=0)
        action.add_argument('--limit',type=int,default=32768 if action is evidence else 100)
    for action in (files,evidence):
        action.add_argument('--attempt',required=True,help='运行 ID')
    files.add_argument('--query',default='',help='按产物相对路径筛选')
    evidence.add_argument('--task',required=True,help='实验 ID')
    evidence_source=evidence.add_mutually_exclusive_group(required=True)
    evidence_source.add_argument('--path',help='回传产物的相对路径')
    evidence_source.add_argument('--log',action='store_true',help='读取已回传的本机日志')
    catalog.add_argument('--dataset',help='只列出指定数据集的表')
    show.add_argument('--dataset',default='')
    show.add_argument('--table',default='',help='结果表 ID，省略时读取默认表')
    show.add_argument('--row-id',action='append',help='只核对所列行，可多次传入')
    chart.add_argument('--dataset',default='')
    chart.add_argument('--table',default='')
    chart.add_argument('--mode',choices=('bar','line','point','box'),default='bar')
    chart.add_argument('--metric',action='append',default=[],help='数值列 ID，可重复')
    chart.add_argument('--group',action='append',default=[],help='汇总中的组 key，可重复')
    merge.add_argument('--dataset',default='',help='结果所属数据集')
    merge.add_argument('file',help='UTF-8 JSON 观测文件，使用 - 从标准输入读取')
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
    selected=launch_selected('studio_cli.py')
    if selected is not None:
        return selected
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
