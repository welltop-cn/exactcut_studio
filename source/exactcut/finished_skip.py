"""已结束检查的进程内无动作记忆；新增读文件必须同步维护 inputs 和防漏列测试。"""
from contextlib import contextmanager
import os
import json
import threading
import time

from . import material_versions as m, tick_snapshot
from .util import read_json

FULL_SCAN_SECONDS = 300
_local = threading.local()
COUNTERS = ('outbox_processed', 'outbox_skipped', 'crash_computed', 'crash_skipped')
# 防漏列测试只豁免锁和诊断日志；业务 JSON（包括 reservations）不得豁免。
AUDIT_EXEMPT_PARTS = frozenset({'media-locks', 'logs'})


def audit_exempt(path):
    return path.suffix == '.lock' or bool(AUDIT_EXEMPT_PARTS.intersection(path.parts))


def stat(path):
    try:
        s = path.stat()
        return s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    except FileNotFoundError:
        return None


class JobSnapshot(list):
    """只索引本次已消费的 job 值，不缓存文件 stat。"""
    def __init__(self, entries):
        super().__init__(entries)
        self.files = {file for file, _ in self}
        self.by_batch = {}
        for file, job in self:
            # 索引不提前改变旧恢复流程对坏记录的异常行为。
            if isinstance(job, dict):
                self.by_batch.setdefault(job.get('batch_id'), []).append((file, job))


def related_jobs(snapshot, batch_ids):
    if isinstance(snapshot, JobSnapshot):
        return [entry for batch_id in batch_ids for entry in snapshot.by_batch.get(batch_id, ())]
    return [(file, job) for file, job in snapshot if job.get('batch_id') in batch_ids]


def result_inputs(path):
    directory = path / 'production/results'
    # 目录自身和所有成员都参与指纹，覆盖新增、删除和替换（包括非 JSON）。
    return {path / 'production/results.json', directory, *directory.glob('*')}


def job_inputs(jobs_dir, snapshot, batch_ids):
    # 文件名集合由消费值清单比较；不依赖 jobs 目录的全局 stat。
    return {file for file, _ in related_jobs(snapshot, batch_ids)}


def crash_inputs(path, jobs_dir, snapshot):
    return ({m.registry(path), *(path / name for name in
             ('runtime.json', 'confirmed.json', 'publications.json', 'media-state.json'))}
            | result_inputs(path) | job_inputs(jobs_dir, snapshot, {path.name}))


def scans_all_jobs(parent, state, related):
    """与取消候选及 verification 的旧汇总入口保持同一门槛。"""
    for event in state['outbox'].values():
        if event['kind'] == 'verification':
            return True
        if event['kind'] != 'candidate':
            continue
        asset = state['assets'][event['asset_id']]
        version = state['versions'][event['version_id']]
        if (state['deleted_at'] or asset['deleted_at'] or version['status'] == 'superseded'
                or version['child_id'] != event['child_id']):
            child = parent.parent / event['child_id']
            if any(cancel_process_candidate(child, job) for _, job in related):
                return True
    return False


def outbox_inputs(parent, jobs_dir, snapshot, state=None, *, children=None, related=None):
    state = m.read(parent) if state is None else state
    files = {m.registry(parent), *(parent / name for name in
             ('media-state.json', 'control.json', 'draft.json', 'access.json'))}
    children = {e['child_id'] for e in state['outbox'].values()} if children is None else children
    related = related_jobs(snapshot, children) if related is None else related
    for child_id in children:
        child = parent.parent / child_id
        files.update({m.registry(child), *(child / name for name in
                      ('publications.json', 'draft.json', 'control.json', 'confirmed.json',
                       'runtime.json', 'media-state.json', 'access.json'))})
        files.update(result_inputs(child))
        for directory in ('production/videos', 'production/previews'):
            files.add(child / directory)
            # 恢复流程不遍历这些媒体；具体内容依赖在下方按事件列出。
    for event in state['outbox'].values():
        if event['kind'] == 'candidate':
            asset = state['assets'][event['asset_id']]
            version = state['versions'][event['version_id']]
            if (state['deleted_at'] or asset['deleted_at'] or version['status'] == 'superseded'
                    or version['child_id'] != event['child_id']):
                child = parent.parent / event['child_id']
                if any(cancel_process_candidate(child, job) for _, job in related):
                    files.add(jobs_dir.parent / 'reservations.json')
                    # 只有可能取消 job 时才会 release/end_execution/update。
                    files.update(file for file, _ in snapshot)
        if event['kind'] == 'verification':
            # queue_verify_delivery/runtime/update 的旧汇总入口自行扫描全部 job。
            files.update(file for file, _ in snapshot)
            edits = parent.parent / event['child_id'] / 'production/edits'
            files.add(edits)
            files.update(edits.rglob('*'))
            child = parent.parent / event['child_id']
            confirmed = read_json(child / 'confirmed.json')
            files.update(child / 'production/videos' / request['output_filename']
                         for request in confirmed['requests'])
        if event['kind'] == 'delivery':
            version = state['versions'][event['version_id']]
            files.add(m.media_file(parent, version['master_path']))
            child = parent.parent / event['child_id']
            files.add(child / 'production/videos' / event['manifest']['requests'][0]['output_filename'])
    # instance_marker -> data_root 可读取环境归属标记。
    files.add(parent.parent.parent / 'environment.json')
    return files | job_inputs(jobs_dir, related, children)


def cancel_process_candidate(child, job):
    """与 cancel_queued_candidate 到进程查询前的门槛一致。"""
    from . import batch_control as control, workflow as w
    if (job.get('batch_id') != child.name
            or job.get('phase') not in {'fetch', 'render', 'media-upload'}):
        return False
    retired = (job.get('status') == 'interrupted'
               and job.get('error') == '候选已取消或被替代；原成片已保留')
    if job.get('status') not in {'queued', 'waiting_resource'}:
        if not retired:
            return False
        runtime = read_json(child / 'runtime.json')
        if runtime.get('status') not in w.ACTIVE and not runtime.get('executor_running'):
            return False
    return control.belongs(job)


def outbox_volatile(parent, state, snapshot, now=None, *, children=None, related=None):
    now = time.time() if now is None else now
    children = {e['child_id'] for e in state['outbox'].values()} if children is None else children
    related = related_jobs(snapshot, children) if related is None else related
    related = [job for _, job in related]
    if any(job.get('not_before', 0) > now for job in related):
        return True
    for event in state['outbox'].values():
        if event['kind'] in {'media-upload', 'verification'} and event.get('status') != 'superseded':
            # recover_verification 只用 superseded 作终止门槛；不能仅信 materialized。
            return True
        if event['kind'] != 'candidate':
            continue
        asset = state['assets'][event['asset_id']]
        version = state['versions'][event['version_id']]
        invalid = (state['deleted_at'] or asset['deleted_at'] or version['status'] == 'superseded'
                   or version['child_id'] != event['child_id'])
        if invalid:
            control = parent.parent / event['child_id'] / 'control.json'
            if control.exists() and read_json(control).get('children'):
                return True
            # cancel_queued_candidate 还会直接按 job token/PID 查询进程表。
            if any(cancel_process_candidate(parent.parent / event['child_id'], j)
                   and (j.get('pid') or j.get('process_token')) for j in related):
                return True
    return False


def fingerprint(kind, path, jobs_dir, snapshot):
    try:
        anchors = (m.registry(path), path / 'media-state.json') if kind == 'outbox' else ()
        before = tuple(stat(p) for p in anchors)
        if kind == 'outbox':
            state = tick_snapshot.parent_state(path)
            batch_ids = {e['child_id'] for e in state['outbox'].values()}
            related = related_jobs(snapshot, batch_ids)
            if outbox_volatile(path, state, snapshot, children=batch_ids, related=related):
                return None
            scope = None if scans_all_jobs(path, state, related) else frozenset(batch_ids)
        else:
            scope = frozenset({path.name})
            related = related_jobs(snapshot, scope)
        if scope is None:
            related = snapshot
        # 快照值也参与比较，防止旧快照挂到新文件指纹下。
        consumed = tuple(sorted((str(file), json.dumps(job, sort_keys=True))
                                for file, job in related))
        session = getattr(_local, 'session', None)
        memory = session[0] if session else None
        known = memory.entries[kind].get(path) if memory else None
        value = None
        if known is not None and consumed == known[1]:
            # 重用输入路径集合，但逐项重新 stat。清单里的目录覆盖成员变化；
            # 状态、runtime、job 等动态清单条件改变时也会失败并重建清单。
            candidate = tuple((p, stat(p)) for p, _ in known[0])
            if candidate == known[0]:
                value = candidate
        if value is None:
            files = (outbox_inputs(path, jobs_dir, snapshot, state, children=batch_ids, related=related)
                     if kind == 'outbox' else crash_inputs(path, jobs_dir, snapshot))
            value = tuple((p, stat(p)) for p in sorted(files))
        if before != tuple(stat(p) for p in anchors):
            return None
        return value, consumed, scope
    except (OSError, ValueError, KeyError, TypeError):
        # 不缓存坏输入，也不改变原恢复流程的异常行为。
        return None


class Memory:
    def __init__(self, *, clock=None, interval=None):
        self.clock = time.monotonic if clock is None else clock
        self.interval = FULL_SCAN_SECONDS if interval is None else interval
        self.last_full = self.clock()
        self.entries = {'outbox': {}, 'crash': {}}

    def sweep(self):
        now = self.clock()
        if now - self.last_full >= self.interval:
            for entries in self.entries.values():
                entries.clear()
            self.last_full = now


@contextmanager
def worker_scope():
    """只有 worker_tick 显式进入；启动恢复、HTTP/CLI 及其他线程一律全量。"""
    previous = getattr(_local, 'session', None)
    enabled = os.environ.get('EXACTCUT_SKIP_FINISHED', '1') != '0'
    if enabled:
        memory = getattr(_local, 'memory', None)
        if memory is None:
            memory = _local.memory = Memory()
        memory.sweep()
    else:
        memory = None
        # 关闭后不保留旧记忆；再次开启仍先全量。
        if hasattr(_local, 'memory'):
            del _local.memory
    session = (memory, dict.fromkeys(COUNTERS, 0))
    _local.session = session
    try:
        yield session[1]
    finally:
        _local.session = previous


def finish_checks(checks):
    """阶段末尾共享一次文件名枚举，首轮也不按项目重复扫描 jobs。"""
    if not any(check.memory for check in checks):
        return
    try:
        current_files = set(checks[0].jobs_dir.glob('*.json'))
    except OSError:
        # Check 初始化时已删除旧项；枚举失败就保持未记忆。
        return
    for check in checks:
        check.finish(current_files=current_files)


class Check:
    def __init__(self, kind, path, jobs_dir, snapshot):
        self.kind, self.path, self.jobs_dir, self.snapshot = kind, path, jobs_dir, snapshot
        self.memory, counts = getattr(_local, 'session', None) or (None, None)
        self.before = fingerprint(kind, path, jobs_dir, snapshot) if self.memory else None
        self.skip = (self.before is not None
                     and self.memory.entries[kind].get(path) == self.before)
        if self.memory and not self.skip:
            self.memory.entries[kind].pop(path, None)
        if counts is not None:
            # outbox_processed/skipped：父项目处理/跳过数；crash_computed/skipped：runtime 计算/跳过数。
            key = ('outbox_skipped' if self.skip else 'outbox_processed') if kind == 'outbox' else (
                'crash_skipped' if self.skip else 'crash_computed')
            counts[key] += 1

    def finish(self, *, current_files=None):
        if self.skip:
            return
        if self.memory:
            # 阶段末共享文件名枚举；直接调用则自行重列目录。
            # 相关新文件未被快照消费时禁止记忆。
            try:
                known_files = (self.snapshot.files if isinstance(self.snapshot, JobSnapshot)
                               else {file for file, _ in self.snapshot})
                scope = self.before[2] if self.before is not None else None
                if current_files is None:
                    current_files = set(self.jobs_dir.glob('*.json'))
                for file in current_files - known_files:
                    if scope is None or read_json(file).get('batch_id') in scope:
                        self.memory.entries[self.kind].pop(self.path, None)
                        return
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.memory.entries[self.kind].pop(self.path, None)
                return
            after = fingerprint(self.kind, self.path, self.jobs_dir, self.snapshot)
            entries = self.memory.entries[self.kind]
            if self.before is not None and self.before == after:
                entries[self.path] = after
            else:
                entries.pop(self.path, None)
