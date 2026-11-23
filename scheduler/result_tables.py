"""Project-local manual result tables, provenance and seed summaries."""
import copy
import csv
import hashlib
import io
import json
import math
import re
import statistics
import time

from werkzeug.exceptions import Conflict

DEFAULT_COLUMNS=[{'id':'metric_1','name':'指标 1','type':'number','direction':'max','precision':4,'unit':''},
                 {'id':'metric_2','name':'指标 2','type':'number','direction':'max','precision':4,'unit':''}]
MAX_ROWS=5000
MAX_COLUMNS=24
DEFAULT_NAME='默认结果表'
DEFAULT_FIELDS=['task','group','seed','notes']


def text(value,limit,label):
    if not isinstance(value,str) or len(value)>limit or '\x00' in value:raise ValueError(label+'须为不超过 '+str(limit)+' 字符的文本')
    return value.strip()


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):raise ValueError('记录或指标 ID 无效')
    return value


def dataset_name(value):
    value=text(value,120,'数据集名称')
    if any(ord(c)<32 for c in value):raise ValueError('数据集名称不能包含换行或控制字符')
    return value


def table_key(project_id,dataset='',table_id=''):
    scope=project_id+':'+hashlib.sha256(dataset.encode('utf-8')).hexdigest() if dataset else project_id
    return scope+':table:'+identifier(table_id) if table_id else scope


def read(engine,project_id,dataset='',table_id=''):
    engine.store.get('projects',project_id)
    dataset=dataset_name(dataset);key=table_key(project_id,dataset,table_id)
    try:value=engine.store.get('result_tables',key)
    except ValueError:
        if table_id:raise ValueError('当前数据集下没有这张结果表') from None
        value={'id':key,'project_id':project_id,'revision':0,'columns':copy.deepcopy(DEFAULT_COLUMNS),'rows':[],'updated_at':None}
    return {'name':DEFAULT_NAME,'fields':DEFAULT_FIELDS.copy(),**value,'dataset':dataset,'table_id':table_id}


def scopes(engine,project_id):
    engine.store.get('projects',project_id)
    try:saved=engine.store.get('result_table_scopes',project_id)['datasets']
    except ValueError:saved=[]
    names=set(saved)
    names.update(d['name'] for d in engine.store.summaries('datasets'))
    names.update(name for t in engine.store.summaries('tasks') if t['project_id']==project_id for name in t.get('datasets',[]))
    return {'datasets':sorted(names,key=str.casefold),'saved_datasets':saved}


def scope_record(engine,project_id,dataset):
    try:value=engine.store.get('result_table_scopes',project_id)
    except ValueError:value={'id':project_id,'datasets':[]}
    if dataset and dataset not in value['datasets']:value['datasets'].append(dataset)
    return value


def table_list(engine,project_id,dataset):
    engine.store.get('projects',project_id)
    dataset=dataset_name(dataset)
    index=scope_record(engine,project_id,dataset).get('tables',{}).get(dataset,{})
    return [{'table_id':'','name':index.get('',DEFAULT_NAME)},
            *[{'table_id':key,'name':name} for key,name in index.items() if key]]


def register_name(engine,project_id,dataset,table_id,name):
    value=scope_record(engine,project_id,dataset)
    names=value.setdefault('tables',{}).setdefault(dataset,{'':DEFAULT_NAME})
    if any(key!=table_id and label.casefold()==name.casefold() for key,label in names.items()):
        raise ValueError('当前数据集已有同名结果表')
    names[table_id]=name
    return value


def table_name(value):
    name=text(value,80,'表名')
    if not name or any(ord(c)<32 for c in name):raise ValueError('请填写有效表名')
    return name


def visible_fields(value):
    allowed={'label',*DEFAULT_FIELDS}
    if not isinstance(value,list) or not all(isinstance(v,str) and v in allowed for v in value) or len(set(value))!=len(value):
        raise ValueError('结果表显示字段无效')
    return value


def persist(engine,value):
    catalog=register_name(engine,value['project_id'],value['dataset'],value['table_id'],value['name'])
    engine.store.put_many([('result_tables',value),('result_table_scopes',catalog)])
    return value


def create(engine,project_id,dataset,data):
    engine.store.get('projects',project_id)
    if not isinstance(data,dict):raise ValueError('请提供结果表设置')
    dataset=dataset_name(dataset);table_id=identifier(data.get('table_id'));name=table_name(data.get('name'))
    key=table_key(project_id,dataset,table_id)
    with engine.store.lock:
        try:return engine.store.get('result_tables',key)
        except ValueError:pass
        columns,rows=validate(engine,project_id,{'columns':data.get('columns',[]),'rows':[]},{'rows':[],'table_id':table_id})
        value={'id':key,'project_id':project_id,'dataset':dataset,'table_id':table_id,'name':name,
               'fields':visible_fields(data.get('fields',['label','group','task'])),'revision':1,
               'updated_at':time.time(),'columns':columns,'rows':rows}
        return persist(engine,value)


def cell_value(col,value,index):
    if col.get('type','number')=='text':return text(value,10000,'文本指标')
    try:
        if isinstance(value,bool) or not isinstance(value,(int,float,str)):raise ValueError()
        number=float(value)
        if not math.isfinite(number):raise ValueError()
        return number
    except (ValueError,TypeError,OverflowError):
        raise ValueError(f'第 {index+1} 行「{col["name"]}」须为有限数值；缺失结果请留空') from None


def validate(engine,project_id,data,previous):
    columns=data.get('columns');rows=data.get('rows')
    minimum=0 if previous.get('table_id') else 1
    if not isinstance(columns,list) or not minimum<=len(columns)<=MAX_COLUMNS:raise ValueError(f'请保留 {minimum}–24 个指标列')
    if not isinstance(rows,list) or len(rows)>MAX_ROWS:raise ValueError('每张数据集结果表最多整理 5000 条结果记录')
    clean_columns=[];seen=set();names=set()
    for col in columns:
        if not isinstance(col,dict):raise ValueError('指标列格式无效')
        key=identifier(col.get('id'));label=text(col.get('name'),80,'指标名称')
        if not label or key in seen or label.casefold() in names:raise ValueError('指标名称和 ID 不能留空或重复')
        seen.add(key);names.add(label.casefold());kind=col.get('type','number');direction=col.get('direction','max');precision=col.get('precision',4)
        if kind not in ('number','text') or direction not in ('max','min','none'):raise ValueError('指标类型或比较方向无效')
        if type(precision)!=int or not 0<=precision<=10:raise ValueError('显示小数位须为 0–10 的整数')
        clean_columns.append({'id':key,'name':label,'type':kind,'direction':direction if kind=='number' else 'none','precision':precision,'unit':text(col.get('unit',''),40,'单位')})
    prior={r['id']:r for r in previous['rows']};tasks={t['id']:t for t in engine.store.summaries('tasks') if t['project_id']==project_id}
    clean_rows=[];seen_rows=set();seeds=set()
    for index,row in enumerate(rows):
        if not isinstance(row,dict):raise ValueError('结果行格式无效')
        key=identifier(row.get('id'))
        if key in seen_rows:raise ValueError('结果记录 ID 重复')
        seen_rows.add(key);group=text(row.get('group',''),200,'实验组别');seed=text(row.get('seed',''),64,'Seed')
        if re.fullmatch(r'[+-]?\d{1,20}',seed):seed=str(int(seed))
        if group and seed:
            if (group,seed) in seeds:raise ValueError('同组 Seed 重复：'+group+' / '+seed+'；请区分方法、数据集或数据划分，避免重复计入统计')
            seeds.add((group,seed))
        cells=row.get('values',{})
        if not isinstance(cells,dict) or set(cells)-seen:raise ValueError('结果包含不存在的指标列')
        values={}
        for col in clean_columns:
            value=cells.get(col['id'])
            if value is None or value=='':continue
            values[col['id']]=cell_value(col,value,index)
        task_id=row.get('task_id') or None;attempt=row.get('attempt_id') or None;snapshot=None
        if task_id:
            if not isinstance(task_id,str):raise ValueError('关联实验无效')
            task=tasks.get(task_id)
            if not task:
                old=prior.get(key,{})
                if old.get('task_id')!=task_id or old.get('attempt_id')!=attempt:raise ValueError('关联实验不存在或不属于此项目')
                snapshot=old.get('task_snapshot')
            else:
                attempts={v.get('attempt_id') for v in task.get('previous_attempts',[])}|{task.get('attempt_id')}
                if attempt and attempt not in attempts:raise ValueError('关联的运行不属于所选实验，请重新选择')
                snapshot={'id':task_id,'name':task['name'],'seq':task['seq']}
        elif attempt:raise ValueError('请先关联实验再选择运行')
        clean_rows.append({'id':key,'group':group,'seed':seed,'task_id':task_id,'attempt_id':attempt,'task_snapshot':snapshot,'values':values,'notes':text(row.get('notes',''),100000,'备注')})
        if row.get('label'):clean_rows[-1]['label']=text(row['label'],200,'行名')
        old=prior.get(key,{})
        old_sources=old.get('sources',{})
        sources=row.get('sources',old_sources)
        if not isinstance(sources,dict):raise ValueError('指标来源须为对象')
        sources={k:text(v,1000,'指标来源') for k,v in sources.items() if k in values and
                 (v!=old_sources.get(k) or (values[k]==old.get('values',{}).get(k) and
                  (task_id,attempt)==(old.get('task_id'),old.get('attempt_id'))))}
        if sources:clean_rows[-1]['sources']=sources
    return clean_columns,clean_rows


def save(engine,project_id,data,dataset='',table_id=''):
    dataset=dataset_name(dataset)
    if not isinstance(data,dict):raise ValueError('结果表须为对象')
    if 'dataset' in data and data['dataset']!=dataset:raise ValueError('保存的数据集与当前结果表不一致，请重新打开对应结果表')
    if data.get('table_id',table_id)!=table_id:raise ValueError('保存目标与当前结果表不一致')
    if len(json.dumps({'columns':data.get('columns'),'rows':data.get('rows')},ensure_ascii=False).encode('utf-8'))>12*1024**2:raise ValueError('结果表最多 12 MB，请减少长备注或记录数')
    revision=data.get('revision');request_id=data.get('request_id')
    if type(revision)!=int or revision<0:raise ValueError('缺少有效的结果表版本')
    if request_id is not None:identifier(request_id)
    with engine.store.lock:
        previous=read(engine,project_id,dataset,table_id)
        name=table_name(data.get('name',previous['name']))
        fields=visible_fields(data.get('fields',previous['fields']))
        columns,rows=validate(engine,project_id,data,previous)
        if request_id and previous.get('request_id')==request_id and previous['columns']==columns and previous['rows']==rows and previous['name']==name and previous['fields']==fields:return previous
        if revision!=previous['revision']:raise Conflict('结果表已被其他页面更新，本页草稿已保留。请导出草稿后重新载入，避免覆盖他人的修改。')
        value={**previous,'revision':revision+1,'updated_at':time.time(),'request_id':request_id,'columns':columns,'rows':rows,'name':name,'fields':fields}
        return persist(engine,value)


def assign_dataset(engine,project_id,data):
    """Atomically label the legacy table; never merge into another saved table."""
    if not isinstance(data,dict):raise ValueError('请提供数据集名称和原结果表版本')
    dataset=dataset_name(data.get('dataset',''));revision=data.get('revision');request_id=data.get('request_id')
    if not dataset:raise ValueError('请填写数据集名称')
    if type(revision)!=int or revision<0:raise ValueError('缺少有效的结果表版本')
    identifier(request_id)
    with engine.store.lock:
        original=read(engine,project_id);target=read(engine,project_id,dataset)
        if target.get('assignment_request_id')==request_id:return target
        if original['revision']!=revision:raise Conflict('原结果表已被其他页面更新，请重新载入后再指定数据集。')
        if target['revision']:raise Conflict('该数据集已有结果表，请切换后查看；不会覆盖已有指标或记录。')
        catalog=register_name(engine,project_id,dataset,'',original['name'])
        catalog.setdefault('tables',{}).setdefault('',{})['']=DEFAULT_NAME
        target.update(columns=copy.deepcopy(original['columns']),rows=copy.deepcopy(original['rows']),name=original['name'],fields=original['fields'],revision=1,
                      updated_at=time.time(),assignment_request_id=request_id)
        empty={**original,'columns':copy.deepcopy(DEFAULT_COLUMNS),'rows':[],'name':DEFAULT_NAME,'fields':DEFAULT_FIELDS.copy(),'revision':revision+1,'updated_at':time.time()}
        empty.pop('request_id',None)
        engine.store.put_many([('result_tables',target),('result_tables',empty),('result_table_scopes',catalog)])
        return target


def summarize(table):
    groups={}
    for row in table['rows']:
        key=('group',row['group']) if row['group'] else ('row',row['id'])
        groups.setdefault(key,[]).append(row)
    result=[]
    for (kind,key),rows in groups.items():
        values={}
        for col in table['columns']:
            items=[r['values'][col['id']] for r in rows if col['id'] in r['values']]
            if col['type']=='number':
                mean=statistics.mean(items) if items else None
                try:std=statistics.stdev(items) if len(items)>1 else None
                except (OverflowError,ValueError):std=None
                values[col['id']]={'n':len(items),'mean':mean,'std':std}
            else:values[col['id']]={'n':len(items),'text':list(dict.fromkeys(items))}
        result.append({'key':kind+':'+key,'name':rows[0]['group'] or rows[0].get('label') or '未分组记录','row_ids':[r['id'] for r in rows],
                       'seeds':[r['seed'] for r in rows if r['seed']],'count':len(rows),'values':values})
    return result


def chart_data(table,metric_ids,group_keys,mode,offset,limit):
    if mode not in ('bar','line','point','box'):raise ValueError('不支持的图形类型')
    if offset<0 or not 1<=limit<=200:raise ValueError('分页范围无效，每页 1–200 组')
    numeric=[c for c in table['columns'] if c['type']=='number']
    selected=set(metric_ids)
    if selected-{c['id'] for c in numeric}:raise ValueError('请选择本表中的数值指标 ID')
    columns=[c for c in numeric if not selected or c['id'] in selected]
    groups=summarize({**table,'columns':columns})
    keys=set(group_keys)
    if keys-{g['key'] for g in groups}:raise ValueError('实验组不属于当前结果表')
    matched=[g for g in groups if not keys or g['key'] in keys]
    best={}
    for c in columns:
        values=[g['values'][c['id']]['mean'] for g in matched if g['values'][c['id']]['n']]
        if len(values)>1 and c['direction']!='none':
            best[c['id']]=(max if c['direction']=='max' else min)(values)
    rows={r['id']:r for r in table['rows']}
    page=[]
    for group in matched[offset:offset+limit]:
        values=group['values']
        for metric,stat in values.items():
            stat['best']=metric in best and stat['mean']==best[metric]
            if mode=='box' and stat['n']:
                data=sorted(rows[k]['values'][metric] for k in group['row_ids'] if metric in rows[k]['values'])
                def percentile(fraction):
                    pos=(len(data)-1)*fraction
                    share=pos%1
                    return data[int(pos)]*(1-share)+data[math.ceil(pos)]*share
                stat.update(min=data[0],q1=percentile(.25),median=percentile(.5),q3=percentile(.75),max=data[-1])
        page.append({'key':group['key'],'name':group['name'],'values':values})
    return {'table_id':table['table_id'],'dataset':table['dataset'],'revision':table['revision'],'mode':mode,
            'metrics':columns,'groups':page,'total':len(matched),'offset':offset,'limit':limit}


def export_csv(table,summary=False):
    buffer=io.StringIO(newline='');writer=csv.writer(buffer)
    labels={c['id']:c['name']+(' ('+c['unit']+')' if c.get('unit') else '') for c in table['columns']}
    def safe(value):
        if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):return "'"+value
        return value
    if summary:
        headers=['数据集','实验组别','关联实验','实验 ID','运行 ID','Seed','记录数'];by_id={r['id']:r for r in table['rows']}
        for col in table['columns']:headers.extend([labels[col['id']]+' 均值',labels[col['id']]+' 样本标准差',labels[col['id']]+' n'] if col['type']=='number' else [labels[col['id']]])
        writer.writerow([safe(v) for v in headers])
        for group in summarize(table):
            rows=[by_id[key] for key in group['row_ids']]
            names=list(dict.fromkeys('#'+str(r['task_snapshot']['seq'])+' '+r['task_snapshot']['name'] for r in rows if r.get('task_snapshot')))
            line=[table.get('dataset') or '未指定数据集',group['name'],'; '.join(names),'; '.join(dict.fromkeys(r['task_id'] for r in rows if r.get('task_id'))),'; '.join(dict.fromkeys(r['attempt_id'] for r in rows if r.get('attempt_id'))),', '.join(group['seeds']),group['count']]
            for col in table['columns']:
                item=group['values'][col['id']];line.extend([item['mean'],item['std'],item['n']] if col['type']=='number' else [' / '.join(item['text'])])
            writer.writerow([safe(v) for v in line])
    else:
        row_names='label' in table.get('fields',[]) or any(r.get('label') for r in table['rows'])
        writer.writerow([safe(v) for v in ['数据集',*(['行名'] if row_names else []),'关联实验编号','关联实验','实验 ID','运行 ID','实验组别','Seed',*[labels[c['id']] for c in table['columns']],'备注']])
        for row in table['rows']:
            task=row.get('task_snapshot') or {};writer.writerow([safe(v) for v in [table.get('dataset') or '未指定数据集',*([row.get('label','')] if row_names else []),task.get('seq',''),task.get('name',''),row['task_id'],row['attempt_id'],row['group'],row['seed'],*[row['values'].get(c['id'],'') for c in table['columns']],row['notes']]])
    return ('\ufeff'+buffer.getvalue()).encode('utf-8')
