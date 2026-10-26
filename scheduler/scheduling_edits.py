"""Atomic policy changes without modifying execution inputs or queue state."""
import math
import time

from .storage import uid
from .environments import environment

EDITABLE = {'draft', 'queued', 'waiting', 'blocked', 'paused'}
FIELDS = {'priority', 'level', 'memory_mb', 'max_utilization', 'gpu_name', 'server_ids', 'max_retries','conda_env','server_conda_envs','code_files','code_exclude','output_files','dependency_patterns','dependency_files','object_files','follow_project_files'}
NUMBERS = {'priority': (1, 999, True), 'memory_mb': (0, 1000000, True),
           'max_utilization': (0, 100, False), 'max_retries': (0, 20, True)}


def update_policies(engine, task_ids, changes):
    if not isinstance(task_ids, list) or not task_ids or any(not isinstance(v, str) or not v for v in task_ids):
        raise ValueError('请选择要修改的实验')
    task_ids = list(dict.fromkeys(task_ids))
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
        raise ValueError('请仅提交需要修改的调度字段，至少选择一项')
    # The engine lock excludes dispatch; the store lock also excludes queue
    # controls that intentionally do not acquire the engine lock.
    with engine.store.lock:
        tasks = []
        rejected = []
        for key in task_ids:
            try:
                task = engine.store.get('tasks', key)
            except ValueError:
                rejected.append(key + '（不存在）')
                continue
            if task.get('archived') or task['status'] not in EDITABLE and not (task['status'] in ('failed','canceled') and set(changes)<= {'conda_env','server_conda_envs','code_files','code_exclude','output_files','dependency_patterns','dependency_files','object_files','follow_project_files'}):
                rejected.append(task['name'] + '（' + task['status'] + '）')
            tasks.append(task)
        if rejected:
            raise ValueError('本次未修改任何实验；以下实验已执行或不存在，请刷新后重新选择：' + '、'.join(rejected[:10]))
        policy = dict(changes)
        from .content import patterns,object_files
        if 'follow_project_files' in policy:policy['follow_project_files']=patterns(policy['follow_project_files'])
        if 'object_files' in policy:policy['object_files']=object_files(policy['object_files'])
        for field in ('code_files','code_exclude','output_files','dependency_patterns'):
            if field in policy and policy[field] is not None:policy[field]=patterns(policy[field])
        if 'dependency_files' in policy:
            if not isinstance(policy['dependency_files'],dict):raise ValueError('前序选择须为映射')
            policy['dependency_files']={k:patterns(v) for k,v in policy['dependency_files'].items()}
            if any(set(policy['dependency_files'])-set(t['depends_on']) for t in tasks):raise ValueError('前序选择包含非直接依赖')
        if 'conda_env' in policy:
            if not isinstance(policy['conda_env'],str):raise ValueError('Conda 环境必须为文本')
            policy['conda_env']=environment(policy['conda_env'])
        if 'server_conda_envs' in policy:
            if not isinstance(policy['server_conda_envs'],dict):raise ValueError('服务器环境必须为映射')
            policy['server_conda_envs']=dict(policy['server_conda_envs'])
            for sid,value in policy['server_conda_envs'].items():
                engine.store.get('servers',sid)
                if not isinstance(value,str):raise ValueError('Conda 环境必须为文本')
                policy['server_conda_envs'][sid]=environment(value)
        for field, (low, high, integer) in NUMBERS.items():
            if field not in policy:
                continue
            value = policy[field]
            try:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise ValueError()
                number = float(value)
                if not math.isfinite(number) or not low <= number <= high or integer and not number.is_integer():
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                raise ValueError(field + ' 必须为 ' + str(low) + ' 至 ' + str(high) + (' 的整数' if integer else ' 的数值')) from None
            policy[field] = int(number) if integer else number
        if 'level' in policy and policy['level'] not in ('idle_only', 'low_interference', 'emergency'):
            raise ValueError('未知调度级别')
        if 'gpu_name' in policy:
            if not isinstance(policy['gpu_name'], str):
                raise ValueError('GPU 型号必须为文本；留空表示不限制')
            policy['gpu_name'] = policy['gpu_name'].strip()
        if 'server_ids' in policy:
            servers = policy['server_ids']
            if not isinstance(servers, list) or any(not isinstance(key, str) or not key for key in servers):
                raise ValueError('允许的服务器必须是列表；空列表表示自动选择所有服务器')
            policy['server_ids'] = list(dict.fromkeys(servers))
            for key in policy['server_ids']:
                try:
                    engine.store.get('servers', key)
                except ValueError:
                    raise ValueError('允许的服务器不存在，请刷新后重新选择：' + key) from None
        updated = [{**task, **policy} for task in tasks]
        for task in updated:
            if 'dependency_patterns' in policy:
                value=task.pop('dependency_patterns');task['dependency_files']={} if value is None else {d:value for d in task['depends_on']}
            task.pop('preparation_retry_at',None);task.pop('preparation_failures',None)
        changed = sum(any(task.get(k)!=updated[i].get(k) for k in set(policy)|{'dependency_files'}) for i,task in enumerate(tasks))
        audit = {'id': uid(), 'created_at': time.time(), 'task_ids': task_ids, 'changes': policy,
                 'before': {t['id']: {k: t.get(k) for k in policy} for t in tasks}, 'changed_count': changed}
        engine.store.put_many([('tasks', t) for t in updated] + [('scheduling_edits', audit)])
        engine.store.event('批量修改实验调度策略：' + str(len(tasks)) + ' 个实验（' + str(changed) + ' 个有变化），字段：' + '、'.join(policy))
        engine.wake_event.set()
        return {'count': len(tasks), 'changed_count': changed, 'task_ids': task_ids, 'changes': policy, 'edit_id': audit['id']}
