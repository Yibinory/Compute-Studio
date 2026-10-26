"""Reviewed bulk controls; retries preserve dependency and result safety."""
import time

from .policy import ACTIVE

ACTIONS={'queue','cancel','pause','resume','retry','restart'}


def preview(engine,task_ids,action):
    if action not in ACTIONS:raise ValueError('未知批量状态操作')
    if not isinstance(task_ids,list) or not task_ids or len(task_ids)>1000 or any(not isinstance(v,str) for v in task_ids):raise ValueError('请选择 1–1000 个实验')
    selected=set(task_ids);all_tasks=engine.store.summaries('tasks');by_id={t['id']:t for t in all_tasks};eligible=[];excluded=[]
    for key in dict.fromkeys(task_ids):
        t=by_id.get(key);reason=None
        if not t:reason='实验不存在'
        elif t.get('archived'):reason='实验已归档，请先恢复归档后再操作'
        elif t['project_id'] in engine.project_storage_busy:reason='项目正在整理存储'
        elif action=='queue':
            if t['status'] in ('queued','waiting','blocked'):reason='已经入队，无需重复提交'
            elif t['status'] in ('success','failed','canceled'):reason='实验已结束；需要重跑请使用重新执行'
            elif t['status'] not in ('draft','paused') or t.get('attempt_id'):reason='仅草稿和未运行的暂停排队实验可以入队'
            elif any(key in other.get('depends_on',[]) and (other['status'] in ACTIVE or other['status']=='success') for other in all_tasks):reason='后续实验正在执行或已完成，保留现有依赖结果'
        elif action=='cancel' and t['status'] not in ('draft','queued','waiting','blocked','paused','staging','launching','running','suspended','canceling'):reason='已经结束或正在回传'
        elif action=='pause' and t['status'] not in ('draft','queued','waiting','blocked','running'):reason='此状态不能暂停'
        elif action=='resume' and t['status'] not in ('paused','suspended'):reason='未处于暂停状态'
        elif action in ('retry','restart'):
            if t['status'] in ('syncing','sync_failed','lost','canceling') or action=='retry' and t['status'] in ACTIVE:reason='尚未安全结束或回传，不能重复执行'
            elif any(key in other.get('depends_on',[]) and other['id'] not in selected and (other['status'] in ACTIVE or other['status']=='success') for other in all_tasks):reason='存在未选中的运行中／已完成后续；请同时选择后续或新建实验'
        row={'id':key,'name':t['name'] if t else key,'status':t['status'] if t else 'missing','reason':reason}
        (excluded if reason else eligible).append(row)
    return {'action':action,'eligible':eligible,'excluded':excluded,'scheduler_paused':engine.settings['paused']}


def apply(engine,task_ids,action,job=None):
    if action=='queue':
        # Revalidate and publish together. No SSH, file preparation or retry
        # resets here: the scheduler consumes the queued dependency graph later.
        with engine.store.lock:
            plan=preview(engine,task_ids,action);tasks=[]
            for row in plan['eligible']:
                engine.check_cancel(job)
                task=engine.store.get('tasks',row['id'])
                task.update(status='queued',reason='')
                task.pop('paused_at',None)
                tasks.append(task)
            engine.check_cancel(job)
            engine.store.put_many([('tasks',t) for t in tasks])
        engine.wake_event.set()
        engine.progress(job,phase='submitted',label='批量入队完成',files_done=len(tasks),files_total=len(tasks))
        return {'results':[{'id':t['id'],'accepted':True,'status':'queued'} for t in tasks],'excluded':plan['excluded'],'accepted_count':len(tasks),'failed_count':0,'scheduler_paused':engine.settings['paused']}
    plan=preview(engine,task_ids,action);ids=[v['id'] for v in plan['eligible']];results=[]
    def control(key,operation):
        task=engine.store.get_summary('tasks',key)
        if operation in ('cancel','pause','resume') and task['status'] in ('staging','launching','running','suspended','canceling'):
            intent=engine.prepare_control(key,operation)
            remote_job=engine.enqueue('批量：'+operation,engine.perform_control_request,key,operation,intent['control_request_id'])
            return {'id':key,'accepted':True,'control_job':remote_job['id']}
        return {'id':key,'accepted':True,'status':engine.task_action(key,operation)['status']}
    if action=='restart':
        for key in ids:
            engine.check_cancel(job);t=engine.store.get_summary('tasks',key)
            if t['status'] in ACTIVE:
                try:control(key,'cancel')
                except Exception as exc:results.append({'id':key,'accepted':False,'error':str(exc)})
        deadline=time.monotonic()+1800
        while True:
            engine.check_cancel(job)
            waiting=[key for key in ids if engine.store.get_summary('tasks',key)['status'] in ACTIVE]
            if not waiting:break
            failed=[key for key in waiting if engine.store.get_summary('tasks',key)['status'] in ('sync_failed','lost')]
            if failed or time.monotonic()>deadline:
                results.extend({'id':key,'accepted':False,'error':'未能安全退出并回传，保留原运行，未重启'} for key in waiting);ids=[key for key in ids if key not in waiting];break
            engine.progress(job,phase='waiting_for_exit',label='等待整个实验退出并完成结果回传',files_done=len(ids)-len(waiting),files_total=len(ids),waiting=waiting)
            time.sleep(.3)
        action='retry'
    if action=='retry':
        # Reset descendants first. Selected successful descendants no longer
        # lock their selected predecessors; external successful runs stay protected.
        ordered=[];by_id={t['id']:t for t in engine.store.summaries('tasks')};selected=set(ids);children={key:0 for key in ids};parents={key:[] for key in ids}
        for key in ids:
            for parent in by_id[key].get('depends_on',[]):
                if parent in selected:children[parent]+=1;parents[key].append(parent)
        ready=[key for key in ids if children[key]==0]
        while ready:
            key=ready.pop();ordered.append(key)
            for parent in parents[key]:
                children[parent]-=1
                if children[parent]==0:ready.append(parent)
        if len(ordered)!=len(ids):raise ValueError('依赖存在环，未重新执行任何实验')
        ids=ordered
    import contextlib
    with engine.store.lock if action=='retry' else contextlib.nullcontext():
        for i,key in enumerate(ids):
            engine.check_cancel(job)
            try:results.append(control(key,action))
            except Exception as exc:results.append({'id':key,'accepted':False,'error':str(exc)})
            engine.progress(job,phase='submitting',label='提交批量状态操作',files_done=i+1,files_total=len(ids))
    return {'results':results,'excluded':plan['excluded'],'accepted_count':sum(r['accepted'] for r in results),'failed_count':sum(not r['accepted'] for r in results),'scheduler_paused':engine.settings['paused']}
