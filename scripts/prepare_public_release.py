"""Build an allowlisted, data-free source export without touching runtime data."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('app.py', 'requirements.txt', 'requirements-dev.txt', 'README.md',
              '.gitignore', '.gitattributes', 'CONTRIBUTING.md', 'THIRD_PARTY_NOTICES.md',
              'start_web.bat', 'start_demo.bat')
CODE_DIRS = {'scheduler': {'.py'}, 'templates': {'.html'}, 'tests': {'.py', '.js'},
             'examples': {'.py', '.sh', '.json'}}
STATIC_EXTENSIONS = {'.js', '.css', '.png', '.json', '.ttf', '.woff', '.woff2'}
TEXT_EXTENSIONS = {'.py','.js','.css','.html','.md','.json','.sh','.txt','.bat'}
GUIDE = 'docs/实验脚本与批量配置指导.md'


def build(output):
    output = Path(output)
    output = (ROOT / output if not output.is_absolute() else output).resolve()
    if ROOT not in output.parents or output == ROOT:
        raise ValueError('Export must be a new directory inside this checkout')
    if output.exists():raise ValueError('Export directory already exists; choose a new output path')
    selected = [ROOT / name for name in ROOT_FILES]
    for folder, extensions in CODE_DIRS.items():
        selected.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts)
    selected.extend(p for p in (ROOT / 'static').rglob('*') if p.is_file() and (p.suffix in STATIC_EXTENSIONS or p.name == 'LICENSE'))
    selected.extend([ROOT / GUIDE, ROOT / 'scripts/prepare_public_release.py', ROOT / 'scripts/audit_public_release.py'])
    selected.extend(p for p in (ROOT/'docs/images'/name for name in ('overview.png','experiments.png','results.png')) if p.is_file())
    output.mkdir(parents=True)
    for source in sorted(set(selected)):
        if source.is_symlink():raise ValueError('Linked source rejected: ' + str(source.relative_to(ROOT)))
        destination = output / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix in TEXT_EXTENSIONS or source.name in {'.gitignore','.gitattributes','LICENSE'}:
            text=source.read_text(encoding='utf-8-sig').replace('\r\n','\n').replace('\r','\n')
            destination.write_bytes(text.replace('\n','\r\n').encode('utf-8') if source.suffix=='.bat' else text.encode('utf-8'))
        else:shutil.copy2(source, destination)
    # Never let an inherited production storage environment redirect demo data.
    demo = output / 'start_demo.bat'
    demo.write_text(demo.read_text(encoding='utf-8').replace('app.py --demo --port 8777', 'app.py --demo --data-dir "%~dp0data-demo" --port 8777'), encoding='utf-8')
    rows = [{'path':p.relative_to(output).as_posix(), 'bytes':p.stat().st_size,
             'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(output.rglob('*')) if p.is_file()]
    return {'root':str(output), 'file_count':len(rows), 'bytes':sum(r['bytes'] for r in rows), 'files':rows}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT/'release/compute-studio'))
    args=parser.parse_args();manifest=build(args.output)
    target=Path(args.output).resolve().parent/'publication-manifest.json'
    target.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in manifest.items() if k!='files'},ensure_ascii=False))
