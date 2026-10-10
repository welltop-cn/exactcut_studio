"""Immutable per-attempt evidence, written before a staged child exits."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import threading
import time
from functools import partial
from collections import OrderedDict
from .util import file_sha256, json_sha256, load_config, now_iso, read_json


class ResourceDeferred(RuntimeError):
    def __init__(self, not_before, message='等待资源'):
        super().__init__(message)
        self.not_before = not_before


def result_path(job, jobs):
    return jobs.parent / 'stage-results' / f"{job['id']}-{job.get('attempt', 1)}.json"


def fingerprint(job, batches, root, *, source_digests=None, config=None, _retry_errors=False):
    path = batches / job.get('batch_id', '')
    values = {'phase': job.get('phase'), 'request_id': job.get('request_id'), 'config': {key: value for key, value in (load_config() if config is None else config).items() if job.get('phase') == 'render' and key in {'render', 'qc', 'safety'}}}
    for name in ('confirmed.json', 'source-prefetch.json'):
        if (path / name).is_file(): values[name] = read_json(path / name)
    if job.get('phase') in {'upload', 'poll'}:
        file = path / 'publications.json'
        entry = read_json(file).get(job.get('request_id'), {}) if file.exists() else {}
        values['approved_sha256'] = entry.get('approved_sha256')
        values['record_id'] = (entry.get('publication') or {}).get('feishu_record_id') if job.get('phase') == 'poll' else None
    if job.get('phase') == 'render' and 'confirmed.json' in values:
        from .media import resolve_episode_video
        from .workflow import download_plan
        from .models import BatchManifest
        manifest = selected_manifest(BatchManifest.model_validate(values['confirmed.json']), job)
        sources = {}
        for episode in download_plan(manifest)['required']:
            try:
                source = resolve_episode_video(root / 'projects' / manifest.drama_id, episode)
                sources[episode] = source_digests[episode] if source_digests is not None else digest_evidence(source)['sha256']
            except EvidenceMismatch: sources[episode] = None
            except (OSError, RuntimeError, ValueError):
                if _retry_errors: raise
                sources[episode] = None
        values['sources'] = sources
    return json_sha256(values)


def artifacts(job, batches, root):
    path = batches / job.get('batch_id', '')
    files = []
    if job.get('phase') == 'render':
        if job.get('request_id'):
            from .workflow import request_for
            _, request = request_for(path, job['request_id'])
            files = [file for directory in ('videos', 'previews')
                     if (file := path / 'production' / directory / request.output_filename).is_file()]
        else:
            files = list((path / 'production/videos').glob('*.mp4')) + list((path / 'production/previews').glob('*.mp4'))
    elif job.get('phase') == 'fetch' and (path / 'confirmed.json').exists():
        from .media import resolve_episode_video
        from .workflow import download_plan
        from .models import BatchManifest
        manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
        files = [resolve_episode_video(root / 'projects' / manifest.drama_id, episode)
                 for episode in download_plan(manifest)['required']]
    return [digest_evidence(p) for p in files]


def complete(job, jobs, batches, root, code, *, status=None, not_before=None):
    target = result_path(job, jobs)
    if target.exists(): return read_json(target)
    result = {'version': 1, 'job_id': job['id'], 'phase': job.get('phase'), 'attempt': job.get('attempt', 1),
              'input_fingerprint': job.get('input_fingerprint') or fingerprint(job, batches, root),
              'status': status or ('done' if code == 0 else 'failed'), 'exit_code': code,
              'ended_at': now_iso(), 'artifacts': artifacts(job, batches, root) if code == 0 else [],
              'source_evidence': source_evidence(job, batches, root)}
    if not_before is not None: result['not_before'] = not_before
    if job.get('phase') in {'upload', 'poll'}:
        entry = read_json(batches / job['batch_id'] / 'publications.json').get(job.get('request_id'), {})
        result['publication'] = entry
        result['publication_sha256'] = json_sha256(entry)
        if entry.get('status') == 'unverified': result['status'] = 'unverified'
    result['evidence_sha256'] = json_sha256(result)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix='.result-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
        try: os.link(temp, target)
        except FileExistsError: pass
    finally: os.unlink(temp)
    return read_json(target)


def verified(job, jobs, batches, root, *, config=None, _retry_errors=False):
    file = result_path(job, jobs)
    if not file.exists(): return None
    try:
        result = read_json(file)
        if result['job_id'] != job['id'] or result['phase'] != job.get('phase') or result['attempt'] != job.get('attempt', 1): return None
        if result['input_fingerprint'] != fingerprint(job, batches, root, config=config, _retry_errors=_retry_errors): return None
        for artifact in result['artifacts']:
            path = Path(artifact['path'])
            if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact['bytes'] or file_sha256(path) != artifact['sha256']: return None
        if 'publication' in result and json_sha256(result['publication']) != result['publication_sha256']: return None
        return result
    except (EvidenceMismatch, json.JSONDecodeError, KeyError, TypeError): return None
    except (OSError, ValueError, RuntimeError):
        if _retry_errors: raise
        return None



class EvidencePending(RuntimeError):
    """Only this job waits for bounded, lock-free evidence preparation."""

class EvidenceMismatch(ValueError):
    """Definitive file identity loss/change, distinct from a transient read error."""


# Spawn rather than fork: server locks and other threads must not be inherited.
# Each read has its own killable process; a blocked read cannot hold a pool thread
# forever. There are only two outstanding reads and no executor backlog.
EVIDENCE_TIMEOUT_SECONDS = 120
_EVIDENCE_LOCK = threading.Lock()
_PENDING = {}
_READY = OrderedDict()


def _expire_evidence_process(signum, frame):
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _compute_evidence(operation, sender, timeout):
    os.setsid()
    # Also bound an orphan if the server dies before its parent-side timer fires.
    signal.signal(signal.SIGALRM, _expire_evidence_process)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        sender.send(('ok', operation()))
    except Exception as exc:
        sender.send(('error', (type(exc).__name__, str(exc))))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        sender.close()


class EvidenceRead:
    def __init__(self, operation):
        timeout = load_config().get('concurrency', {}).get('evidence_timeout_seconds', EVIDENCE_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float('inf'):
            raise ValueError('concurrency.evidence_timeout_seconds 必须为有限正数')
        context = multiprocessing.get_context('spawn')
        self.receiver, sender = context.Pipe(duplex=False)
        self.process = context.Process(target=_compute_evidence, args=(operation, sender, timeout), daemon=True)
        self.expired = False
        self.closed = False
        self.lifecycle_lock = threading.Lock()
        try: self.process.start()
        except Exception:
            self.receiver.close(); sender.close()
            raise
        sender.close()
        self.timer = threading.Timer(timeout, self.expire)
        self.timer.daemon = True
        self.timer.start()

    def kill(self):
        if self.process.is_alive():
            try:
                # Include ffprobe children used by legacy budget preparation.
                if os.getpgid(self.process.pid) == self.process.pid:
                    os.killpg(self.process.pid, signal.SIGKILL)
                else: self.process.kill()
            except ProcessLookupError: pass

    def expire(self):
        with self.lifecycle_lock:
            if not self.closed:
                self.expired = True
                self.kill()

    def done(self):
        return self.expired or self.receiver.poll() or not self.process.is_alive()

    def result(self):
        if self.expired: raise EvidencePending('阶段摘要超时，已终止读取；下一 tick 重算')
        try: status, value = self.receiver.recv()
        except (EOFError, OSError) as exc: raise OSError('摘要读取进程中断，等待重算') from exc
        if status == 'error':
            kind, message = value
            error = {'EvidenceMismatch': EvidenceMismatch, 'ValueError': ValueError,
                     'OSError': OSError, 'FileNotFoundError': FileNotFoundError}.get(kind, RuntimeError)
            raise error(message)
        return value

    def close(self):
        with self.lifecycle_lock:
            if self.closed: return
            self.closed = True
            self.timer.cancel()
            self.process.join(.1)
            self.kill()
            self.process.join(.1)
            self.receiver.close()
            if not self.process.is_alive(): self.process.close()


def _start_evidence(operation):
    return EvidenceRead(operation)


def file_identity(path):
    path = Path(path)
    if path.is_symlink(): raise EvidenceMismatch('阶段文件缺失或为软链接')
    try: identity = path.stat()
    except FileNotFoundError as exc: raise EvidenceMismatch('阶段文件缺失或为软链接') from exc
    if not stat.S_ISREG(identity.st_mode): raise EvidenceMismatch('阶段文件不是普通文件')
    return [identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns, identity.st_ctime_ns]


def digest_evidence(path):
    path = Path(path)
    before = file_identity(path)
    digest = file_sha256(path)
    if before != file_identity(path): raise EvidenceMismatch('阶段文件在摘要计算期间发生变化')
    return {'path': str(path), 'sha256': digest, 'bytes': before[2], 'stat': before}


def selected_manifest(manifest, job):
    request_id = job.get('request_id')
    if not request_id: return manifest
    selected = [r for r in manifest.requests if r.request_id == request_id]
    if not selected: raise ValueError('素材不存在')
    return manifest.model_copy(update={'requests': selected})


def source_paths(job, batches, root):
    path = batches / job.get('batch_id', '') / 'confirmed.json'
    if job.get('phase') != 'render' or not path.exists(): return {}
    from .media import resolve_episode_video
    from .workflow import download_plan
    from .models import BatchManifest
    manifest = selected_manifest(BatchManifest.model_validate(read_json(path)), job)
    sources = {}
    for episode in download_plan(manifest)['required']:
        try: sources[episode] = resolve_episode_video(root / 'projects' / manifest.drama_id, episode)
        except (OSError, RuntimeError, ValueError): sources[episode] = None
    return sources


def source_evidence(job, batches, root):
    return {episode: digest_evidence(path) if path is not None else None
            for episode, path in source_paths(job, batches, root).items()}


def background(key, operation):
    with _EVIDENCE_LOCK:
        failure = None
        for old, future in list(_PENDING.items()):
            if future.done():
                del _PENDING[old]
                try: _READY[old] = future.result()
                except Exception as exc:
                    # Failed futures never enter the reusable cache. Only their
                    # own job observes the error; unrelated jobs can take the slot.
                    if old == key: failure = exc
                finally: future.close()
        while len(_READY) > 128: _READY.popitem(last=False)
        if failure is not None: raise failure
        if key in _READY:
            _READY.move_to_end(key)
            return _READY[key]
        if key not in _PENDING and len(_PENDING) < 2:
            _PENDING[key] = _start_evidence(operation)
        raise EvidencePending('阶段摘要正在锁外准备')


def digest_sources(paths):
    return {episode: digest_evidence(path) if path is not None else None for episode, path in paths.items()}


def source_snapshot(paths):
    return {episode: {'path': str(path), 'stat': file_identity(path)} if path is not None else None
            for episode, path in paths.items()}


def scheduler_fingerprint(job, batches, root, *, jobs=None):
    try: return _scheduler_fingerprint(job, batches, root, jobs=jobs)
    except EvidenceMismatch: raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise EvidencePending('阶段输入暂不可读，下一 tick 重算') from exc


def _scheduler_fingerprint(job, batches, root, *, jobs=None):
    paths = source_paths(job, batches, root)
    if not paths: return fingerprint(job, batches, root, source_digests={})
    snapshot = source_snapshot(paths)
    if jobs is not None and job.get('parent_id'):
        parent_file = jobs / (job['parent_id'] + '.json')
        if parent_file.exists():
            parent = read_json(parent_file)
            result = scheduler_verified(parent, jobs, batches, root)
            if result and result['status'] == 'done':
                rows = {row['path']: row for row in result['artifacts']}
                if all(path is not None and str(path) in rows and rows[str(path)].get('stat') == snapshot[episode]['stat']
                       for episode, path in paths.items()):
                    return fingerprint(job, batches, root, source_digests={episode: rows[str(path)]['sha256'] for episode, path in paths.items()})
    key = ('sources', json_sha256(snapshot))
    try: evidence = background(key, partial(digest_sources, paths))
    except (OSError, ValueError, RuntimeError) as exc:
        raise EvidencePending('素材摘要读取失败，下一 tick 重算') from exc
    if source_snapshot(paths) != snapshot: raise EvidencePending('素材变化，等待重新准备摘要')
    if any(value is not None and (value['stat'] != snapshot[episode]['stat'] or value['path'] != snapshot[episode]['path']) for episode, value in evidence.items()):
        raise ValueError('阶段输入在摘要计算期间发生变化')
    return fingerprint(job, batches, root, source_digests={episode: value['sha256'] if value else None
                                                         for episode, value in evidence.items()})


def verification_snapshot(job, jobs, batches, root, result):
    """Fence legacy strong validation against concurrent mutation and file replacement."""
    files = [result_path(job, jobs)]
    path = batches / job.get('batch_id', '')
    files += [path / name for name in ('confirmed.json', 'source-prefetch.json', 'publications.json') if (path / name).exists()]
    files += [Path(row['path']) for row in result['artifacts']]
    files += [value for value in source_paths(job, batches, root).values() if value is not None]
    return {str(file): file_identity(file) for file in files}


def scheduler_verified(job, jobs, batches, root):
    """No large reads: new child evidence is stat-bound; legacy evidence is async."""
    file = result_path(job, jobs)
    try:
        if not file.exists(): return None
        result = read_json(file)
        if result['job_id'] != job['id'] or result['phase'] != job.get('phase') or result['attempt'] != job.get('attempt', 1): return None
        seal = result.get('evidence_sha256')
        if seal is not None and seal != json_sha256({key: value for key, value in result.items() if key != 'evidence_sha256'}): return None
        if seal is not None and 'source_evidence' in result and all('stat' in row for row in result['artifacts']):
            evidence = result['source_evidence']
            paths = source_paths(job, batches, root)
            if set(paths) != set(evidence): return None
            for episode, path in paths.items():
                row = evidence[episode]
                if path is None:
                    if row is not None: return None
                elif row is None or str(path) != row['path'] or file_identity(path) != row['stat']: return None
            current = fingerprint(job, batches, root, source_digests={episode: row['sha256'] if row else None for episode, row in evidence.items()})
            if result['input_fingerprint'] != current: return None
            for row in result['artifacts']:
                if file_identity(row['path']) != row['stat'] or row['bytes'] != row['stat'][2]: return None
            if 'publication' in result and json_sha256(result['publication']) != result['publication_sha256']: return None
            return result
        # Old results with no media require no expensive read, retaining immediate
        # recovery behavior. Media-bearing legacy evidence is strongly checked off-lock.
        if not result['artifacts'] and not source_paths(job, batches, root):
            return verified(job, jobs, batches, root, _retry_errors=True)
        snapshot = verification_snapshot(job, jobs, batches, root, result)
        config = load_config()
        key = ('verified', json_sha256({'job': job, 'snapshot': snapshot, 'config': config}))
        value = background(key, partial(verify_legacy, job, jobs, batches, root, result, snapshot, config))
        return value if snapshot == verification_snapshot(job, jobs, batches, root, result) else None
    except EvidencePending: raise
    except (EvidenceMismatch, json.JSONDecodeError, KeyError, TypeError): return None
    except (OSError, ValueError, RuntimeError) as exc:
        raise EvidencePending('阶段证据暂不可读，下一 tick 重算') from exc


def verify_legacy(job, jobs, batches, root, result, snapshot, config):
    value = verified(job, jobs, batches, root, config=config, _retry_errors=True)
    return value if snapshot == verification_snapshot(job, jobs, batches, root, result) else None

def run_child(operation, *args):
    from . import workflow
    token = sys._xoptions.get('exactcut_process_token')
    jobs = workflow.ROOT / '.web-state/jobs'
    job = next((read_json(p) for p in jobs.glob('*.json') if read_json(p).get('process_token') == token), None) if token else None
    try: code = operation(*args)
    except ResourceDeferred as exc:
        if job: complete(job, jobs, workflow.BATCHES, workflow.ROOT, 75, status='waiting_resource', not_before=exc.not_before)
        return 75
    if job: complete(job, jobs, workflow.BATCHES, workflow.ROOT, code)
    return code
