"""Work locations are storage endpoints. GPU reservations always belong to the host."""
import hashlib
import posixpath


class WorkDiskFull(ValueError):
    """The selected remote work filesystem cannot complete this operation."""
    def __init__(self, message, required_gb=None):
        super().__init__(message)
        self.required_gb=required_gb


def roots(server):
    return list(dict.fromkeys([server.get('work_root', ''), *server.get('extra_work_roots', [])]))


def preferred_root(project, server):
    paths = roots(server)
    if len(paths) == 1:
        return paths[0]
    chosen = project.get('preferred_work_roots', {}).get(server['id'])
    if chosen in paths:
        return chosen
    previous = server.get('existing_project_root')
    if previous and previous['path'] in paths and project.get('created_at', 0) <= previous['through']:
        return previous['path']
    return None


def capacity(server):
    """Summarize the last sample for the configured work paths on one host."""
    snapshot = server.get('snapshot') or {}
    samples = snapshot.get('work_volumes', {})
    disks = {}
    unavailable = []
    paths = roots(server)
    for path in paths:
        sample = samples.get(path, {})
        if len(paths) == 1 and not sample:
            sample = {**(snapshot.get('work_disk') or {}), 'free_gb':snapshot.get('work_free_gb')}
        device = sample.get('device')
        free = sample.get('free_bytes')
        if free is None and sample.get('free_gb') is not None:
            free = round(sample['free_gb'] * 1024**3)
        if sample.get('error') or free is None or (len(paths) > 1 and not device):
            unavailable.append({'path':path, 'error':sample.get('error') or '等待采样'})
            continue
        if device not in disks:
            disks[device] = {'paths':[], 'free_bytes':free, 'total_bytes':sample.get('total_bytes'),
                             'used_bytes':sample.get('used_bytes')}
        disks[device]['paths'].append(path)
    values = list(disks.values())
    def total(field):
        if values and all(v[field] is not None for v in values):
            return sum(v[field] for v in values)
        return None
    return {'disks':values, 'unavailable':unavailable, 'free_bytes':total('free_bytes'),
            'total_bytes':total('total_bytes'), 'used_bytes':total('used_bytes')}


def validate_extra(value, primary):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ValueError('附加工作目录须为路径列表')
    paths = []
    for raw in value:
        p = raw.strip().rstrip('/')
        if not p and not raw.strip():
            continue
        if (not p.startswith(('/', '~/')) or '..' in p.split('/') or '.compute-projects' in p.split('/') or
                posixpath.normpath(p) != p or p in ('/home','/tmp','/usr','/etc','/root','/var') or
                any(ord(c)<32 for c in p)):
            raise ValueError('附加工作目录必须是具体的 Linux 绝对目录或 ~/ 子目录')
        if p == primary.rstrip('/') or p in paths:
            raise ValueError('工作目录重复')
        paths.append(p)
    return paths


def endpoint_id(server, path):
    if path == server['work_root']:
        return server['id']
    return server['id'] + '~' + hashlib.sha256(path.encode()).hexdigest()[:20]


def endpoints(servers):
    locations = {}
    for server in servers:
        for path in roots(server):
            key = endpoint_id(server, path)
            locations[key] = {**server, 'id':key, 'host_id':server['id'], 'work_root':path}
    return locations


def available(task, server, preferred=None):
    """Sorted candidates from one resource sample; never count paths as extra GPUs."""
    paths = roots(server)
    snap = server.get('snapshot') or {}
    samples = snap.get('work_volumes', {})
    rejected = task.get('disk_exclusions', {})
    if rejected and task.get('disk_retry_server') != server['id']:
        return []
    options = []
    recovered = []
    for path in paths:
        sample = samples.get(path, {})
        free = sample.get('free_gb', snap.get('work_free_gb', 0) if len(paths)==1 else -1)
        if free <= 0 or free < server.get('min_disk_gb',5) or sample.get('error'):
            continue
        device = sample.get('device')
        if path in rejected or device and device in rejected.values():
            limit=task.get('disk_retry_thresholds',{}).get(path)
            if limit and snap.get('sampled_at',0)>limit['checked_at'] and free>=limit['free_gb']:
                recovered.append((path != preferred,-free,paths.index(path),path))
            continue
        options.append((path != preferred, -free, paths.index(path), path))
    return [row[-1] for row in sorted(options or recovered)]
