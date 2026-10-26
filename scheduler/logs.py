"""Bounded UTF-8 log reads, shared by local and dedicated SFTP streams."""
import codecs
from pathlib import Path

TAIL_BYTES=2*1024**2
CHUNK_BYTES=128*1024


def tail_stream(stream,length,lines=200,max_bytes=TAIL_BYTES):
    end=length;blocks=[];read=0;newlines=0
    while end>0 and read<max_bytes and newlines<=lines:
        amount=min(65536,end,max_bytes-read);end-=amount;stream.seek(end);block=stream.read(amount)
        blocks.append(block);read+=len(block);newlines+=block.count(b'\n')
        if not block:break
    raw=b''.join(reversed(blocks))
    selected=raw.splitlines(keepends=True)[-lines:]
    text=b''.join(selected).decode('utf-8','replace')
    return {'text':text,'lines':len(selected),'size':length,'has_more':end>0 or len(raw.splitlines())>lines,
            'truncated_line':end>0 and newlines<=lines,'read_bytes':read}


def chunk_stream(stream,length,offset=0,limit=CHUNK_BYTES):
    if offset<0 or not 1<=limit<=CHUNK_BYTES:raise ValueError('日志分页参数无效')
    if offset>length:offset=0
    stream.seek(offset);raw=stream.read(min(limit,max(0,length-offset)))
    final=offset+len(raw)>=length
    decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
    text=decoder.decode(raw,final=final);pending=decoder.getstate()[0]
    # A very small requested page must still make progress across a UTF-8 char.
    if pending and len(pending)==len(raw):
        extra=stream.read(min(4,max(0,length-offset-len(raw))))
        raw+=extra;text+=decoder.decode(extra,final=offset+len(raw)>=length);pending=decoder.getstate()[0]
    next_offset=offset+len(raw)-len(pending)
    return {'text':text,'offset':offset,'next_offset':next_offset,'size':length,'has_more':next_offset<length}


def read_local(path,mode='tail',offset=0,limit=CHUNK_BYTES):
    path=Path(path)
    if not path.exists():return {'text':'','size':0,'lines':0,'has_more':False,'offset':0,'next_offset':0,'source':'local'}
    if path.is_symlink() or not path.is_file():raise ValueError('日志必须是普通文件')
    with path.open('rb') as stream:
        info=path.stat();result=chunk_stream(stream,info.st_size,offset,limit) if mode=='full' else tail_stream(stream,info.st_size)
    return {**result,'modified_at':info.st_mtime,'source':'local'}
