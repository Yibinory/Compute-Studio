"""Child-process source scanning; never read the controller database here."""
import hashlib
import os
import stat
from pathlib import Path

from .content import matches
from .files import relative

IGNORE={'.git','.hg','.svn','.venv','venv','node_modules','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache'}
LOCK='.compute-sync.lock'


def stamp(path):
    value=path.stat()
    return [value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_ino,value.st_dev]


def linked(path):
    value=path.lstat()
    return path.is_symlink() or bool(getattr(value,'st_file_attributes',0)&0x400)


def scan(root,exclude,check,ignore=None):
    root=Path(root)
    ignore=IGNORE if ignore is None else ignore
    if not root.is_absolute() or not root.is_dir() or linked(root):raise ValueError('源码目录不存在或是目录链接')
    if (root/LOCK).exists():raise ValueError('源码正在批量修改：请完成修改后移除 .compute-sync.lock')
    result={};folded=set()
    for current,dirs,files in os.walk(root,followlinks=False):
        check()
        kept=[]
        for name in dirs:
            path=Path(current)/name;rel=path.relative_to(root).as_posix()
            if name in ignore or matches(rel,exclude) or matches(rel+'/',exclude):continue
            if linked(path):raise ValueError('源码目录不支持符号链接或目录联接：'+rel)
            kept.append(name)
        dirs[:]=sorted(kept)
        for name in sorted(files):
            path=Path(current)/name;rel=path.relative_to(root).as_posix()
            if name==LOCK or name.endswith(('.pyc','.pyo','.swp','.swo','~')) or matches(rel,exclude):continue
            relative(rel)
            if rel=='deps' or rel.startswith('deps/'):raise ValueError('deps 是运行依赖保留目录')
            if linked(path) or not stat.S_ISREG(path.stat().st_mode):raise ValueError('源码包含链接或特殊文件：'+rel)
            if rel.casefold() in folded:raise ValueError('源码文件大小写冲突：'+rel)
            folded.add(rel.casefold());result[rel]=stamp(path)
            if len(result)>100000:raise ValueError('源码文件超过 100000 个，请缩小目录或设置排除规则')
    if (root/LOCK).exists():raise ValueError('源码正在批量修改，请稍后同步')
    return result


def capture(spec,check,progress):
    source=Path(spec['source']);exclude=spec.get('exclude',[])
    before=scan(source,exclude,check)
    root_identity=[source.stat().st_dev,source.stat().st_ino]
    if not before and not spec.get('allow_empty'):raise ValueError('源码目录为空或全部被排除，已保留旧版本，未清空公共代码')
    if spec.get('expected') is not None and before!=spec['expected']:raise ValueError('源码仍在变化，等待稳定后重试')
    if spec.get('scan_only'):return {'stamps':before,'root_identity':root_identity}
    stage=Path(spec['stage']);stage.mkdir(parents=True,exist_ok=True)
    prior=spec.get('prior',{});files={};copied=0
    for index,(rel,value) in enumerate(before.items()):
        check();old=prior.get(rel,{})
        if old.get('source_stamp')==value:
            files[rel]={'sha256':old['sha256'],'size':old['size'],'source_stamp':value};continue
        source_file=source/rel;dest=stage/rel;dest.parent.mkdir(parents=True,exist_ok=True)
        digest=hashlib.sha256();length=0
        # Normalize shell inputs without modifying the editor's original file.
        if rel.lower().endswith('.sh'):
            if value[0]>2*1024**2:raise ValueError('Shell 脚本最多 2 MB：'+rel)
            content=source_file.read_bytes().decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n').encode('utf-8')
            if b'\x00' in content:raise ValueError('脚本含无效字符：'+rel)
            dest.write_bytes(content);digest.update(content);length=len(content)
        else:
            with source_file.open('rb') as reader,dest.open('xb') as writer:
                for block in iter(lambda:reader.read(1024**2),b''):
                    check();writer.write(block);digest.update(block);length+=len(block)
                    progress(copied+length,0,rel,index,len(before))
        if stamp(source_file)!=value:raise ValueError('复制期间源码被修改：'+rel)
        files[rel]={'sha256':digest.hexdigest(),'size':length,'source_stamp':value};copied+=length
    if scan(source,exclude,check)!=before or [source.stat().st_dev,source.stat().st_ino]!=root_identity:
        raise ValueError('同步期间源码目录变化，未发布不完整版本')
    return {'files':files,'stamps':before,'root_identity':root_identity,'copied_bytes':copied}
