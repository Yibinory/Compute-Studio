"""Remove public inputs without scanning, hashing or copying them in HTTP requests."""
import os
import time

from .files import confined, relative
from .file_process import run
from .policy import ACTIVE
from .storage import uid


class PublicFiles:
    def __init__(self, engine):
        self.engine = engine

    def validate(self, project_id, paths, all_files, job=None):
        e = self.engine
        if e.storage_moving:
            raise ValueError('本机保存目录正在迁移，请稍后清除')
        e.store.get('projects', project_id)
        if e.code_sources.bound(project_id):raise ValueError('项目已绑定源码目录，请先解绑再清理公共副本；工具不会反向删除原目录')
        active = [t['name'] for t in e.store.all('tasks') if t['project_id'] == project_id and t['status'] in ACTIVE]
        if active:
            raise ValueError('项目存在执行、暂停运行、回传或状态未知的实验，不能清除公共文件：' + '、'.join(active[:5]))
        if e.uploads and e.uploads.pending(project_id, exclude_job=(job or {}).get('id')):
            raise ValueError('项目有未完成的文件导入或清除操作，请完成或放弃导入后再清除')
        if not isinstance(all_files, bool) or not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError('paths 须为文件路径数组，all 须为布尔值')
        if all_files and paths or not all_files and not 1 <= len(paths) <= 1000:
            raise ValueError('请选择 1–1000 个文件，或选择清空全部；两种方式不能同时使用')
        project = e.local_project(project_id)
        target = confined(project, 'code')
        if target.resolve() != target.absolute():
            raise ValueError('公共文件目录不支持符号链接或目录联接')
        normalized = [relative(p) for p in paths]
        if len({p.casefold() for p in normalized}) != len(normalized):
            raise ValueError('不能重复选择同一文件')
        for rel in normalized:
            if not confined(target, rel).is_file():
                raise ValueError('文件不存在或不是普通文件，请刷新列表：' + rel)
        return target, normalized

    def clear_public_files(self, project_id, paths, all_files=False, job=None):
        e = self.engine
        # Only metadata moves occur while holding the scheduler lock. The old
        # tree is then unlinked by a separate process, outside this lock.
        with e.lock:
            target, paths = self.validate(project_id, paths, all_files, job)
            trash = confined(e.local_project(project_id), '.file-deletions/' + (job['id'] if job else uid()))
            trash.mkdir(parents=True)
            moved = []
            try:
                if all_files:
                    if target.exists():
                        os.replace(target, trash / 'code')
                        moved.append((target, trash / 'code'))
                    target.mkdir(exist_ok=True)
                else:
                    for rel in paths:
                        source, dest = confined(target, rel), confined(trash, rel)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(source, dest)
                        moved.append((source, dest))
                project = e.store.get('projects', project_id)
                project.update(public_files_revision=uid(), public_files_updated_at=time.time())
                e.store.put('projects', project)
            except Exception:
                if all_files and moved and target.exists():
                    target.rmdir()
                for source, dest in reversed(moved):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(dest, source)
                raise
        e.progress(job, phase='clearing', path='项目公共文件', files_total=len(paths) if not all_files else 0, files_done=0)
        def update(kind, event, scratch, pid):
            if kind == 'progress':
                e.progress(job, **{k: v for k, v in event.items() if k != 'type'}, worker_pid=pid)
        try:
            run(e.root, 'remove', path=str(trash), update=update, report_removal=True)
        except Exception as exc:
            raise ValueError('公共文件已移出代码目录，但磁盘清理未完成；剩余位置：' + str(trash) + '；' + str(exc)) from exc
        e.progress(job, phase='complete', files_done=len(paths), files_total=len(paths))
        e.store.event('清除项目公共文件 · ' + project['name'] + (' · 全部' if all_files else ' · ' + str(len(paths)) + ' 个文件'))
        return {'project_id': project_id, 'all': all_files, 'deleted_paths': paths, 'deleted_count': len(paths) if not all_files else None}
