"""Paginated, durable file decisions; never put large inventories in /state."""
import json
import time
from itertools import islice
from .transfer_storage import JournalWriter, initialize
from .file_browser import sql_directory_page


def product_servers(servers,inventory):
    for server in servers:
        for item in inventory.items():
            yield server,item


class TransferJournal:
    def __init__(self,store):
        self.store=store
        with store.lock:
            initialize(store.db)

    def save_batches(self, rows, write):
        rows=iter(rows)
        while True:
            batch=list(islice(rows,1000))
            if not batch:
                return
            with self.store.lock,self.store.db:
                write(JournalWriter(self.store.db),batch)
                self.store.db.execute('DELETE FROM transfer_details WHERE uses=0')

    def prepare(self,job,servers,inventory,action='pending'):
        if not job:return
        now=time.time()
        def rows():
            for server,(path,item) in product_servers(servers,inventory):
                data={'path':path,'server_id':server['id'],'server_name':server['name'],'expected_sha256':item['sha256'],'size':item['size'],'created_at':now,'events':[]}
                yield job['id'],server['id'],path,'queued',action,'unchecked',0,item['size'],data
        self.save_batches(rows(),JournalWriter.prepare)

    def plan(self,job,group,inventory,before,source):
        if not job:return
        now=time.time()
        def rows():
            for server,(path,item) in product_servers(group,inventory):
                remote=before.get(path)
                initial='missing' if remote is None else 'matching' if remote['sha256']==item['sha256'] else 'mismatch'
                action={'missing':'upload','matching':'skip','mismatch':'replace'}[initial]
                data={'path':path,'server_id':server['id'],'server_name':server['name'],'size':item['size'],
                      'expected_sha256':item['sha256'],'previous_sha256':remote.get('sha256') if remote else None,
                      'source_server_id':source['id'],'source_server_name':source['name'],'shared':server['id']!=source['id'],
                      'created_at':now,'updated_at':now,'events':[{'time':now,'status':'planned','message':action}]}
                yield job['id'],server['id'],path,'queued',action,initial,0,item['size'],data
        self.save_batches(rows(),JournalWriter.plan)

    def update(self,job,server_ids,path,status,done=None,error=None):
        self.update_many(job,server_ids,{path:done},status,error)

    def update_many(self,job,server_ids,amounts,status,error=None):
        if not job:return
        now=time.time()
        with self.store.lock,self.store.db:
            writer=JournalWriter(self.store.db)
            for sid in server_ids:
                for path,done in amounts.items():
                    row=self.store.db.execute(
                        'SELECT scope_key,path_key,status,bytes,total,data FROM transfer_file_rows '
                        'WHERE job_id=? AND server_id=? AND path=?',(job['id'],sid,path)).fetchone()
                    if not row:continue
                    scope,file,previous,count,total,raw=row
                    data=json.loads(raw)
                    if previous!=status:
                        data['events'].append({'time':now,'status':status})
                        if status=='uploading':data.setdefault('started_at',now)
                        if status in ('success','skipped','failed','interrupted'):data['finished_at']=now
                    data['updated_at']=now
                    if error:data['error']=str(error)
                    if done is not None:count=min(total,max(0,int(done)))
                    writer.update_state([(scope,file,data,status,count)])
            self.store.db.execute('DELETE FROM transfer_details WHERE uses=0')

    def _change_rows(self, job, change, server=None, pending=False):
        if not job:return
        with self.store.lock:
            scopes=self.store.db.execute('SELECT id,server_id FROM transfer_scopes WHERE job_id=?',
                                         (job['id'],)).fetchall()
        for scope,sid in scopes:
            if server is not None and server!=sid:continue
            last=0
            condition=" AND status IN ('queued','uploading','verifying')" if pending else ''
            while True:
                with self.store.lock,self.store.db:
                    rows=self.store.db.execute(
                        'SELECT path_key,status,action,bytes,total,data FROM transfer_file_rows '
                        'WHERE scope_key=? AND path_key>?'+condition+' ORDER BY path_key LIMIT 1000',
                        (scope,last)).fetchall()
                    if not rows:break
                    last=rows[-1][0]
                    changes=[]
                    for file,status,action,done,total,raw in rows:
                        data=json.loads(raw)
                        status,done=change(data,status,action,done,total)
                        changes.append((scope,file,data,status,done))
                    JournalWriter(self.store.db).update_state(changes)
                    self.store.db.execute('DELETE FROM transfer_details WHERE uses=0')

    def verify(self,job,sid,inventory,method='sha256',source=None):
        if not job:return
        now=time.time()
        source_name=self.store.get('servers',source)['name'] if source else None
        def checked(data,status,action,done,total):
            actual=inventory.get(data['path'],{}).get('sha256')
            state='missing' if actual is None else 'matching' if actual==data['expected_sha256'] else 'mismatch'
            data.update(current_sha256=actual,current_state=state,verified_at=now,verification_method=method,verification_source=source)
            if source:data.update(source_server_id=source,source_server_name=source_name,shared=source!=sid)
            if state=='matching':
                data['events'].append({'time':now,'status':'verified'})
                if action=='verify':status='success'
            else:
                data['error']='服务器最终 hash 核验不一致'
                data['events'].append({'time':now,'status':'failed'})
                status='failed'
            return status,total if action=='verify' else done
        self._change_rows(job,checked,server=sid)

    def verification_pending(self,job,sid,error):
        now=time.time()
        def pending(data,status,action,done,total):
            data.update(verification_error=str(error),current_state='unchecked')
            data['events'].append({'time':now,'status':'verification_pending'})
            return 'verification_pending',done
        self._change_rows(job,pending,server=sid)

    def local_complete(self,job):
        now=time.time()
        def complete(data,status,action,done,total):
            status='skipped' if action=='skip' else 'success'
            data.update(current_state='matching',current_sha256=data['expected_sha256'],verified_at=now,finished_at=now,updated_at=now)
            data['events'].append({'time':now,'status':status})
            return status,total
        self._change_rows(job,complete,server='local')

    def finish_pending(self,job,status,error):
        now=time.time()
        def finish(data,previous,action,done,total):
            data.update(error=str(error),updated_at=now,finished_at=now)
            data['events'].append({'time':now,'status':status})
            return status,done
        self._change_rows(job,finish,pending=True)

    def page(self,job_id,offset=0,limit=100,query='',server='',status='',directory=None):
        if offset<0 or not 1<=limit<=200:raise ValueError('分页参数无效')
        where=['s.job_id=?']
        args=[job_id]
        if query:
            where.append('instr(lower(p.path),?)>0')
            args.append(query.lower())
        if server:
            where.append('s.server_id=?')
            args.append(server)
        if status:
            where.append('e.status=?')
            args.append(status)
        if directory:
            where.append('p.path>=? AND p.path<?')
            args.extend((directory+'/',directory+'0'))
        condition=' WHERE '+' AND '.join(where)
        entries=(' FROM transfer_scopes s JOIN transfer_entries e ON e.scope=s.id '
                 'JOIN transfer_paths p ON p.id=e.file')
        if directory is not None:
            with self.store.read_snapshot() as db:
                source = 'SELECT p.path,e.scope AS key1,e.file AS key2'+entries+condition
                folders, keys, count = sql_directory_page(db, source, args, directory, query, offset, limit)
                files = self._browse_rows(db,keys)
                counts = self._page_counts(db,job_id)
            return {'files':files,'folders':folders,'entry_count':count,'directory':directory,
                    'total_count':sum(n for _,_,n in counts),'matched_count':count,'offset':offset,'limit':limit,
                    'summary':[{'server_id':sid,'status':s,'count':n} for sid,s,n in counts]}
        with self.store.lock:
            counts=self._page_counts(self.store.db,job_id)
            total=sum(count for _,_,count in counts)
            candidates=sum(n for sid,st,n in counts if (not server or server==sid) and (not status or status==st))
            summary=[{'server_id':sid,'status':s,'count':count} for sid,s,count in counts]
            matched=self.store.db.execute('SELECT count(*)'+entries+condition,args).fetchone()[0] if query else candidates
            # A large result can stream directly in display order. Small or rare
            # subsets are cheaper to sort than walking the global path dictionary.
            if candidates>10000:
                entries=(' FROM transfer_paths p INDEXED BY transfer_paths_order CROSS JOIN transfer_scopes s '
                         'CROSS JOIN transfer_entries e ON e.scope=s.id AND e.file=p.id')
            selected=('SELECT e.scope,e.file,p.path AS path,s.server_id AS sid'+entries+condition+
                      ' ORDER BY p.path COLLATE NOCASE,s.server_id,p.path LIMIT ? OFFSET ?')
            rows=self.store.db.execute(
                'WITH page AS ('+selected+') '
                'SELECT f.status,f.action,f.initial_state,f.bytes,f.total,f.data FROM page '
                'CROSS JOIN transfer_file_rows f ON f.scope_key=page.scope AND f.path_key=page.file '
                'ORDER BY page.path COLLATE NOCASE,page.sid,page.path',args+[limit,offset]).fetchall()
        return {'files':[{**json.loads(raw),'status':s,'action':a,'initial_state':initial,'bytes':done,'total':length} for s,a,initial,done,length,raw in rows],
                'total_count':total,'matched_count':matched,'offset':offset,'limit':limit,'summary':summary}

    @staticmethod
    def _page_counts(db, job_id):
        scopes=db.execute('SELECT id,server_id FROM transfer_scopes WHERE job_id=? ORDER BY server_id',(job_id,)).fetchall()
        return [(sid,st,count) for scope,sid in scopes for st,count in db.execute(
            'SELECT status,count(*) FROM transfer_entries WHERE scope=? GROUP BY status',(scope,))]

    @staticmethod
    def _browse_rows(db, keys):
        if not keys:
            return []
        values = ','.join('(?,?)' for _ in keys)
        rows = db.execute(
            f'WITH page(scope,file) AS (VALUES {values}) '
            'SELECT page.scope,page.file,f.status,f.action,f.initial_state,f.bytes,f.total,f.data '
            'FROM page CROSS JOIN transfer_file_rows f ON f.scope_key=page.scope AND f.path_key=page.file',
            [item for key in keys for item in key])
        by_key = {(scope,file): {**json.loads(raw),'status':s,'action':a,'initial_state':initial,'bytes':done,'total':length}
                  for scope,file,s,a,initial,done,length,raw in rows}
        return [by_key[key] for key in keys if key in by_key]
