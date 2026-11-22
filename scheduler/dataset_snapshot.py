"""Bounded parallel file verification shared by the controller and its SSH helper."""
import hashlib
import os
import stat
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def leaf(path, size, value):
    return hashlib.sha256(path.encode('utf-8') + b'\0' + str(size).encode('ascii')
                          + b'\0' + str(value).encode('ascii')).digest()


def fingerprint(leaves):
    leaves.sort()
    result = hashlib.sha256()
    for entry in leaves:
        result.update(entry)
    return result.hexdigest()


def expected(inventory):
    return fingerprint([leaf(path, item['size'], item['sha256']) for path, item in inventory.items()])


def scan(root, hashes=True, metadata=True, ignore_marker=None, progress=None, inventory_only=False):
    content, stamps = [], []
    inventory = {}
    count = total = 0

    def directory(relative):
        subdirs, records = [], []
        with os.scandir(os.path.join(root, relative)) as children:
            for child in children:
                path = relative + '/' + child.name if relative else child.name
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                    raise ValueError('文件树含链接：' + path)
                if stat.S_ISDIR(info.st_mode):
                    if child.name not in ('.git', '.venv', '__pycache__', 'node_modules'):
                        subdirs.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('文件树含特殊文件：' + path)
                if ignore_marker and path == '.compute-verify-' + ignore_marker:
                    with open(child.path,'rb') as stream:
                        if stream.read(33) == ignore_marker.encode('ascii'):
                            continue
                digest = None
                if hashes:
                    h = hashlib.sha256()
                    with open(child.path,'rb') as stream:
                        for block in iter(lambda:stream.read(1024**2),b''):
                            h.update(block)
                        after = os.fstat(stream.fileno())
                    if ((os.name != 'nt' and after.st_ino != info.st_ino)
                            or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns)):
                        raise ValueError('核验期间文件发生变化：' + path)
                    digest = h.hexdigest() if inventory_only else leaf(path, info.st_size, h.hexdigest())
                stamp = leaf(path, info.st_size, info.st_mtime_ns) if metadata else None
                records.append((path, info.st_size, digest, stamp))
        return subdirs, records

    directories, pending = [''], set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        while directories or pending:
            while directories and len(pending) < 32:
                pending.add(pool.submit(directory, directories.pop()))
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                children, files = future.result()
                directories.extend(children)
                count += len(files)
                for path, size, digest, stamp in files:
                    total += size
                    if inventory_only:inventory[path]={'size':size,'sha256':digest}
                    elif hashes:content.append(digest)
                    if metadata:stamps.append(stamp)
            if progress:progress(total,count)
    if inventory_only:return {'exists':True,'files':inventory,'skipped':[]}
    return {'count':count,'bytes':total,'sha256':fingerprint(content) if hashes else None,
            'metadata':fingerprint(stamps) if metadata else None}
