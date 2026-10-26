"""Read every immediate predecessor from its namespaced dependency directory."""
import json
import os
from pathlib import Path

dependencies=json.loads(os.environ.get('DEPENDENCIES_JSON','{}'))
results={}
for task_id, directory in dependencies.items():
    results[task_id]=json.loads((Path(directory)/'outputs/checkpoint.json').read_text())
Path('evaluation.json').write_text(json.dumps(results),encoding='utf-8')
print('evaluated predecessors:',list(results),flush=True)
