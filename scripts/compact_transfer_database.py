"""Stage a compact transfer journal online, then install it under the stopped controller's root lock."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scheduler import transfer_storage as journal
from scheduler.local_storage import root_lock


GUARD = 'transfer_compaction_guard'
EVENTS = ('INSERT','UPDATE','DELETE')
TRIGGERS = tuple('transfer_compaction_'+op.lower() for op in EVENTS)


def remove_guard(db):
    for name in TRIGGERS:
        db.execute('DROP TRIGGER IF EXISTS '+name)
    db.execute('DROP TABLE IF EXISTS '+GUARD)


def verify_guard(db, token):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    row = db.execute('SELECT token,changed FROM '+GUARD).fetchone()
    if not set(TRIGGERS)<=names or row != (token,0):
        raise ValueError('传输明细在准备期间发生变化，请重新生成迁移副本')


def stage(source, output):
    if not source.is_file() or output.exists():
        raise ValueError('需要已有的控制器数据库和未使用的输出路径')
    token=uuid.uuid4().hex
    with closing(sqlite3.connect(source.as_uri()+'?mode=rw',uri=True,timeout=20)) as watch:
        with watch:
            watch.execute('BEGIN IMMEDIATE')
            if watch.execute("SELECT type FROM sqlite_master WHERE name='transfer_files'").fetchone()!=('table',):
                raise ValueError('此数据库没有待转换的旧传输表')
            remove_guard(watch)
            watch.execute('CREATE TABLE '+GUARD+'(token TEXT,changed INTEGER)')
            watch.execute('INSERT INTO '+GUARD+' VALUES (?,0)',(token,))
            for name,op in zip(TRIGGERS,EVENTS):
                watch.execute(f'CREATE TRIGGER {name} AFTER {op} ON transfer_files WHEN (SELECT changed FROM {GUARD})=0 BEGIN UPDATE {GUARD} SET changed=1; END')
        try:
            with output.open('xb'):
                pass
            with closing(sqlite3.connect(output)) as dst:
                # Only the unpublished, reproducible staging file uses a memory journal.
                dst.execute('PRAGMA journal_mode=MEMORY')
                dst.execute('PRAGMA cache_size=-262144')
                journal.initialize(dst)
                last=0
                count=0
                samples=0
                writer=journal.JournalWriter(dst,cache_size=32768)
                next_report=time.monotonic()+30
                while True:
                    rows=watch.execute('SELECT rowid,'+journal.LEGACY_COLUMNS+
                                       ' FROM transfer_files WHERE rowid>? ORDER BY rowid LIMIT 4096',(last,)).fetchall()
                    if not rows:break
                    if time.monotonic()>=next_report:
                        verify_guard(watch,token)
                        print(f'{count:,} records converted',flush=True)
                        next_report=time.monotonic()+30
                    with dst:
                        packed=[writer.encode((*r[1:-1],json.loads(r[-1]))) for r in rows]
                        dst.executemany('INSERT INTO transfer_entries VALUES (?,?,?,?,?,?,?,?,?)',packed)
                    for r in (rows[0],rows[-1]):
                        saved=dst.execute('SELECT '+journal.LEGACY_COLUMNS+' FROM transfer_files WHERE job_id=? AND server_id=? AND path=?',r[1:4]).fetchone()
                        if saved is None or saved[:-1]!=r[1:-1] or json.loads(saved[-1])!=json.loads(r[-1]):
                            raise ValueError('历史明细逐字段抽查不一致')
                        samples+=1
                    count+=len(rows)
                    last=rows[-1][0]
                verify_guard(watch,token)
                old=watch.execute('SELECT job_id,server_id,status,count(*) FROM transfer_files GROUP BY job_id,server_id,status').fetchall()
                new=dst.execute('SELECT s.job_id,s.server_id,e.status,count(*) FROM transfer_scopes s JOIN transfer_entries e ON s.id=e.scope GROUP BY s.id,e.status').fetchall()
                if sorted(old)!=sorted(new) or count!=sum(x[3] for x in new):
                    raise ValueError('作业、服务器或状态计数不一致')
                if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
                    raise ValueError('迁移副本完整性检查未通过')
                with dst:
                    dst.execute('CREATE TABLE transfer_compaction_receipt(token TEXT,rows INTEGER)')
                    dst.execute('INSERT INTO transfer_compaction_receipt VALUES (?,?)',(token,count))
                print(json.dumps(dict(rows=count,compared_samples=samples,groups=len(new),bytes=output.stat().st_size)),flush=True)
        except BaseException:
            with watch:
                watch.execute('BEGIN IMMEDIATE')
                if watch.execute('SELECT token FROM '+GUARD).fetchone()==(token,):
                    remove_guard(watch)
            raise


def ident(name):
    return '"'+name.replace('"','""')+'"'


def ready(source, output):
    with closing(sqlite3.connect(output.as_uri()+'?mode=rw',uri=True)) as dst, \
         closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as live:
        token,count=dst.execute('SELECT token,rows FROM transfer_compaction_receipt').fetchone()
        verify_guard(live,token)
        journal.initialize(dst)
        for (scope,) in dst.execute('SELECT id FROM transfer_scopes'):
            for direction in ('ASC','DESC'):
                rows=dst.execute('SELECT '+journal.LEGACY_COLUMNS+' FROM transfer_file_rows '
                                 f'WHERE scope_key=? ORDER BY path_key {direction} LIMIT 2',(scope,))
                for row in rows:
                    old=live.execute('SELECT '+journal.LEGACY_COLUMNS+' FROM transfer_files '
                                     'WHERE job_id=? AND server_id=? AND path=?',row[:3]).fetchone()
                    if not old or row[:-1]!=old[:-1] or json.loads(row[-1])!=json.loads(old[-1]):
                        raise ValueError('切换前兼容视图抽查不一致')
        verify_guard(live,token)
    return token,count


def check_activity(db):
    # Demo or isolated transfer-journal databases may have no controller state.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone():
        return
    from scheduler.uploads import BUSY
    blocked = {'jobs': {'queued','running'}, 'uploads': BUSY,
               'tasks': {'staging','launching','syncing','sync_failed','canceling','lost'}}
    for kind, statuses in blocked.items():
        params = ','.join('?' for _ in statuses)
        row = db.execute(f"SELECT id,json_extract(data,'$.status') FROM records WHERE kind=? "
                         f"AND json_extract(data,'$.status') IN ({params}) LIMIT 1",(kind,*statuses)).fetchone()
        if row:
            raise ValueError(f'切换需等待正在处理的 {kind} 作业 {row[0]}（{row[1]}）')


def verify_backup(backup, token):
    if not backup or not backup.is_file():
        raise ValueError('需要已经核验的完整恢复备份')
    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro&immutable=1',uri=True)) as rollback:
        verify_guard(rollback,token)


def install(source, output, backup):
    if not output.is_file() or not source.is_file() or not backup or not backup.is_file() or backup in (source,output):
        raise ValueError('安装需要已核验的恢复备份、迁移副本和原数据库')
    if output.stat().st_dev!=source.stat().st_dev:
        raise ValueError('原库与迁移副本须位于同一文件系统')
    token,count=ready(source,output)
    verify_backup(backup,token)
    with root_lock(source.parent), closing(sqlite3.connect(source.as_uri()+'?mode=rw',uri=True,timeout=5)) as live:
        if live.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise ValueError('数据库仍有读写连接，暂缓切换')
        live.execute('BEGIN IMMEDIATE')
        check_activity(live)
        with closing(sqlite3.connect(output.as_uri()+'?mode=rw',uri=True)) as dst:
            verify_guard(live,token)
            dst.execute('ATTACH DATABASE ? AS original',(source.as_uri()+'?mode=ro',))
            skip={'transfer_files','transfer_compaction_guard','sqlite_sequence'}
            objects=dst.execute("SELECT type,name,tbl_name,sql FROM original.sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
            with dst:
                dst.execute('BEGIN')
                for pragma in ('user_version','application_id'):
                    value=dst.execute('PRAGMA original.'+pragma).fetchone()[0]
                    dst.execute(f'PRAGMA main.{pragma}={int(value)}')
                for kind,name,owner,sql in objects:
                    if kind!='table' or name in skip or name.startswith('sqlite_'):continue
                    dst.execute('DROP TABLE IF EXISTS main.'+ident(name))
                    dst.execute(sql)
                    if 'WITHOUT ROWID' in sql.upper():
                        dst.execute(f'INSERT INTO main.{ident(name)} SELECT * FROM original.{ident(name)}')
                    else:
                        copy_rowids(dst,name)
                    original_count=dst.execute('SELECT count(*) FROM original.'+ident(name)).fetchone()[0]
                    copied_count=dst.execute('SELECT count(*) FROM main.'+ident(name)).fetchone()[0]
                    if copied_count!=original_count:
                        raise ValueError('业务表复制数量不一致：'+name)
                if any(name=='sqlite_sequence' for _,name,_,_ in objects):
                    dst.execute('DELETE FROM main.sqlite_sequence')
                    dst.execute('INSERT INTO main.sqlite_sequence SELECT * FROM original.sqlite_sequence')
                for kind,name,owner,sql in objects:
                    if kind in ('index','trigger','view') and owner not in skip and name not in TRIGGERS:
                        if kind=='view':
                            dst.execute('DROP VIEW IF EXISTS main.'+ident(name))
                        dst.execute(sql)
            dst.execute('DETACH DATABASE original')
            if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
                raise ValueError('切换前完整性检查未通过')
        verify_guard(live,token)
        live.rollback()
        live.close()
        for dbpath in (source,output):
            if any(Path(str(dbpath)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
                raise ValueError('数据库存在活动日志，暂缓切换')
        before=source.stat().st_size
        os.replace(output,source)
        print(json.dumps(dict(rows=count,before_bytes=before,after_bytes=source.stat().st_size)))


def copy_rowids(db,name):
    cols=','.join(ident(row[1]) for row in db.execute('PRAGMA original.table_info('+ident(name)+')'))
    db.execute(f'INSERT INTO main.{ident(name)}(rowid,{cols}) SELECT rowid,{cols} FROM original.{ident(name)}')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('stage','ready','install'))
    p.add_argument('--source',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--backup',type=Path)
    args=p.parse_args()
    source,output=args.source.resolve(),args.output.resolve()
    if args.action=='stage':stage(source,output)
    elif args.action=='ready':
        token,count=ready(source,output)
        if args.backup:
            verify_backup(args.backup.resolve(),token)
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as live:
            check_activity(live)
        print('Ready:',count,'rows')
    else:install(source,output,args.backup.resolve() if args.backup else None)
