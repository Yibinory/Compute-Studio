def outputs(task):
    inputs = task.get('input_manifest', {})
    return [{'path':path, **value, 'change':'created' if path not in inputs else 'modified'}
            for path, value in sorted(task.get('result_manifest', {}).items())
            if inputs.get(path, {}).get('sha256') != value.get('sha256')]


def node(task, notes):
    result = {k:v for k,v in task.items() if k not in ('input_manifest','result_manifest','remote_state')}
    result['outputs'] = outputs(task)
    result['notes'] = [n for n in notes if n['task_id']==task['id']]
    return result
