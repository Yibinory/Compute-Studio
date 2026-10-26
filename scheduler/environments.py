"""Environment defaults and connection-specific Conda inventory."""
import re


def environment(value):
    value=str(value or '').strip()
    if len(value)>1024 or re.search(r'[\x00-\x1f\x7f]',value) or value.startswith('-'):
        raise ValueError('Conda 环境名称或路径无效')
    if value and not value.startswith(('/','~/')) and not re.fullmatch(r'[\w.-]+',value):
        raise ValueError('Conda 环境应为名称或 Linux 绝对路径')
    if '..' in value.split('/'):
        raise ValueError('Conda 环境路径不能包含 ..')
    return value


def signature(server):
    return [server.get(k) for k in ('host','port','username','identity_file','proxy_jump',
                                  'trust_new_host','credential_revision','conda_executable')]


def resolve(task,project,server):
    overrides=task.get('server_conda_envs',{})
    common='' if server['id'] in overrides else task.get('conda_env')
    for source,value in [('task_server',overrides.get(server['id'])),('task',common),
                         ('project',project.get('server_conda_envs',{}).get(server['id'])),
                         ('server',server.get('default_conda_env'))]:
        if value:return {'conda_env':environment(value),'source':source}
    return {'conda_env':'','source':'shell'}
