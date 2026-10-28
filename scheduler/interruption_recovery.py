"""Requeue proven infrastructure interruptions, preserving inputs and logs."""
import shutil
import time

from .content import Content
from .operations import OperationCanceled


def cleanup_failed_workspaces(engine,job=None):
    """Remove ended failure working copies, retaining the content-addressed archive."""
    candidates={};results=[]
    for task in engine.store.summaries('tasks'):
        rows=task.get('previous_attempts',[])+([task] if task['status'] in ('failed','canceled') else [])
        for row in rows:
            if row.get('attempt_id') and row.get('server_id') and (row.get('interrupted') or isinstance(row.get('exit_code'),int) and row['exit_code']!=0):
                candidates[(task['project_id'],row['attempt_id'])]=(task,row)
    for i,((pid,attempt),(task,row)) in enumerate(candidates.items()):
        engine.check_cancel(job);pool=Content(engine,pid)
        if pool.read_catalog(attempt).get('workspace_cleanup'):continue
        spec={'project_id':pid,'attempt_id':attempt}
        engine.progress(job,phase='cleanup',label='清理已结束的失败运行工作副本，保留日志与共享内容',files_done=i,files_total=len(candidates))
        try:
            remote=engine.remote(engine.store.get('servers',row['server_id']))
            state=remote.call('status',spec)
            if state['state']!='finished' or not (state.get('interrupted') or isinstance(state.get('exit_code'),int) and state['exit_code']!=0):
                results.append({'attempt_id':attempt,'skipped':True,'reason':'远程未确认失败结束'});continue
            remote.download(pool.return_destination(attempt,'control'),spec,'runs/'+attempt+'/_control')
            engine.check_cancel(job)
            cleanup=remote.call('discard_failed_workspace',spec,timeout=600)
            pool.save_catalog(attempt,workspace_cleanup=cleanup)
            results.append({'attempt_id':attempt,'project_id':pid,**cleanup})
        except OperationCanceled:raise
        except Exception as exc:results.append({'attempt_id':attempt,'error':str(exc)})
    report={'runs':results,'cleaned_count':sum('cleaned_at' in v for v in results),
            'removed_workspace_bytes':sum(v.get('removed_workspace_bytes',0) for v in results),'shared_objects_preserved':True}
    engine.progress(job,phase='complete',label='失败工作副本清理完成；日志与归档输出保留',files_done=len(candidates),files_total=len(candidates))
    engine.store.event('清理失败运行副本：'+str(report['cleaned_count'])+' 个，工作副本 '+str(report['removed_workspace_bytes'])+' 字节；共享内容库保留')
    return report


def recover(engine,task,remote):
    if task.get('recovery_check_after',0)>time.time():return
    task['status']='lost'
    attempt=task['attempt_id'];spec={**engine.spec(task),'known_process':task.get('remote_state',{}),'known_started_at':task.get('started_at')}
    proof=remote.call('interruption_status',spec)
    if not proof.get('confirmed_stopped'):
        task.update(status='lost',recovery_check_after=time.time()+30,reason='远程进程状态尚未确认，继续核验；不会重复启动或清理运行文件')
        engine.put_runtime_task(task);return
    task.update(status='lost',interruption=proof,recovery_check_after=time.time()+30)
    engine.runtime_progress(task,'recovering','已确认服务器重启；保存日志并清理中断工作副本')
    remote.call('seal_interrupted',spec)
    pool=Content(engine,task['project_id']);catalog=pool.read_catalog(attempt)
    snapshot_ok=task.get('storage_version')==2 and catalog.get('inputs') is not None
    if snapshot_ok:
        for item in {v['sha256']:v for v in catalog['inputs'].values()}.values():
            engine.check_cancel(None)
            if not pool.contains(item['sha256'],item['size']):snapshot_ok=False;break
    # The log download must succeed before removing anything. A broken network
    # leaves the same attempt pending, and the next poll resumes this step.
    remote.download(pool.return_destination(attempt,'control'),engine.spec(task),'runs/'+attempt+'/_control')
    engine.check_cancel(None)
    log=pool.return_destination(attempt,'control')/'run.log'
    if log.is_file():shutil.copyfile(log,pool.return_destination(attempt,'run.log'))
    cleanup=remote.call('discard_failed_workspace',spec,timeout=600)
    pool.save_catalog(attempt,outputs={},artifacts={},interruption=proof,workspace_cleanup=cleanup,discarded_incomplete_outputs=True)
    local_result=pool.return_destination(attempt,'result')
    if local_result.is_dir():
        if local_result.resolve().parent!=pool.catalog(attempt).parent.resolve():raise ValueError('本机失败结果目录越界')
        shutil.rmtree(local_result)
    with engine.store.lock:
        latest=engine.store.get('tasks',task['id'])
        if latest.get('attempt_id')!=attempt:raise OperationCanceled('运行记录已经变化，忽略旧恢复任务')
        count=latest.get('interruption_retries',0);limit=engine.settings.get('interruption_retry_limit',2)
        canceled=latest.get('cancel_requested') or latest.get('canceled')
        paused=latest.get('control_request')=='pause' or latest['status']=='suspended' or task.get('interruption_was_suspended')
        blockers=any(task['id'] in t.get('depends_on',[]) and (t['status']=='success' or t['status'] in ('staging','launching','running','suspended','syncing','sync_failed','canceling')) for t in engine.store.summaries('tasks'))
        reason=('用户已请求中断，不自动重跑' if canceled else '原实验已暂停，保持暂停排队' if paused else
                '原输入快照缺失，停止自动重跑以免使用不同代码' if not snapshot_ok else
                '存在已执行的后续实验，保留依赖关系并停止自动重跑' if blockers else
                '异常中断自动重试已关闭' if not engine.settings.get('auto_requeue_interrupted',True) else
                '异常中断自动重试次数已用尽' if count>=limit else '')
        latest.update(interruption=proof,workspace_cleanup=cleanup,result_manifest={},exit_code=None,synced_at=None)
        if reason or latest.get('archived'):
            latest.update(status='canceled' if canceled else 'paused' if paused else 'failed',finished_at=time.time(),reason='服务器重启中断；日志已保留、失败工作副本已清理；'+reason)
        else:
            history={'attempt_id':attempt,'server_id':latest.get('server_id'),'exit_code':None,'synced_at':None,
                     'interrupted':True,'interruption_cause':proof.get('interruption_cause','server_reboot'),
                     'logs_saved_at':time.time(),'workspace_cleanup':cleanup,
                     'resolved_conda_env':latest.get('resolved_conda_env'),'conda_env_source':latest.get('conda_env_source')}
            latest['previous_attempts']=latest.get('previous_attempts',[])+[history]
            delay=engine.settings.get('interruption_retry_delay',30)
            latest.update(status='queued',reason=f'服务器重启中断；失败副本已清理，{delay} 秒后自动重试（{count+1}/{limit}），沿用原输入快照',
                          interruption_retries=count+1,retry_snapshot_attempt=attempt,preparation_retry_at=time.time()+delay,
                          server_id=None,attempt_id=None,gpu_uuid=None,gpu_index=None,started_at=None,finished_at=None,
                          cancel_requested=False,canceled=False,remote_state={},runtime_progress={})
            for field in ('input_manifest','result_manifest','control_request','control_request_id','control_requested_at','resolved_conda_env','conda_env_source','recovery_check_after','interruption_was_suspended'):
                latest.pop(field,None)
        engine.store.put('tasks',latest)
        engine.store.event('异常恢复：#'+str(latest.get('seq',''))+' '+latest['name']+'；'+latest['reason']+'；清理工作副本 '+str(cleanup.get('removed_workspace_bytes',0))+' 字节')
    engine.wake_event.set()
