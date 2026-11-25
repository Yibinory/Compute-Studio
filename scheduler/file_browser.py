"""Page filesystem and manifest entries one directory at a time."""
import os
from pathlib import Path

from .files import EXCLUDED_DIRS, confined, relative


def browse_directory(value):
    return relative(value) if value else ''


def directory_page(rows, directory, query, offset, limit):
    """Rows are (path, metadata) pairs. Search is recursive within the directory."""
    prefix = directory + '/' if directory else ''
    query = query.strip().casefold()
    folders, files = set(), []
    for path, metadata in rows:
        if not path.startswith(prefix):
            continue
        tail = path[len(prefix):]
        if query:
            if query in path.casefold():
                files.append((path, metadata))
        elif '/' in tail:
            folders.add(prefix + tail.split('/', 1)[0])
        else:
            files.append((path, metadata))
    return pack_page(folders, files, directory, offset, limit)


def pack_page(folders, files, directory, offset, limit):
    folders = sorted(folders, key=lambda path: (path.casefold(), path))
    files.sort(key=lambda item: (item[0].casefold(), item[0]))
    file_offset = max(0, offset - len(folders))
    visible_folders = folders[offset:offset + limit]
    visible_files = files[file_offset:file_offset + limit - len(visible_folders)]
    return {'directory': directory, 'folders': visible_folders,
            'files': [{**metadata, 'path': path} for path, metadata in visible_files],
            'entry_count': len(folders) + len(files), 'offset': offset, 'limit': limit}


def disk_page(root, directory, offset, limit):
    """Read only immediate children and stat only the selected files."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError('不支持符号链接目录')
    root = root.resolve()
    folder = confined(root, directory) if directory else root
    if not folder.is_dir():
        return pack_page([], [], directory, offset, limit)
    folders, files = [], []
    for part in Path(directory).parts:
        if part in EXCLUDED_DIRS:
            return pack_page([], [], directory, offset, limit)
    with os.scandir(folder) as entries:
        for item in entries:
            if item.is_symlink():
                continue
            path = relative(directory+'/'+item.name if directory else item.name)
            if item.is_dir(follow_symlinks=False) and item.name not in EXCLUDED_DIRS:
                folders.append(path)
            elif item.is_file(follow_symlinks=False):
                files.append((path, {}))
    page = pack_page(folders, files, directory, offset, limit)
    for entry in page['files']:
        value = confined(root, entry['path']).stat()
        entry.update(size=value.st_size, modified_at=value.st_mtime)
    return page


def sql_directory_page(db, entries, args, directory, query, offset, limit):
    """Group paths before paginating. The input SELECT exposes path, key1 and key2."""
    if query:
        source = 'SELECT path, key1, key2, 0 AS folder FROM entries'
    else:
        start = len(directory) + 2 if directory else 1
        source = f'''SELECT DISTINCT
                     CASE WHEN slash=0 THEN path ELSE substr(path,1,{start}-2+slash) END AS path,
                     CASE WHEN slash=0 THEN key1 END AS key1,
                     CASE WHEN slash=0 THEN key2 END AS key2, slash>0 AS folder
                     FROM (SELECT *,instr(substr(path,{start}),'/') AS slash FROM entries)'''
    cte = 'WITH entries AS (' + entries + '), children AS (' + source + ')'
    rows = db.execute(cte + ''' SELECT path,key1,key2,folder,count(*) OVER () FROM children
                                ORDER BY folder DESC,path COLLATE NOCASE,path,key2,key1
                                LIMIT ? OFFSET ?''', [*args, limit, offset]).fetchall()
    count = rows[0][-1] if rows else 0
    if offset and not rows:
        count = db.execute(cte + ' SELECT count(*) FROM children', args).fetchone()[0]
    return [path for path, _, _, folder, _ in rows if folder], [
        (one, two) for _, one, two, folder, _ in rows if not folder], count
