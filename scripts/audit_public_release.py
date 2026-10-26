"""Audit a prepared source export. Reports paths/rule IDs, never matched secrets."""
import argparse
import hashlib
import json
import re
from pathlib import Path

FORBIDDEN_DIRS={'.git','.venv','__pycache__','.pytest_cache','data','data-demo','logs','backups',
                'objects','runs','datasets','workers','history','.ssh','node_modules'}
FORBIDDEN_SUFFIXES={'.sqlite','.sqlite3','.db','.log','.pem','.key','.p12','.pfx','.pt','.pth','.ckpt',
                    '.safetensors','.npz','.npy','.nii','.h5','.hdf5','.zip','.pyc'}
RULES={
    'private-key':re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    'github-token':re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'api-key':re.compile(rb'sk-[A-Za-z0-9_-]{30,}'),
    'absolute-user-home':re.compile(rb'[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+',re.I),
}


def audit(root,private_markers=()):
    root=Path(root).absolute().resolve();findings=[];files=[]
    for p in sorted(root.rglob('*')):
        if not p.is_file():continue
        relative=p.relative_to(root);path=relative.as_posix();raw=p.read_bytes()
        if p.is_symlink() or any(part in FORBIDDEN_DIRS for part in relative.parts) or p.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append({'path':path,'rule':'runtime-or-data-file'})
        if p.name.startswith('storage-location') or p.name in {'access-token.txt','session.key','known_hosts','id_rsa','id_ed25519','.env','controller.lock'}:
            findings.append({'path':path,'rule':'local-configuration-or-credential'})
        for rule,pattern in RULES.items():
            if pattern.search(raw):findings.append({'path':path,'rule':rule})
        if any(marker.encode('utf-8').lower() in raw.lower() for marker in private_markers):
            findings.append({'path':path,'rule':'private-marker'})
        files.append({'path':path,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                      'git_blob_sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()})
    return {'clean':not findings,'file_count':len(files),'bytes':sum(v['bytes'] for v in files),'findings':findings,'files':files}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root');parser.add_argument('--report');parser.add_argument('--private-markers',help='Local JSON array of strings; never include that file in the export')
    args=parser.parse_args();markers=json.loads(Path(args.private_markers).read_text(encoding='utf-8')) if args.private_markers else []
    report=audit(args.root,markers)
    if args.report:Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=False))
    raise SystemExit(0 if report['clean'] else 1)
