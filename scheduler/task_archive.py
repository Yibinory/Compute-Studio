"""Reversible experiment retirement without deleting provenance or objects."""
import time

SAFE={'draft','queued','waiting','blocked','paused','success','failed','canceled'}


def check_dependencies(engine,task):
    for key in task.get('depends_on',[]):
        try:dep=engine.store.get_summary('tasks',key)
        except ValueError:continue  # New nodes in the same validated import.
        if dep.get('archived') and not (dep['status']=='success' and dep.get('synced_at')):
            raise ValueError('前序实验 '+dep['name']+' 已归档且未完成，请先恢复归档或重新选择前序')


def preview(engine,task_ids,archived):
    if type(archived) is not bool:raise ValueError('归档状态必须为 true 或 false')
    if not isinstance(task_ids,list) or not 1<=len(task_ids)<=1000 or any(not isinstance(k,str) or not k for k in task_ids):
        raise ValueError('请选择 1–1000 个实验')
    tasks=engine.store.summaries('tasks');index={t['id']:t for t in tasks};reasons={};eligible=set()
    ids=list(dict.fromkeys(task_ids))
    for key in ids:
        t=index.get(key)
        if not t:reasons[key]='实验不存在'
        elif bool(t.get('archived'))==archived:reasons[key]='已经归档' if archived else '尚未归档'
        elif t['status'] not in SAFE or key in engine.runtime_pending:reasons[key]='实验正在执行、回传或状态未知，请先中断并完成回传，或恢复状态后再归档'
        elif t['project_id'] in engine.project_storage_busy:reasons[key]='项目正在整理存储，请稍后再试'
        else:eligible.add(key)
    # Remove blocked nodes repeatedly: a selected descendant may itself be
    # unsafe. Never hide an unfinished prerequisite of a retained workflow.
    changed=True
    while changed:
        changed=False
        for key in list(eligible):
            t=index[key];blockers=[]
            if archived and not (t['status']=='success' and t.get('synced_at')):
                blockers=[other for other in tasks if key in other.get('depends_on',[]) and not other.get('archived') and other['id'] not in eligible]
            elif not archived:
                blockers=[index[d] for d in t.get('depends_on',[]) if d in index and index[d].get('archived') and d not in eligible and not (index[d]['status']=='success' and index[d].get('synced_at'))]
            if blockers:
                label='仍有未归档后续引用，请一起选择或先调整依赖：' if archived else '请同时恢复未完成的归档前序：'
                reasons[key]=label+'、'.join('#'+str(v.get('seq','?'))+' '+v['name'] for v in blockers[:3])
                eligible.remove(key);changed=True
    def row(key):
        t=index.get(key,{})
        return {'id':key,'name':t.get('name',key),'seq':t.get('seq'),'status':t.get('status','missing'),'reason':reasons.get(key)}
    return {'archived':archived,'eligible':[row(k) for k in ids if k in eligible],
            'excluded':[row(k) for k in ids if k not in eligible]}


def apply(engine,task_ids,archived):
    with engine.store.lock:
        plan=preview(engine,task_ids,archived);updates=[]
        for item in plan['eligible']:
            task=engine.store.get('tasks',item['id'])
            if archived:
                task.update(archived=True,archived_at=time.time(),archive_previous_status=task['status'])
                if task['status'] in ('queued','waiting','blocked'):
                    task.update(status='paused',reason='已归档并退出调度；恢复后需手动入队')
            else:
                task.update(archived=False,unarchived_at=time.time())
                if task['status']=='paused':task['reason']='已恢复归档实验，等待手动入队'
            updates.append(task)
        engine.store.put_many([('tasks',t) for t in updates])
    if updates:
        engine.store.event(('归档实验' if archived else '恢复归档实验')+'：'+str(len(updates))+' 个；记录与文件保留')
        engine.wake_event.set()
    return {**plan,'changed_count':len(updates)}
