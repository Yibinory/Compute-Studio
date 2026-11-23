"""Lossless, shared dictionaries for the per-file transfer journal."""
import hashlib
import json
import re
from functools import lru_cache
from itertools import islice


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS transfer_paths (id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE)",
    "CREATE INDEX IF NOT EXISTS transfer_paths_order ON transfer_paths(path COLLATE NOCASE)",
    "CREATE TABLE IF NOT EXISTS transfer_hashes (id INTEGER PRIMARY KEY,digest BLOB NOT NULL)",
    "CREATE INDEX IF NOT EXISTS transfer_hash_prefix ON transfer_hashes(substr(digest,1,8))",
    "CREATE TABLE IF NOT EXISTS transfer_scopes (id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,server_id TEXT NOT NULL,UNIQUE(job_id,server_id))",
    "CREATE TABLE IF NOT EXISTS transfer_details (id INTEGER PRIMARY KEY,signature BLOB NOT NULL,body TEXT NOT NULL,uses INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS transfer_detail_signature ON transfer_details(signature)",
    """CREATE TABLE IF NOT EXISTS transfer_entries (
        scope INTEGER NOT NULL, file INTEGER NOT NULL, hash_id INTEGER NOT NULL, detail INTEGER NOT NULL,
        status TEXT NOT NULL, action TEXT NOT NULL, initial_state TEXT NOT NULL, bytes INTEGER NOT NULL,
        total INTEGER NOT NULL, PRIMARY KEY(scope,file)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS transfer_entry_status ON transfer_entries(scope,status)",
)
LEGACY_COLUMNS = 'job_id,server_id,path,status,action,initial_state,bytes,total,data'
DIGEST_SQL = "CASE WHEN typeof(h.digest)='blob' THEN lower(hex(h.digest)) ELSE h.digest END"
VIEW = f"""CREATE VIEW transfer_file_rows AS
    SELECT s.job_id,s.server_id,p.path,e.status,e.action,e.initial_state,e.bytes,e.total,
           json_patch(json_patch(
               json_set(d.body,'$.path',p.path,'$.server_id',s.server_id,'$.size',e.total,'$.expected_sha256',{DIGEST_SQL}),
               CASE WHEN json_type(d.body,'$.previous_sha256')='true'
                    THEN json_object('previous_sha256',{DIGEST_SQL}) ELSE '{{}}' END),
               CASE WHEN json_type(d.body,'$.current_sha256')='true'
                    THEN json_object('current_sha256',{DIGEST_SQL}) ELSE '{{}}' END) AS data,
           e.scope AS scope_key,e.file AS path_key
    FROM transfer_entries e JOIN transfer_scopes s ON s.id=e.scope
    JOIN transfer_paths p ON p.id=e.file JOIN transfer_hashes h ON h.id=e.hash_id
    JOIN transfer_details d ON d.id=e.detail"""


def split_detail(data, path, server, length):
    data = data.copy()
    if (data.pop('path'), data.pop('server_id'), data.pop('size')) != (path, server, length):
        raise ValueError('传输详情与索引不一致')
    expected = data.pop('expected_sha256')
    # Old fixtures and imports may contain noncanonical text. Preserve it verbatim.
    digest = bytes.fromhex(expected) if re.fullmatch('[0-9a-f]{64}',expected) else expected
    for key in ('previous_sha256', 'current_sha256'):
        if key in data and data[key] == expected:
            data[key] = True
    return digest, json.dumps(data,ensure_ascii=False,separators=(',',':'),sort_keys=True)


class _Dictionaries:
    def __init__(self, db):
        self.db = db

    def _intern(self, lookup, keys, insert, values):
        found = self.db.execute(lookup,keys).fetchone()
        return found[0] if found else self.db.execute(insert,values).lastrowid

    def path(self, path):
        return self._intern('SELECT id FROM transfer_paths WHERE path=?',(path,),
                            'INSERT INTO transfer_paths(path) VALUES (?)',(path,))

    def digest(self, digest):
        # The index stores only a prefix. Equality always uses the whole digest.
        return self._intern('SELECT id FROM transfer_hashes WHERE substr(digest,1,8)=? AND digest=?',
                            (digest[:8],digest),'INSERT INTO transfer_hashes(digest) VALUES (?)',(digest,))

    def detail(self, body):
        signature = hashlib.blake2b(body.encode(),digest_size=8).digest()
        return self._intern('SELECT id FROM transfer_details WHERE signature=? AND body=?',(signature,body),
                            'INSERT INTO transfer_details(signature,body) VALUES (?,?)',(signature,body))

    def scope(self, job, server):
        return self._intern('SELECT id FROM transfer_scopes WHERE job_id=? AND server_id=?',(job,server),
                            'INSERT INTO transfer_scopes(job_id,server_id) VALUES (?,?)',(job,server))

class JournalWriter:
    """Discard lookup caches when their transaction or migration ends."""
    def __init__(self, db, cache_size=1024):
        self.db = db
        dictionaries = _Dictionaries(db)
        self.path = lru_cache(cache_size)(dictionaries.path)
        self.digest = lru_cache(cache_size)(dictionaries.digest)
        self.detail = lru_cache(min(cache_size,8192))(dictionaries.detail)
        self.scope = lru_cache(128)(dictionaries.scope)

    def encode(self, row):
        job,server,path,status,action,initial,done,total,data = row
        digest,body = split_detail(data,path,server,total)
        return (self.scope(job,server),self.path(path),self.digest(digest),self.detail(body),
                status,action,initial,done,total)

    def prepare(self, rows):
        def new_entries():
            for row in rows:
                job,server,path = row[:3]
                keys = (self.scope(job,server),self.path(path))
                if not self.db.execute('SELECT 1 FROM transfer_entries WHERE scope=? AND file=?',keys).fetchone():
                    yield self.encode(row)
        self.db.executemany('INSERT INTO transfer_entries VALUES (?,?,?,?,?,?,?,?,?)',new_entries())

    def plan(self, rows):
        def updates():
            for job,server,path,status,action,initial,done,total,data in rows:
                keys = self.db.execute('SELECT e.scope,e.file FROM transfer_scopes s '
                                       'JOIN transfer_entries e ON e.scope=s.id JOIN transfer_paths p ON p.id=e.file '
                                       'WHERE s.job_id=? AND s.server_id=? AND p.path=?',(job,server,path)).fetchone()
                if keys:
                    digest,body = split_detail(data,path,server,total)
                    yield self.digest(digest),self.detail(body),status,action,initial,done,total,*keys
        self.db.executemany('UPDATE transfer_entries SET hash_id=?,detail=?,status=?,action=?,'
                            'initial_state=?,bytes=?,total=? WHERE scope=? AND file=?',updates())

    def update_state(self, rows):
        def updates():
            for scope,file,data,status,done in rows:
                _,body = split_detail(data,data['path'],data['server_id'],data['size'])
                yield self.detail(body),status,done,scope,file
        self.db.executemany('UPDATE transfer_entries SET detail=?,status=?,bytes=? '
                            'WHERE scope=? AND file=?',updates())


def copy_legacy(rows, db):
    writer = JournalWriter(db, cache_size=16384)
    count = 0
    while True:
        batch = list(islice(rows,4096))
        if not batch:
            return count
        packed = []
        for *columns, raw in batch:
            packed.append(writer.encode((*columns,json.loads(raw))))
        db.executemany('INSERT INTO transfer_entries VALUES (?,?,?,?,?,?,?,?,?)',packed)
        count += len(batch)


def track_detail_usage(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='transfer_use_insert'").fetchone():
        return
    if 'uses' not in {column[1] for column in db.execute('PRAGMA table_info(transfer_details)')}:
        db.execute('ALTER TABLE transfer_details ADD COLUMN uses INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE TEMP TABLE transfer_usage(id INTEGER PRIMARY KEY,n INTEGER)')
    db.execute('INSERT INTO transfer_usage SELECT detail,count(*) FROM transfer_entries GROUP BY detail')
    db.execute('UPDATE transfer_details SET uses=coalesce((SELECT n FROM transfer_usage WHERE id=transfer_details.id),0)')
    db.execute('DROP TABLE transfer_usage')
    db.execute('CREATE INDEX transfer_unused_details ON transfer_details(id) WHERE uses=0')
    db.execute('DELETE FROM transfer_details WHERE uses=0')
    db.execute('''CREATE TRIGGER transfer_use_insert AFTER INSERT ON transfer_entries BEGIN
        UPDATE transfer_details SET uses=uses+1 WHERE id=NEW.detail; END''')
    db.execute('''CREATE TRIGGER transfer_use_update AFTER UPDATE OF detail ON transfer_entries
        WHEN OLD.detail!=NEW.detail BEGIN
        UPDATE transfer_details SET uses=uses-1 WHERE id=OLD.detail;
        UPDATE transfer_details SET uses=uses+1 WHERE id=NEW.detail; END''')
    db.execute('''CREATE TRIGGER transfer_use_delete AFTER DELETE ON transfer_entries BEGIN
        UPDATE transfer_details SET uses=uses-1 WHERE id=OLD.detail; END''')


def initialize(db):
    old = db.execute("SELECT type FROM sqlite_master WHERE name='transfer_files'").fetchone()
    with db:
        # DDL must be part of the same transaction as the conversion.
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:
            db.execute(statement)
        if old == ('table',):
            copied = copy_legacy(db.execute('SELECT '+LEGACY_COLUMNS+' FROM transfer_files'),db)
            if db.execute('SELECT count(*) FROM transfer_entries').fetchone()[0] != copied:
                raise ValueError('迁移后传输明细数量不一致')
            db.execute('DROP TABLE transfer_files')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='transfer_file_rows'").fetchone():
            db.execute('DROP VIEW IF EXISTS transfer_files')
            db.execute(VIEW)
        db.execute('CREATE VIEW IF NOT EXISTS transfer_files AS SELECT '+LEGACY_COLUMNS+' FROM transfer_file_rows')
        track_detail_usage(db)
