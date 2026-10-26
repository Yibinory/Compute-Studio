"""Small CPU-compatible training stand-in. Replace with your GPU experiment."""
import json
import os
from pathlib import Path

Path('outputs').mkdir(exist_ok=True)
value={'metric':0.91,'task':os.environ.get('TASK_ID'),'gpu':os.environ.get('CUDA_VISIBLE_DEVICES'),'datasets':json.loads(os.environ.get('DATASETS_JSON','{}'))}
Path('outputs/checkpoint.json').write_text(json.dumps(value),encoding='utf-8')
print('checkpoint saved:',value,flush=True)
