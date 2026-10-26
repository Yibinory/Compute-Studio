"""Prove shared storage by bidirectional visibility, never by path spelling."""
import time

from .storage import uid


def configuration(servers,scope):
    if scope not in ('work','data'):
        raise ValueError('未知存储类型')
    return {s['id']:[s.get(k) for k in ('host','port','username',scope+'_root','identity_file','proxy_jump','trust_new_host','credential_revision')] for s in servers}


def detect(servers, remote, scope):
    if scope not in ('work', 'data'):
        raise ValueError('未知存储类型')
    tokens = {s['id']: uid() for s in servers}
    states, views, groups = {}, {}, []
    attempted = []
    try:
        for server in servers:
            sid = server['id']
            attempted.append(server)
            try:
                value = remote(server).call('storage_marker', {'scope': scope, 'token': tokens[sid], 'write': True}, timeout=15)
                if not value['visible']:
                    raise ValueError('探测文件写入后不可见')
                states[sid] = {'status': 'verified', 'root': value.get('root', server[scope+'_root'])}
            except Exception as exc:
                states[sid] = {'status': 'unverified', 'error': str(exc)}
        for source in servers:
            sid = source['id']
            if states[sid]['status'] != 'verified':
                continue
            views[sid] = set()
            try:
                for target in servers:
                    tid = target['id']
                    if states[tid]['status'] == 'verified':
                        value = remote(source).call('storage_marker', {'scope': scope, 'token': tokens[tid]}, timeout=15)
                        if not value['visible']:
                            time.sleep(0.05)
                            value = remote(source).call('storage_marker', {'scope': scope, 'token': tokens[tid]}, timeout=15)
                        if value['visible']:
                            views[sid].add(tid)
            except Exception as exc:
                states[sid] = {'status': 'unverified', 'error': str(exc)}
        for sid, visible in views.items():
            for tid in visible:
                if tid != sid and sid not in views.get(tid, set()):
                    states[sid] = {'status': 'unverified', 'error': '探测可见性不对称，请重试或检查挂载权限'}
                    states[tid] = {'status': 'unverified', 'error': '探测可见性不对称，请重试或检查挂载权限'}
        # A group must be a mutual clique, not a transitive inference through an alias.
        for server in servers:
            sid = server['id']
            if states[sid]['status'] != 'verified':
                continue
            group = next((g for g in groups if all(sid in views.get(t, set()) and t in views.get(sid, set()) for t in g)), None)
            if group is None:
                groups.append([sid])
            else:
                group.append(sid)
    finally:
        for server in attempted:
            try:
                remote(server).call('storage_marker', {'scope': scope, 'token': tokens[server['id']], 'remove': True}, timeout=15)
            except Exception as exc:
                states[server['id']] = {'status': 'unverified', 'error': '探测文件清理失败：'+str(exc)}
    groups = [[sid for sid in group if states[sid]['status'] == 'verified'] for group in groups]
    return {'id': scope, 'scope': scope, 'checked_at': time.time(), 'groups': [g for g in groups if g], 'states': states,
            'configuration': configuration(servers,scope)}
