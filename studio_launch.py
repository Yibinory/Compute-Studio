"""Locate staged program versions without changing the workspace data path."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


UPDATE_DIR = '.compute-updates'
WORKSPACE_LOCKED = 73
WORKSPACE_BUSY = 74
VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
PROGRAM_ROOT = Path(__file__).resolve().parent
INSTALL_ROOT = PROGRAM_ROOT.parents[2] if PROGRAM_ROOT.parent.name == 'versions' and PROGRAM_ROOT.parent.parent.name == UPDATE_DIR else PROGRAM_ROOT


def version_key(version):
    if not isinstance(version,str) or not VERSION_PATTERN.fullmatch(version) or len(version)>32:
        raise ValueError('版本号必须为主版本.次版本.修订号')
    return tuple(int(part) for part in version.split('.'))


def program_version(source=PROGRAM_ROOT):
    value=(source/'VERSION').read_text(encoding='utf-8').strip()
    version_key(value)
    return value


def update_state(home=INSTALL_ROOT):
    try:
        return json.loads((home/UPDATE_DIR/'selection.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def write_update_state(value,home=INSTALL_ROOT):
    root=home/UPDATE_DIR
    root.mkdir(exist_ok=True)
    with NamedTemporaryFile(mode='w',dir=root,delete=False,encoding='utf-8') as stream:
        temp=Path(stream.name)
        json.dump(value,stream,ensure_ascii=False)
    try:
        os.replace(temp,root/'selection.json')
    finally:
        temp.unlink(missing_ok=True)


def version_source(version,home=INSTALL_ROOT):
    version_key(version)
    versions=home/UPDATE_DIR/'versions'
    target=versions/version
    if target.resolve().parent!=versions.resolve() or program_version(target)!=version:
        raise ValueError('已安装的版本目录无效')
    return target


def launch_selected(entry,*,activate=False):
    if PROGRAM_ROOT!=INSTALL_ROOT:
        return None
    selection=update_state()
    pending=selection.get('pending') if activate else None
    current=selection.get('current')
    args=sys.argv[1:]
    if pending:
        try:
            result=subprocess.call([sys.executable,'-B',str(version_source(pending)/entry),*args])
        except (OSError,ValueError):
            result=1
        state=update_state()
        if state.get('pending')!=pending or result==WORKSPACE_LOCKED:
            return result
        if result==0:
            return result
        record_start_failure(pending, result, INSTALL_ROOT)
    if current:
        return subprocess.call([sys.executable,'-B',str(version_source(current)/entry),*args])
    return None


def mark_running(version,home=INSTALL_ROOT):
    from scheduler.local_storage import root_lock

    with root_lock(home/UPDATE_DIR):
        state=update_state(home)
        if state.get('pending')==version:
            write_update_state({'current':version,'previous':state.get('current'),'pending':None},home)


def record_start_failure(version, result, home):
    from scheduler.local_storage import root_lock

    with root_lock(home/UPDATE_DIR):
        state=update_state(home)
        if state.get('pending')!=version:
            return
        if result==WORKSPACE_BUSY:
            write_update_state({**state,'error':'上次启动时仍有后台工作，更新保留至下次空闲重启。'},home)
        else:
            write_update_state({**state,'pending':None,'error':f'版本 {version} 启动失败，已恢复上一版本。'},home)
