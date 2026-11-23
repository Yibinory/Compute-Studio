"""Collect returned experiment measurements without replacing a researcher's table."""
import copy
import codecs
import hashlib
import json
import re
import time

from werkzeug.exceptions import Conflict

from . import result_tables as tables
from .content import Content
from .files import confined, relative
from .logs import CHUNK_BYTES, chunk_stream
from .storage import uid


def completed_runs(task):
    current=task.get('attempt_id')
    for run in task.get('previous_attempts',[]):
        if run.get('attempt_id') and run['attempt_id']!=current and run.get('exit_code')==0 and run.get('synced_at'):
            yield run,False
    if current and task['status']=='success':
        yield task,True


def run_list(engine,project_id,offset,limit):
    engine.store.get('projects',project_id)
    if offset<0 or not 1<=limit<=200:raise ValueError('分页范围无效，每页 1–200 项')
    runs=[]
    for t in engine.store.summaries('tasks'):
        if t['project_id']!=project_id:continue
        for run,current in completed_runs(t):
            runs.append({'task_id':t['id'],'name':t['name'],'seq':t['seq'],'attempt_id':run['attempt_id'],
                         'current':current,'datasets':t.get('datasets',[]),'synced_at':run.get('synced_at')})
    runs.sort(key=lambda r:(r['seq'],r['synced_at'] or 0,r['attempt_id']))
    return {'project_id':project_id,'runs':runs[offset:offset+limit],'total':len(runs),'offset':offset,'limit':limit}


def require_run(engine,project_id,task_id,attempt_id):
    task=engine.store.get_summary('tasks',tables.identifier(task_id))
    tables.identifier(attempt_id)
    if task['project_id']!=project_id or not any(r['attempt_id']==attempt_id for r,_ in completed_runs(task)):
        raise ValueError('请选择本项目内已成功完成的实验运行')


def evidence(engine,project_id,task_id,attempt_id,path,log,offset,limit):
    require_run(engine,project_id,task_id,attempt_id)
    if offset<0 or not 1<=limit<=CHUNK_BYTES:raise ValueError('读取范围无效')
    if bool(path)==log:raise ValueError('请选择一个产物路径或回传日志')
    pool=Content(engine,project_id);item={}
    if log:
        path='control/run.log'
        source=confined(pool.project,f'runs/{attempt_id}/{path}')
        if not source.is_file():
            path='run.log'
            source=confined(pool.project,f'runs/{attempt_id}/{path}')
        if not source.is_file():raise ValueError('本次运行没有已回传的本机日志')
    else:
        path=relative(path)
        item=pool.run_files(attempt_id,'outputs').get(path)
        if item is None:raise ValueError('此路径不在本次运行的输出清单内')
        if item.get('sha256'):
            source=pool.path(item['sha256'])
        else:source=confined(pool.project,f'runs/{attempt_id}/result/{path}')
    if not source.is_file() or offset>source.stat().st_size:raise ValueError('文件缺失或偏移量超出文件范围')
    try:
        with source.open('rb') as stream:
            head=codecs.getincrementaldecoder('utf-8')().decode(stream.read(2048))
            if '\x00' in head:raise ValueError('该产物不是可读取的 UTF-8 文本')
            if item.get('sha256') and not pool.contains(item['sha256'],item['size']):
                raise ValueError('本机产物缺失或未通过内容核验')
            chunk=chunk_stream(stream,source.stat().st_size,offset,limit,errors='strict')
    except UnicodeDecodeError:
        raise ValueError('文件须为 UTF-8 文本，请从 next_offset 继续读取') from None
    return {**chunk,'source':'local','task_id':task_id,'attempt_id':attempt_id,'path':path,'log_tail_snapshot':log and path=='run.log'}


def stable_id(prefix,*parts):
    digest=hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return prefix+digest[:40]


def target_table(engine,pid,dataset,body):
    if 'table_id' in body:
        if not isinstance(body['table_id'],str):raise ValueError('表 ID 须为字符串')
        doc=tables.read(engine,pid,dataset,body['table_id'])
        if 'name' in body and tables.table_name(body['name'])!=doc['name']:raise Conflict('结果表 ID 与表名不一致')
        return doc
    name=tables.table_name(body.get('name'))
    for item in tables.table_list(engine,pid,dataset):
        if name.casefold()==item['name'].casefold():return tables.read(engine,pid,dataset,item['table_id'])
    # A rename leaves the original ID in use. Pick a fresh ID on creation,
    # and look up the persisted name under the store lock on retries.
    table_id=uid()
    return {'id':tables.table_key(pid,dataset,table_id),'project_id':pid,'dataset':dataset,'name':name,
            'table_id':table_id,'revision':0,'fields':['label','task','group','seed','notes'],'columns':[],'rows':[]}


def merged_columns(existing,additions):
    if not isinstance(additions,list):raise ValueError('columns 须为数组')
    result=list(existing);by_id={c['id']:c for c in existing}
    for col in additions:
        if not isinstance(col,dict):raise ValueError('指标列须为对象')
        key=tables.identifier(col.get('id'))
        if key not in by_id:
            if not tables.text(col.get('name'),80,'指标名称'):raise ValueError('新增列需要指标名称')
            result.append(col);by_id[key]=col
        elif any(by_id[key].get(k)!=v for k,v in col.items()):
            raise Conflict('已有指标定义不同，请读取原表核对名称、单位与指标 ID')
    return result


def merge_observation(rows,original_ids,manual_runs,updates,seen,columns):
    if not isinstance(updates,dict):raise ValueError('每条观测须为对象')
    task=tables.identifier(updates.get('task_id'));attempt=tables.identifier(updates.get('attempt_id'))
    key=tables.text(updates.get('key','main'),200,'观测键')
    if not key:raise ValueError('请指定稳定的观测键')
    row_id=stable_id('obs_',task,attempt,key)
    if 'row_id' in updates:
        row_id=tables.identifier(updates['row_id'])
        if row_id not in original_ids:raise ValueError('指定的目标行不存在')
    if row_id in seen:raise ValueError('本批出现重复观测，请合并同一行的指标')
    seen.add(row_id)
    row=rows.get(row_id)
    if row is None:
        if (task,attempt) in manual_runs:
            raise Conflict('本次运行已有手工关联行，请核对后用 row_id 指定目标行')
        row={'id':row_id,'task_id':task,'attempt_id':attempt,'values':{},'sources':{}}
        rows[row_id]=row
    elif (row.get('task_id'),row.get('attempt_id'))!=(task,attempt):
        raise Conflict('已有行的实验关联不同')
    for field in ('group','seed','label'):
        if field not in updates:continue
        incoming=tables.text(updates[field],200 if field!='seed' else 64,field)
        if field=='seed' and re.fullmatch(r'[+-]?\d{1,20}',incoming):incoming=str(int(incoming))
        if row.get(field) and row[field]!=incoming:raise Conflict('已有行的 '+field+' 不同，请核对原表')
        row[field]=incoming
    values=updates.get('values');sources=updates.get('sources',{})
    if not isinstance(values,dict) or not values or not isinstance(sources,dict) or set(sources)-set(values):
        raise ValueError('请提供非空指标 values 和对应的来源')
    for metric,value in values.items():
        if value is None or value=='':raise ValueError('省略缺失指标，仅提交有来源的测量值')
        source=tables.text(sources.get(metric,updates.get('source')),1000,'指标来源')
        if not source:raise ValueError('请注明产物路径或回传日志，以及字段或行位置')
        if metric not in columns:raise ValueError('结果包含不存在的指标列：'+metric)
        value=tables.cell_value(columns[metric],value,len(seen)-1)
        if metric in row['values'] and row['values'][metric]!=value:
            raise Conflict('已有指标值不同：'+metric+'。请核对原表与产物，已有值已保留。')
        row['values'][metric]=value
        row.setdefault('sources',{}).setdefault(metric,source)
    return row_id


def merge(engine,pid,dataset,body):
    dataset=tables.dataset_name(dataset)
    if not isinstance(body,dict) or not isinstance(body.get('rows'),list) or not 1<=len(body['rows'])<=tables.MAX_ROWS:
        raise ValueError('请提供 1–5000 条结果观测')
    if 'dataset' in body and body['dataset']!=dataset:raise ValueError('数据集与目标不一致')
    with engine.store.lock:
        original=target_table(engine,pid,dataset,body)
        columns=merged_columns(original['columns'],body.get('columns',[]))
        by_id={c['id']:c for c in columns}
        rows={r['id']:copy.deepcopy(r) for r in original['rows']}
        manual_runs={(r.get('task_id'),r.get('attempt_id')) for r in rows.values() if not re.fullmatch(r'obs_[0-9a-f]{40}',r['id'])}
        original_ids=set(rows);seen=set();checked=set();keys=[]
        for item in body['rows']:
            if not isinstance(item,dict):raise ValueError('每条观测须为对象')
            run=tables.identifier(item.get('task_id')),tables.identifier(item.get('attempt_id'))
            if run not in checked:
                require_run(engine,pid,*run);checked.add(run)
            keys.append(merge_observation(rows,original_ids,manual_runs,item,seen,by_id))
        merged={'columns':columns,'rows':list(rows.values())}
        if len(json.dumps(merged,ensure_ascii=False).encode())>12*1024**2:raise ValueError('结果表最多 12 MB')
        columns,clean=tables.validate(engine,pid,merged,original)
        previous={r['id']:r for r in original['rows']}
        clean=[r if r['id'] in seen else previous[r['id']] for r in clean]
        changed=sum(r['id'] in seen and r!=previous.get(r['id']) for r in clean)
        created=len(seen-original_ids)
        value=original
        if clean!=original['rows'] or columns!=original['columns']:
            value={**original,'rows':clean,'columns':columns,'revision':original['revision']+1,'updated_at':time.time(),'request_id':None}
            tables.persist(engine,value)
        return {**{k:value[k] for k in ('project_id','dataset','table_id','name','revision')},'created':created,
                'updated':changed-created,'unchanged':len(keys)-changed,'row_ids':keys}
