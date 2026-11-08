"""持久化任务的最小结构边界；隔离记录不进入 jobs 扫描。"""
import json
import math
import re
import threading
import signal
import time

from .util import read_json, write_json_atomic
from . import diagnostics as diag

_READ_LOCK = threading.RLock()


def alert(job_id, batch_id, reason):
    diag.event('job.quarantined', level='error', job_id=job_id, batch_id=batch_id,
               reason=reason, error={'type': 'InvalidJobRecord', 'message': reason})


def read(file, *, isolate=False, reader=read_json):
    with _READ_LOCK:
        # 仅文件本身消失属于正常删除；权限和其他 I/O 故障向外暴露。
        try:
            stat = file.stat()
        except FileNotFoundError:
            return None
        stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        marker = file.parent.parent / 'job-quarantine' / (file.name + '.json')
        if isolate and marker.exists() and read_json(marker).get('stamp') == stamp:
            return None
        try:
            job = reader(file)
            if isinstance(job, dict): return job
            reason = '任务 JSON 必须是对象：' + file.name
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError):
            reason = '任务 JSON 无法解析：' + file.name
        if isolate:
            write_json_atomic(marker, {'stamp': stamp, 'reason': reason})
            alert(file.stem, None, reason)
        return None


def load(file, jobs, batches, *, isolate=False, reader=read_json):
    from . import batch_control
    # rebuild/outbox 也使用同一身份确认及隔离入口，且在预留账本锁之前调用。
    with batch_control.LOCK:
        job = read(file, isolate=isolate, reader=reader)
        if job is None or job.get('quarantine'): return None
        reason = validate(job, file, batches)
        if reason:
            if isolate: quarantine(job, file, reason, jobs, batches)
            return None
        return job


def quarantine(job, file, reason, jobs, batches):
    """仅在确认自有执行器退出后终结；系统/身份查询故障继续向外暴露。"""
    from . import batch_control, workflow, storage
    if isinstance(job, dict) and job.get('quarantine'):
        return
    if isinstance(job, dict):
        identity = job
        if batch_control.belongs(job) and batch_control.valid_token(job.get('process_token')):
            found = batch_control.PROCESSES.find(job['process_token'])
            if found:
                identity = {**job, **found}
        if batch_control.child_state(identity) not in {None, 'Z'}:
            batch_control.signal_identity(identity, signal.SIGKILL)
            for _ in range(20):
                if batch_control.child_state(identity) in {None, 'Z'}: break
                time.sleep(0.01)
            else:
                raise RuntimeError('坏任务执行器终止尚未确认，保留任务与预留')
    job = dict(job) if isinstance(job, dict) else {}
    batch = job.get('batch_id')
    path = (batches / batch if batches is not None and isinstance(batch, str)
            and re.fullmatch(r'[0-9a-f]{32}', batch) else None)
    delivery = job.get('phase') in ('upload', 'poll') or job.get('action') == 'publish'
    status = 'unverified' if delivery else 'failed'
    if path and delivery and isinstance(job.get('request_id'), str):
        publications = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
        entry = publications.get(job.get('request_id'), {})
        if (workflow.delivery_blocked(entry) or entry.get('publication') or entry.get('intent')
                or entry.get('verification_only') or entry.get('verification_history')):
            status = 'unverified'
            entry.update(status=status, error='待核实：任务记录损坏，禁止重放交付')
            workflow.store_publication(path, job.get('request_id'), entry)
    if (batches is not None and isinstance(batch, str) and re.fullmatch(r'[a-zA-Z0-9_-]+', batch)
            and not (batches / batch / 'draft.json').exists()):
        status = 'interrupted' if status == 'failed' else status
    job.update(id=file.stem, status=status, finished=time.time(), quarantine={'reason': reason})
    write_json_atomic(file, job)
    storage.release(file.stem, jobs)
    alert(file.stem, batch, reason)


def validate(job, file, batches):
    if not isinstance(job, dict): return '任务必须是对象'
    if (not isinstance(job.get('id'), str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', job['id'])
            or job['id'] != file.stem): return '任务 ID 与文件名不匹配'
    batch = job.get('batch_id')
    if batch is not None and (not isinstance(batch, str) or not re.fullmatch(r'[0-9a-f]{32}', batch)):
        return 'batch_id 必须是 32 位十六进制字符串'
    for key in ('created', 'not_before', 'started', 'finished'):
        if key in job and (type(job[key]) not in (int, float) or not math.isfinite(job[key])):
            return key + ' 必须是有限数值'
    if 'attempt' in job and (type(job['attempt']) is not int or job['attempt'] < 0):
        return 'attempt 必须是非负整数'
    for key in ('status', 'action', 'lane', 'phase', 'request_id', 'owner', 'project', 'instance', 'process_token'):
        if key in job and job[key] is not None and not isinstance(job[key], str): return key + ' 必须是字符串'
    if 'command' in job and (not isinstance(job['command'], list) or not job['command']
                             or any(not isinstance(arg, str) for arg in job['command'])):
        return 'command 必须是非空字符串列表'
    admission = job.get('admission')
    if admission is not None:
        if not isinstance(admission, dict): return 'admission 必须是对象'
        paths = admission.get('paths')
        if paths is not None and (not isinstance(paths, dict) or any(
                not isinstance(path, str) or type(size) is not int or size < 0 for path, size in paths.items())):
            return 'admission.paths 必须是路径到非负字节数的映射'
        allocations = admission.get('allocations')
        if allocations is not None and (not isinstance(allocations, dict) or any(
                not isinstance(row, dict) or not isinstance(row.get('path'), str)
                or type(row.get('bytes')) is not int or row['bytes'] < 0 for row in allocations.values())):
            return 'admission.allocations 无效'
    if (batches is not None and batch and job.get('status') == 'running' and job.get('action') == 'prefetch'
            and not (batches / batch / 'draft.json').is_file()):
        return '预取任务的批次草稿不存在'
    return None
