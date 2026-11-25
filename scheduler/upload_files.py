"""Upload receipts live in indexed rows, never in repeatedly rewritten JSON."""
import json
from .file_browser import sql_directory_page


class UploadFiles:
    def __init__(self,store):
        self.store=store
        with store.lock,store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS upload_files (upload_id TEXT, idx INTEGER, path TEXT, search TEXT, size INTEGER, received INTEGER, data TEXT, PRIMARY KEY(upload_id,idx))')
        for upload in store.all('uploads'):
            if 'files' in upload:
                files=upload.pop('files');self.replace(upload['id'],files)
                upload.update(file_count=len(files),files_done=sum(f.get('received',0)==f['size'] for f in files));store.put('uploads',upload)

    def replace(self,key,files):
        rows=[(key,i,f['path'],f['path'].casefold(),f['size'],f.get('received',0),json.dumps(f,ensure_ascii=False)) for i,f in enumerate(files)]
        with self.store.lock,self.store.db:
            self.store.db.execute('DELETE FROM upload_files WHERE upload_id=?',(key,))
        for offset in range(0,len(rows),1000):
            with self.store.lock,self.store.db:
                self.store.db.executemany('INSERT INTO upload_files VALUES (?,?,?,?,?,?,?)',rows[offset:offset+1000])

    def get(self,key,index):
        with self.store.lock:
            row=self.store.db.execute('SELECT data,received FROM upload_files WHERE upload_id=? AND idx=?',(key,index)).fetchone()
        if not row:raise ValueError('上传文件不存在')
        return {**json.loads(row[0]),'received':row[1]}

    def all(self,key):
        with self.store.lock:
            rows=self.store.db.execute('SELECT data,received FROM upload_files WHERE upload_id=? ORDER BY idx',(key,)).fetchall()
        return [{**json.loads(raw),'received':done} for raw,done in rows]

    def page(self,key,offset=0,limit=100,query=''):
        query=query.casefold()
        with self.store.lock:
            count=self.store.db.execute('SELECT count(*) FROM upload_files WHERE upload_id=? AND instr(search,?)>0',(key,query)).fetchone()[0]
            rows=self.store.db.execute('SELECT idx,data,received FROM upload_files WHERE upload_id=? AND instr(search,?)>0 ORDER BY idx LIMIT ? OFFSET ?',(key,query,limit,offset)).fetchall()
        return [{**json.loads(raw),'index':idx,'received':done} for idx,raw,done in rows],count

    def browse(self, key, offset, limit, query, directory):
        entries = 'SELECT path,idx AS key1,NULL AS key2 FROM upload_files WHERE upload_id=?'
        args = [key]
        if directory:
            entries += ' AND path>=? AND path<?'
            args.extend((directory+'/', directory+'0'))
        if query:
            entries += ' AND instr(search,?)>0'
            args.append(query.casefold())
        with self.store.read_snapshot() as db:
            folders, keys, total = sql_directory_page(db, entries, args, directory, query, offset, limit)
            indices = [idx for idx, _ in keys]
            by_index = {}
            if indices:
                placeholders = ','.join('?' for _ in indices)
                rows = db.execute(
                    f'SELECT idx,data,received FROM upload_files WHERE upload_id=? AND idx IN ({placeholders})',
                    [key, *indices])
                by_index = {idx: {**json.loads(raw),'index':idx,'received':done} for idx,raw,done in rows}
        return {'folders': folders, 'files': [by_index[idx] for idx in indices], 'entry_count':total}
