"""Explicit import overrides; original experiment files stay untouched."""
from .environments import environment
from .files import name
from .scheduling_edits import NUMBERS

FIELDS=set(NUMBERS)|{'level','gpu_name','server_ids','conda_env','server_conda_envs','datasets'}


def validate(engine, changes):
    if not isinstance(changes,dict) or set(changes)-FIELDS:raise ValueError('导入覆盖配置包含未知字段')
    result=dict(changes)
    for key,(low,high,integer) in NUMBERS.items():
        if key in result:result[key]=engine.numeric(result[key],low,high,integer)
    if 'level' in result and result['level'] not in ('idle_only','low_interference','emergency'):raise ValueError('未知调度级别')
    for key in ('gpu_name','conda_env'):
        if key in result:
            if not isinstance(result[key],str):raise ValueError(key+' 必须为文本')
            result[key]=environment(result[key]) if key=='conda_env' else result[key].strip()
    for key in ('server_ids','datasets'):
        if key in result:
            if not isinstance(result[key],list) or any(not isinstance(v,str) or not v for v in result[key]):raise ValueError(key+' 须为字符串数组')
            result[key]=list(dict.fromkeys(result[key]))
            for value in result[key]:
                if key=='server_ids':engine.store.get('servers',value)
                else:name(value)
    if 'server_conda_envs' in result:
        if not isinstance(result['server_conda_envs'],dict):raise ValueError('服务器环境必须为映射')
        mapping={}
        for sid,value in result['server_conda_envs'].items():
            engine.store.get('servers',sid)
            if not isinstance(value,str):raise ValueError('Conda 环境必须为文本')
            mapping[sid]=environment(value)
        result['server_conda_envs']=mapping
    return result


def apply(entries, changes):
    result=[]
    for entry in entries:
        item={**entry,**changes}
        if 'server_ids' in changes:item.pop('server_names',None)
        result.append(item)
    return result
