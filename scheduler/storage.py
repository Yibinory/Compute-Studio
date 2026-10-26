from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import logging
from pathlib import Path


def compact_result(value,depth=0):
    """Bound polling metadata; full job results stay in records for details."""
    if isinstance(value,str):return value if len(value)<=2000 else value[:2000]+'…'
    if isinstance(value,list):return [compact_result(v,depth+1) for v in value[:10]] if depth<3 else []
    if not isinstance(value,dict):return value
    result={}
    excluded={'manifest','input_manifest','result_manifest','remote_state','files','tasks','entries','source_entries','fingerprints'}
    for key,item in value.items():
        if key in excluded:
            if isinstance(item,(dict,list)):result[{'files':'file_count','tasks':'task_count'}.get(key,key+'_count')]=len(item)
            continue
        if isinstance(item,list):
            result[{'results':'result_count','differences':'difference_count'}.get(key,key+'_count')]=len(item)
            result[key]=[compact_result(v,depth+1) for v in item[:(20 if key=='changes' else 10)]] if depth<3 else []
        elif isinstance(item,dict):result[key]=compact_result(item,depth+1) if depth<3 else {}
        else:result[key]=compact_result(item,depth+1)
    if len(json.dumps(result,ensure_ascii=False).encode('utf-8'))>32768:
        result={k:v for k,v in result.items() if not isinstance(v,(dict,list))}
        result['details_available']=True
    return result


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.root / 'compute.sqlite3'), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, level TEXT, message TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS record_summaries (kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))')
        for kind,key,raw in self.db.execute("SELECT kind,id,data FROM records WHERE kind IN ('datasets','jobs','replicas','notes','tasks','dataset_notes') AND NOT EXISTS (SELECT 1 FROM record_summaries s WHERE s.kind=records.kind AND s.id=records.id)").fetchall():
            self.db.execute('INSERT INTO record_summaries VALUES (?,?,?)',(kind,key,json.dumps(self.summary(kind,json.loads(raw)),ensure_ascii=False)))
        self.db.execute('CREATE TABLE IF NOT EXISTS summary_versions (kind TEXT PRIMARY KEY,version INTEGER)')
        # Upgrade derived caches once, without rewriting the original records.
        # Subsequent computer restarts must not reparse all historical results.
        for kind in ('jobs','replicas'):
            version=self.db.execute('SELECT version FROM summary_versions WHERE kind=?',(kind,)).fetchone()
            if version and version[0]==2:
                # A still-running older controller may have written one old
                # summary after an online cache upgrade. Repair only those
                # rows, rather than loading every historical result again.
                cursor=self.db.execute("SELECT r.id,r.data FROM records r JOIN record_summaries s ON r.kind=s.kind AND r.id=s.id WHERE r.kind=? AND json_extract(s.data,'$._summary_version') IS NOT 2",(kind,))
            else:cursor=self.db.execute('SELECT id,data FROM records WHERE kind=?',(kind,))
            while True:
                batch=cursor.fetchmany(32)
                if not batch:break
                self.db.executemany('INSERT INTO record_summaries VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',
                    [(kind,key,json.dumps(self.summary(kind,json.loads(raw)),ensure_ascii=False)) for key,raw in batch])
            self.db.execute('INSERT INTO summary_versions VALUES (?,2) ON CONFLICT(kind) DO UPDATE SET version=excluded.version',(kind,))
        self.db.execute("CREATE INDEX IF NOT EXISTS summary_status_idx ON record_summaries(kind,json_extract(data,'$.status'))")
        self.db.commit()
        self.log=logging.getLogger('compute.store.'+uuid.uuid4().hex)
        self.log.setLevel(logging.INFO)
        self.log.propagate=False
        self.configure_log()

    def configure_log(self):
        for handler in self.log.handlers[:]:
            handler.close();self.log.removeHandler(handler)
        from logging.handlers import RotatingFileHandler
        folder=self.root/'logs';folder.mkdir(exist_ok=True)
        handler=RotatingFileHandler(folder/'events.log',maxBytes=10*1024**2,backupCount=5,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        self.log.addHandler(handler)

    def relocate(self,root):
        with self.lock:
            self.db.close()
            self.root=Path(root).resolve()
            self.db=sqlite3.connect(str(self.root/'compute.sqlite3'),check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            self.configure_log()

    def close(self):
        with self.lock:
            self.db.close()
            for handler in self.log.handlers[:]:
                handler.close();self.log.removeHandler(handler)

    def put(self, kind, value):
        raw=json.dumps(value,ensure_ascii=False)
        summary=json.dumps(self.summary(kind,value),ensure_ascii=False) if kind in ('datasets','jobs','replicas','notes','tasks','dataset_notes') else None
        with self.lock, self.db:
            self.db.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data', (kind, value['id'], raw))
            if summary is not None:
                self.db.execute('INSERT INTO record_summaries VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',(kind,value['id'],summary))
        return value

    @staticmethod
    def summary(kind,value):
        result=dict(value)
        if kind in ('jobs','replicas'):result['_summary_version']=2
        if kind=='tasks':
            from scheduler.graph import outputs
            inventory=outputs(value)
            result.update(output_count=len(inventory),outputs=inventory[:3])
            for field in ('input_manifest','result_manifest','remote_state'):result.pop(field,None)
            return result
        if kind=='dataset_notes':
            from scheduler.dataset_notes import compact
            return compact(value)
        if kind=='notes':
            from scheduler.notes import public_note
            return public_note(value)
        elif kind=='datasets':
            inventory=result.pop('manifest',{})
            result.update(file_count=len(inventory),total_bytes=sum(v['size'] for v in inventory.values()))
        elif kind=='replicas':
            result['file_count']=len(result.pop('files',{}))
            differences=result.get('differences',[])
            result.update(difference_count=len(differences),differences=differences[:3])
            if isinstance(result.get('skipped'),list):result.update(skipped_count=len(result['skipped']),skipped=result['skipped'][:3])
        elif kind=='jobs' and isinstance(result.get('result'),dict):
            result['result']=compact_result(result['result'])
        return result

    def summaries(self,kind):
        with self.lock:
            rows=self.db.execute('SELECT data FROM record_summaries WHERE kind=? ORDER BY rowid',(kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def active_summaries(self,kind,statuses=('queued','running')):
        if not statuses:return []
        with self.lock:
            rows=self.db.execute("SELECT data FROM record_summaries WHERE kind=? AND json_extract(data,'$.status') IN ("+','.join('?' for _ in statuses)+') ORDER BY rowid',(kind,*statuses)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recent_summaries(self,kind,limit=50,active_statuses=()):
        with self.lock:
            rows=self.db.execute('SELECT rowid,data FROM record_summaries WHERE kind=? ORDER BY rowid DESC LIMIT ?',(kind,limit)).fetchall()
            if active_statuses:
                rows+=self.db.execute("SELECT rowid,data FROM record_summaries WHERE kind=? AND json_extract(data,'$.status') IN ("+','.join('?' for _ in active_statuses)+')',(kind,*active_statuses)).fetchall()
        return [json.loads(raw) for _,raw in sorted(dict(rows).items())]

    def get_summary(self,kind,key):
        with self.lock:
            row=self.db.execute('SELECT data FROM record_summaries WHERE kind=? AND id=?',(kind,key)).fetchone()
        if not row:raise ValueError('记录不存在')
        return json.loads(row[0])

    def put_many(self,records):
        rows=[(kind,value['id'],json.dumps(value,ensure_ascii=False)) for kind,value in records]
        summaries=[(kind,value['id'],json.dumps(self.summary(kind,value),ensure_ascii=False)) for kind,value in records if kind in ('datasets','jobs','replicas','notes','tasks','dataset_notes')]
        with self.lock,self.db:
            self.db.executemany('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',rows)
            self.db.executemany('INSERT INTO record_summaries VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',summaries)

    def get(self, kind, key):
        with self.lock:
            row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        if not row:
            raise ValueError('记录不存在')
        return json.loads(row[0])

    def delete(self, kind, key):
        with self.lock, self.db:
            self.db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, key))
            self.db.execute('DELETE FROM record_summaries WHERE kind=? AND id=?',(kind,key))

    def all(self, kind):
        with self.lock:
            rows = self.db.execute('SELECT data FROM records WHERE kind=? ORDER BY rowid', (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def event(self, message, level='info'):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(time,level,message) VALUES (?,?,?)', (time.time(), level, str(message)[:4000]))
            getattr(self.log,'warning' if level=='warning' else 'error' if level=='error' else 'info')(str(message)[:4000])

    def events(self):
        with self.lock:
            rows = self.db.execute('SELECT seq,time,level,message FROM events ORDER BY seq DESC LIMIT 120').fetchall()
        return [dict(zip(('seq', 'time', 'level', 'message'), r)) for r in rows]
