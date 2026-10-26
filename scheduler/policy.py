from __future__ import annotations

import time
import math

ACTIVE = {'staging', 'launching', 'running', 'suspended', 'canceling', 'syncing', 'sync_failed', 'lost'}


def dependencies_ready(task, tasks):
    index = {t['id']: t for t in tasks}
    for key in task.get('depends_on', []):
        dep = index.get(key)
        if not dep or dep['status'] != 'success' or not dep.get('synced_at'):
            return False
    return True


def dependency_reason(task, tasks):
    index={t['id']:t for t in tasks};pending=[]
    labels={'draft':'草稿未入队','queued':'待调度','waiting':'等待资源','blocked':'等待它的前序','paused':'暂停排队','staging':'准备输入','launching':'启动中','running':'运行中','suspended':'运行暂停','canceling':'正在中断','sync_failed':'回传失败，请重试回传','failed':'执行失败','canceled':'已中断','lost':'状态未知'}
    for key in task.get('depends_on',[]):
        dep=index.get(key)
        if dep and dep['status']=='success' and dep.get('synced_at'):continue
        if not dep:pending.append('前序不存在 '+key);continue
        title='#'+str(dep.get('seq','?'))+' '+dep['name'];status=dep['status']
        if status=='syncing':
            percent=dep.get('runtime_progress',{}).get('percent')
            detail='计算已结束，回传中'+(f' {max(0,min(100,int(percent)))}%' if isinstance(percent,(int,float)) and math.isfinite(percent) else '')
        elif dep.get('archived'):detail='前序已归档，需先恢复归档'
        elif status=='success':detail='等待本机回传确认'
        else:detail=labels.get(status,status)
        pending.append(title+'（'+detail+'）')
    return '等待前序：'+'；'.join(pending[:3])+('；另有 '+str(len(pending)-3)+' 个前序未就绪' if len(pending)>3 else '') if pending else ''


def choose(task, servers, tasks, settings):
    """Rank globally: idle first, measured TFLOPS, available memory, stable IDs."""
    if task.get('archived'):return None
    options = []
    now = time.time()
    for server in servers:
        if not server.get('enabled') or server.get('paused') or not server.get('connected'):
            continue
        if task.get('server_ids') and server['id'] not in task['server_ids']:
            continue
        snap = server.get('snapshot', {})
        if now - snap.get('sampled_at', 0) > settings.get('stale_seconds', 30):
            continue
        if snap.get('work_free_gb', 0) < server.get('min_disk_gb', 5):
            continue
        for gpu in snap.get('gpus', []):
            key = gpu.get('uuid') or str(gpu['index'])
            if key in server.get('blocked_gpus', []):
                continue
            if task.get('gpu_name') and task['gpu_name'].lower() not in gpu.get('name', '').lower():
                continue
            owned = [t for t in tasks if t['status'] in ACTIVE and t.get('gpu_uuid') == key and (gpu.get('uuid') or t.get('server_id') == server['id'])]
            # Conservative reservations account for races between dispatch and sampling.
            reserved = sum(max(t.get('memory_mb',0), settings.get('zero_memory_reservation_mb', 1024)) for t in owned)
            samples = server.get('history', {}).get(key, [])[-settings.get('history_samples', 6):] or [gpu]
            peak_mem = max(g.get('memory_used_mb', 0) for g in samples)
            peak_util = max(g.get('utilization_gpu', 0) for g in samples)
            free = max(0, min(gpu.get('memory_free_mb',0), gpu.get('memory_total_mb',0)-peak_mem)-reserved)
            idle = bool(gpu.get('is_idle')) and not owned and peak_util <= 10
            level = task.get('level', 'idle_only')
            if not idle and (level == 'idle_only' or (level == 'low_interference' and peak_util >= task.get('max_utilization',30))):
                continue
            if free < task.get('memory_mb', 0):
                continue
            score = server.get('benchmarks', {}).get(key, {}).get('tensor_tflops', 0) or gpu.get('performance_hint',0)
            options.append((not idle, -score, -free, server['id'], gpu['index'], server, gpu))
    options.sort(key=lambda o: o[:5])
    return (options[0][-2], options[0][-1]) if options else None
