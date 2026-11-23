import json
import sqlite3
from contextlib import closing
from threading import RLock
from types import SimpleNamespace

import pytest

from scheduler import transfer_storage as journal
from scheduler.transfer_journal import TransferJournal
from scripts.compact_transfer_database import install, ready, stage


def legacy(db):
    db.execute('''CREATE TABLE transfer_files(job_id TEXT,server_id TEXT,path TEXT,status TEXT,
                action TEXT,initial_state TEXT,bytes INTEGER,total INTEGER,data TEXT,
                PRIMARY KEY(job_id,server_id,path))''')
    rows=[]
    for job in ('first','second'):
        for server in ('source','peer'):
            for path in ('AA','aa','样本'):
                digest='12'*32 if path!='样本' else '12'*8+'34'*24
                details={'path':path,'server_id':server,'size':10,'expected_sha256':digest,
                         'current_sha256':digest,'events':[{'status':'verified','time':10.5}]}
                if path=='aa':details.update(previous_sha256='ab'*32,current_sha256=None)
                if path=='AA':details.update(previous_sha256=digest)
                rows.append((job,server,path,'success','skip','matching',10,10,json.dumps(details)))
    db.executemany('INSERT INTO transfer_files VALUES (?,?,?,?,?,?,?,?,?)',rows)
    db.commit()
    return rows


def check_history(db, rows):
    assert [col[1] for col in db.execute('PRAGMA table_info(transfer_files)')]==journal.LEGACY_COLUMNS.split(',')
    assert db.execute('SELECT count(*) FROM transfer_hashes').fetchone()==(2,)
    assert db.execute('SELECT count(*) FROM transfer_paths').fetchone()==(3,)
    assert db.execute('SELECT count(*) FROM transfer_entries').fetchone()==(len(rows),)
    assert db.execute('SELECT sum(uses) FROM transfer_details').fetchone()==(len(rows),)
    assert db.execute('SELECT max(length(digest)) FROM transfer_hashes').fetchone()==(32,)
    for expected in rows:
        actual=db.execute('SELECT '+journal.LEGACY_COLUMNS+' FROM transfer_files WHERE job_id=? AND server_id=? AND path=?',expected[:3]).fetchone()
        assert actual[:-1]==expected[:-1]
        assert json.loads(actual[-1])==json.loads(expected[-1])


def test_old_database_roundtrip_and_reopen(tmp_path):
    database=tmp_path/'compute.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        rows=legacy(db)
        journal.initialize(db)
        check_history(db,rows)
    with closing(sqlite3.connect(database)) as db:
        journal.initialize(db)
        check_history(db,rows)


def test_corrupt_legacy_row_rolls_back_upgrade():
    with closing(sqlite3.connect(':memory:')) as db:
        legacy(db)
        db.execute("UPDATE transfer_files SET data='{}' WHERE rowid=2")
        db.commit()
        with pytest.raises((KeyError,ValueError)):
            journal.initialize(db)
        assert db.execute("SELECT type FROM sqlite_master WHERE name='transfer_files'").fetchone()==('table',)
        assert db.execute('SELECT count(*) FROM transfer_files').fetchone()==(12,)


def test_many_servers_share_hashes_and_preserve_paged_history():
    with closing(sqlite3.connect(':memory:')) as db:
        history=TransferJournal(SimpleNamespace(db=db,lock=RLock()))
        servers=[{'id':str(i),'name':str(i)} for i in range(3)]
        inventory={f'part-{i:04d}':{'size':16,'sha256':'34'*32} for i in range(4000)}
        job={'id':'transfer'}
        history.plan(job,servers,inventory,{},servers[0])
        assert db.execute('SELECT count(*) FROM transfer_entries').fetchone()==(0,)
        history.prepare(job,servers,inventory)
        history.prepare(job,servers,{'part-0000':{'size':1,'sha256':'ef'*32}})
        history.plan(job,servers,inventory,inventory,servers[0])
        assert db.execute('SELECT count(*) FROM transfer_hashes').fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM transfer_paths').fetchone()==(4000,)
        assert db.execute('SELECT count(*),sum(uses) FROM transfer_details').fetchone()==(3,12000)
        page=history.page('transfer',limit=2,offset=6001)
        assert page['total_count']==page['matched_count']==12000
        assert [(f['path'],f['server_id']) for f in page['files']]==[('part-2000','1'),('part-2000','2')]
        history.update(job,['1'],'part-2000','uploading',8)
        selected=history.page('transfer',query='PART-2000',server='1',status='uploading')
        assert selected['matched_count']==1 and selected['files'][0]['bytes']==8
        assert selected['files'][0]['previous_sha256']=='34'*32
        assert not db.execute('SELECT 1 FROM transfer_details WHERE uses=0').fetchone()


def test_online_stage_detects_changes_and_copies_latest_business_rows(tmp_path):
    source=tmp_path/'compute.sqlite3'
    output=tmp_path/'next.sqlite3'
    backup=tmp_path/'rollback.sqlite3'
    with closing(sqlite3.connect(source)) as db:
        rows=legacy(db)
        db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT)')
        db.execute("INSERT INTO events(seq,message) VALUES (100,'original')")
        db.commit()
    stage(source,output)
    with closing(sqlite3.connect(source)) as db:
        db.execute('UPDATE transfer_files SET bytes=8 WHERE rowid=1')
        db.commit()
    with pytest.raises(ValueError,match='发生变化'):
        ready(source,output)
    rows[0]=(*rows[0][:6],8,*rows[0][7:])
    output=tmp_path/'second.sqlite3'
    stage(source,output)
    with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(backup)) as rollback:
        db.backup(rollback)
        db.execute("INSERT INTO events(message) VALUES ('during conversion')")
        db.commit()
    install(source,output,backup)
    with closing(sqlite3.connect(source)) as db:
        check_history(db,rows)
        assert db.execute('SELECT * FROM events ORDER BY seq').fetchall()==[(100,'original'),(101,'during conversion')]
        db.execute("INSERT INTO events(message) VALUES ('after')")
        assert db.execute('SELECT max(seq) FROM events').fetchone()==(102,)
