"""Paginated, durable file decisions; never put large inventories in /state."""
import json
import time


class TransferJournal:
    def __init__(self,store):
        self.store=store
        with store.lock,store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS transfer_files (job_id TEXT, server_id TEXT, path TEXT, status TEXT, action TEXT, initial_state TEXT, bytes INTEGER, total INTEGER, data TEXT, PRIMARY KEY(job_id,server_id,path))')
            store.db.execute('CREATE INDEX IF NOT EXISTS transfer_files_status ON transfer_files(job_id,server_id,status)')

    def write_batches(self,sql,rows):
        for offset in range(0,len(rows),1000):
            with self.store.lock,self.store.db:
                self.store.db.executemany(sql,rows[offset:offset+1000])

    def prepare(self,job,servers,inventory,action='pending'):
        if not job:return
        now=time.time()
        rows=[]
        for server in servers:
            for path,item in inventory.items():
                data={'path':path,'server_id':server['id'],'server_name':server['name'],'expected_sha256':item['sha256'],'size':item['size'],'created_at':now,'events':[]}
                rows.append((job['id'],server['id'],path,'queued',action,'unchecked',0,item['size'],json.dumps(data,ensure_ascii=False)))
        self.write_batches('INSERT OR IGNORE INTO transfer_files VALUES (?,?,?,?,?,?,?,?,?)',rows)

    def plan(self,job,group,inventory,before,source):
        if not job:return
        now=time.time();rows=[]
        for server in group:
            for path,item in inventory.items():
                remote=before.get(path)
                initial='missing' if remote is None else 'matching' if remote['sha256']==item['sha256'] else 'mismatch'
                action={'missing':'upload','matching':'skip','mismatch':'replace'}[initial]
                data={'path':path,'server_id':server['id'],'server_name':server['name'],'size':item['size'],
                      'expected_sha256':item['sha256'],'previous_sha256':remote.get('sha256') if remote else None,
                      'source_server_id':source['id'],'source_server_name':source['name'],'shared':server['id']!=source['id'],
                      'created_at':now,'updated_at':now,'events':[{'time':now,'status':'planned','message':action}]}
                rows.append(('queued',action,initial,0,item['size'],json.dumps(data,ensure_ascii=False),job['id'],server['id'],path))
        self.write_batches('UPDATE transfer_files SET status=?,action=?,initial_state=?,bytes=?,total=?,data=? WHERE job_id=? AND server_id=? AND path=?',rows)

    def update(self,job,server_ids,path,status,done=None,error=None):
        if not job:return
        now=time.time();updates=[]
        with self.store.lock,self.store.db:
            for sid in server_ids:
                row=self.store.db.execute('SELECT status,bytes,total,data FROM transfer_files WHERE job_id=? AND server_id=? AND path=?',(job['id'],sid,path)).fetchone()
                if not row:continue
                previous,count,total,data=row;data=json.loads(data)
                if previous!=status:
                    data['events'].append({'time':now,'status':status})
                    if status=='uploading':data.setdefault('started_at',now)
                    if status in ('success','skipped','failed','interrupted'):data['finished_at']=now
                data['updated_at']=now
                if error:data['error']=str(error)
                updates.append((status,min(total,max(0,int(done))) if done is not None else count,json.dumps(data,ensure_ascii=False),job['id'],sid,path))
            self.store.db.executemany('UPDATE transfer_files SET status=?,bytes=?,data=? WHERE job_id=? AND server_id=? AND path=?',updates)

    def verify(self,job,sid,inventory,method='sha256',source=None):
        if not job:return
        now=time.time();updates=[]
        with self.store.lock,self.store.db:
            rows=self.store.db.execute('SELECT path,action,data FROM transfer_files WHERE job_id=? AND server_id=?',(job['id'],sid)).fetchall()
            for path,action,raw in rows:
                data=json.loads(raw);actual=inventory.get(path,{}).get('sha256')
                data.update(current_sha256=actual,current_state='missing' if actual is None else 'matching' if actual==data['expected_sha256'] else 'mismatch',verified_at=now,verification_method=method,verification_source=source)
                if source:data.update(source_server_id=source,source_server_name=self.store.get('servers',source)['name'],shared=source!=sid)
                if data['current_state']=='matching':data['events'].append({'time':now,'status':'verified'})
                if data['current_state']!='matching':
                    data['error']='服务器最终 hash 核验不一致'
                    data['events'].append({'time':now,'status':'failed'})
                updates.append((data['current_state']=='matching',json.dumps(data,ensure_ascii=False),job['id'],sid,path))
            self.store.db.executemany("UPDATE transfer_files SET status=CASE WHEN ? THEN CASE WHEN action='verify' THEN 'success' ELSE status END ELSE 'failed' END,bytes=CASE WHEN action='verify' THEN total ELSE bytes END,data=? WHERE job_id=? AND server_id=? AND path=?",updates)

    def verification_pending(self,job,sid,error):
        if not job:return
        with self.store.lock,self.store.db:
            rows=self.store.db.execute('SELECT path,data FROM transfer_files WHERE job_id=? AND server_id=?',(job['id'],sid)).fetchall()
            updates=[]
            for path,raw in rows:
                data=json.loads(raw);data.update(verification_error=str(error),current_state='unchecked')
                data['events'].append({'time':time.time(),'status':'verification_pending'})
                updates.append((json.dumps(data,ensure_ascii=False),job['id'],sid,path))
            self.store.db.executemany("UPDATE transfer_files SET status='verification_pending',data=? WHERE job_id=? AND server_id=? AND path=?",updates)

    def local_complete(self,job):
        if not job:return
        now=time.time();updates=[]
        with self.store.lock:
            rows=self.store.db.execute("SELECT path,action,data FROM transfer_files WHERE job_id=? AND server_id='local'",(job['id'],)).fetchall()
        for path,action,raw in rows:
            data=json.loads(raw);status='skipped' if action=='skip' else 'success'
            data.update(current_state='matching',current_sha256=data['expected_sha256'],verified_at=now,finished_at=now,updated_at=now)
            data['events'].append({'time':now,'status':status})
            updates.append((status,json.dumps(data,ensure_ascii=False),job['id'],path))
        self.write_batches("UPDATE transfer_files SET status=?,bytes=total,data=? WHERE job_id=? AND server_id='local' AND path=?",updates)

    def finish_pending(self,job,status,error):
        if not job:return
        now=time.time();updates=[]
        with self.store.lock,self.store.db:
            rows=self.store.db.execute("SELECT server_id,path,data FROM transfer_files WHERE job_id=? AND status IN ('queued','uploading','verifying')",(job['id'],)).fetchall()
            for sid,path,raw in rows:
                data=json.loads(raw);data.update(error=str(error),updated_at=now,finished_at=now)
                data['events'].append({'time':now,'status':status})
                updates.append((status,json.dumps(data,ensure_ascii=False),job['id'],sid,path))
            self.store.db.executemany('UPDATE transfer_files SET status=?,data=? WHERE job_id=? AND server_id=? AND path=?',updates)

    def page(self,job_id,offset=0,limit=100,query='',server='',status=''):
        if offset<0 or not 1<=limit<=200:raise ValueError('分页参数无效')
        where=['job_id=?'];args=[job_id]
        if query:where.append('instr(lower(path),?)>0');args.append(query.lower())
        if server:where.append('server_id=?');args.append(server)
        if status:where.append('status=?');args.append(status)
        condition=' AND '.join(where)
        with self.store.lock:
            total=self.store.db.execute('SELECT count(*) FROM transfer_files WHERE job_id=?',(job_id,)).fetchone()[0]
            matched=self.store.db.execute('SELECT count(*) FROM transfer_files WHERE '+condition,args).fetchone()[0]
            rows=self.store.db.execute('SELECT status,action,initial_state,bytes,total,data FROM transfer_files WHERE '+condition+' ORDER BY path COLLATE NOCASE,server_id LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
            counts=self.store.db.execute('SELECT server_id,status,count(*) FROM transfer_files WHERE job_id=? GROUP BY server_id,status',(job_id,)).fetchall()
        return {'files':[{**json.loads(raw),'status':s,'action':a,'initial_state':initial,'bytes':done,'total':length} for s,a,initial,done,length,raw in rows],
                'total_count':total,'matched_count':matched,'offset':offset,'limit':limit,'summary':[{'server_id':sid,'status':s,'count':count} for sid,s,count in counts]}
