from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def name(value):
    value = str(value).strip()
    if not re.fullmatch(r'[\w.-]{1,100}', value, re.UNICODE) or value in ('.', '..') or value.endswith('.'):
        raise ValueError('名称只能包含文字、数字、下划线、短横线和点（1–100 字符）')
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        raise ValueError('名称是系统保留字')
    return value


def relative(value):
    value = str(value)
    path = PurePosixPath(value)
    if not value or re.search(r'[\\<>:"|?*\x00-\x1f]',value) or path.is_absolute() or any(p in ('..', '') for p in path.parts):
        raise ValueError('文件路径必须是安全的相对路径')
    if any(p.endswith((' ', '.')) or p.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))} for p in path.parts):
        raise ValueError('文件路径包含系统保留名称')
    return path.as_posix()


def confined(root, rel):
    root = Path(root).resolve()
    target = root / relative(rel)
    if root not in target.resolve().parents:
        raise ValueError('路径越界')
    for ancestor in (target, *target.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError('不支持符号链接')
    return target


def digest(path, progress=None, check=None):
    h = hashlib.sha256()
    done = 0
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if check:check()
            h.update(block)
            done += len(block)
            if progress:
                progress(done)
    return h.hexdigest()


def manifest(root, progress=None, check=None):
    root = Path(root).resolve()
    result = {}
    if not root.exists():
        return result
    entries=[]
    for base, dirs, files in os.walk(root, followlinks=False):
        if check:check()
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '__pycache__', '.venv', 'node_modules'))
        for item in dirs + files:
            p = Path(base) / item
            confined(root,p.relative_to(root).as_posix())
            if p.is_symlink():
                raise ValueError('文件树含符号链接：' + str(p))
        for item in sorted(files):
            p = Path(base) / item
            rel = relative(p.relative_to(root).as_posix())
            entries.append((rel,p,p.stat().st_size))
    total=sum(item[2] for item in entries)
    done=0
    for index,(rel,p,length) in enumerate(entries):
        if progress:
            progress(done,total,rel,index,len(entries))
        result[rel]={'sha256':digest(p,lambda n:progress(done+n,total,rel,index,len(entries)) if progress else None,check=check),'size':length}
        done+=length
        if progress:
            progress(done,total,rel,index+1,len(entries))
    return result


def file_metadata(root, check=None):
    """List current input files without opening their contents or hashing them."""
    if Path(root).is_symlink():raise ValueError('不支持符号链接目录')
    root=Path(root).resolve()
    entries=[]
    if not root.exists():return entries
    for base,dirs,files in os.walk(root,followlinks=False):
        if check:check()
        dirs[:]=sorted(d for d in dirs if d not in ('.git','__pycache__','.venv','node_modules'))
        for item in dirs+files:
            path=Path(base)/item
            confined(root,path.relative_to(root).as_posix())
            if path.is_symlink():raise ValueError('文件树含符号链接：'+str(path))
        for item in files:
            if check:check()
            path=Path(base)/item
            if not path.is_file():continue
            value=path.stat()
            entries.append({'path':relative(path.relative_to(root).as_posix()),
                            'size':value.st_size,'modified_at':value.st_mtime})
    return sorted(entries,key=lambda e:(e['path'].casefold(),e['path']))


def copy_tree(source, target, check=None):
    # Final input hashing happens after assembly. Listing metadata here avoids
    # hashing the entire tree again before each copy.
    for item in file_metadata(source,check=check):
        if check:check()
        rel=item['path']
        dest = confined(target, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if check is None:
            shutil.copy2(Path(source)/rel,dest)
            continue
        handle=tempfile.NamedTemporaryFile(prefix='.copy-',dir=dest.parent,delete=False)
        temporary=Path(handle.name)
        try:
            with handle, (Path(source)/rel).open('rb') as stream:
                while True:
                    check();block=stream.read(1024*1024)
                    if not block:break
                    handle.write(block)
            check();shutil.copystat(Path(source)/rel,temporary);os.replace(temporary,dest)
        finally:temporary.unlink(missing_ok=True)


def extract_zip(archive, target, limit=50 * 1024**3, progress=None, hash_progress=None):
    """Validate the entire archive before writing; reject symlinks and collisions."""
    with zipfile.ZipFile(archive) as z:
        entries, total, seen = [], 0, set()
        if len(z.infolist()) > 200000:
            raise ValueError('ZIP 文件数量过多')
        for info in z.infolist():
            # ZipInfo.filename normalizes backslashes on Windows; validate the
            # unmodified archive name before any normalization takes place.
            rel = relative(info.orig_filename.rstrip('/'))
            key = rel.casefold()
            if key in seen:
                raise ValueError('ZIP 含重复或大小写冲突文件')
            seen.add(key)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError('ZIP 不允许符号链接')
            total += info.file_size
            if total > limit:
                raise ValueError('解压后文件超过限制')
            entries.append((info, confined(target, rel)))
        space=Path(target)
        while not space.exists():space=space.parent
        if shutil.disk_usage(space).free<total+64*1024**2:raise ValueError('本机保存目录空间不足以解压此文件')
        done=0
        if progress:
            progress(0,total,'',0,len(entries))
        for index,(info, dest) in enumerate(entries):
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as source, dest.open('wb') as out:
                    for block in iter(lambda:source.read(1024*1024),b''):
                        out.write(block);done+=len(block)
                        if progress:
                            progress(done,total,info.filename,index,len(entries))
            if progress:
                progress(done,total,info.filename,index+1,len(entries))
    return manifest(target,progress=hash_progress)
