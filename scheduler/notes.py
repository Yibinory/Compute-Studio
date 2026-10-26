"""Unbounded Markdown sources; compact metadata for routine state polling."""
import codecs
import hashlib
import os
import time
from pathlib import Path
from scheduler.files import confined
from scheduler.storage import uid

INLINE_CHARS=12000
PREVIEW_CHARS=1500

def public_note(value):
    result={k:v for k,v in value.items() if k!='body_file'}
    body=value.get('body','')
    chars=value.get('body_chars',len(body))
    result.update(body_chars=chars,body_bytes=value.get('body_bytes',len(body.encode('utf-8'))),large=bool(value.get('body_file')) or chars>INLINE_CHARS)
    if result['large']:
        result.pop('body',None)
        result['body_preview']=value.get('body_preview',body[:PREVIEW_CHARS])
    return result

class Notes:
    def __init__(self,store):self.store=store

    def get(self,task_id,note_id):
        value=self.store.get('notes',note_id)
        if value['task_id']!=task_id:raise ValueError('记录不属于此节点')
        return value

    def path(self,value):return confined(self.store.root/'notes',value['body_file'])

    def previous(self,task_id,note_id):return self.get(task_id,note_id) if note_id else {}

    def save(self,task,body,note_id=None):
        if not isinstance(body,str) or not body.strip():raise ValueError('Notes 内容不能为空，须为 Markdown 文本')
        if len(body)>INLINE_CHARS:
            import io
            return self.save_stream(task,io.BytesIO(body.encode('utf-8')),note_id)
        with self.store.lock:
            previous=self.previous(task['id'],note_id)
            value=self.record(task,previous,body=body,body_chars=len(body),body_bytes=len(body.encode('utf-8')))
            self.store.put('notes',value)
            self.remove_file(previous)
        return public_note(value)

    @staticmethod
    def record(task,previous,**content):
        return {'id':previous.get('id') or uid(),'task_id':task['id'],'project_id':task['project_id'],'created_at':previous.get('created_at',time.time()),'updated_at':time.time(),**content}

    def save_stream(self,task,stream,note_id=None):
        # Request threads stream to disk; no entire-file JSON/body allocation.
        self.previous(task['id'],note_id)
        directory=self.store.root/'notes';directory.mkdir(exist_ok=True)
        filename=uid()+'.md';path=directory/filename;temporary=directory/(filename+'.tmp')
        decoder=codecs.getincrementaldecoder('utf-8-sig')('strict')
        chars=total=0;preview='';nonempty=False;digest=hashlib.sha256()
        try:
            with temporary.open('xb') as output:
                while True:
                    block=stream.read(1024*1024)
                    if not block:break
                    text=decoder.decode(block)
                    chars+=len(text);total+=len(block);nonempty=nonempty or bool(text.strip())
                    if len(preview)<PREVIEW_CHARS:preview+=text[:PREVIEW_CHARS-len(preview)]
                    digest.update(block);output.write(block)
                tail=decoder.decode(b'',final=True);chars+=len(tail);nonempty=nonempty or bool(tail.strip())
                if len(preview)<PREVIEW_CHARS:preview+=tail[:PREVIEW_CHARS-len(preview)]
            if not nonempty:raise ValueError('Notes 内容不能为空')
            os.replace(temporary,path)
            with self.store.lock:
                previous=self.previous(task['id'],note_id)
                content={'body_chars':chars,'body_bytes':total}
                if chars<=INLINE_CHARS:content['body']=path.read_text(encoding='utf-8-sig')
                else:content.update(body_file=filename,body_preview=preview,sha256=digest.hexdigest())
                value=self.record(task,previous,**content);self.store.put('notes',value)
                if 'body_file' not in content:path.unlink(missing_ok=True)
                self.remove_file(previous)
            return public_note(value)
        except UnicodeError as exc:
            path.unlink(missing_ok=True)
            raise ValueError('Markdown 文件须为 UTF-8 编码') from exc
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:temporary.unlink(missing_ok=True)

    def remove_file(self,value):
        if value.get('body_file'):
            try:self.path(value).unlink(missing_ok=True)
            except (OSError,ValueError) as exc:self.store.event('旧 Notes 源文件清理失败：'+str(exc),'warning')

    def delete(self,task_id,note_id):
        with self.store.lock:
            previous=self.get(task_id,note_id);self.store.delete('notes',note_id);self.remove_file(previous)
