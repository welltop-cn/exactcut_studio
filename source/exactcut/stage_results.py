"""Immutable per-attempt evidence, written before a staged child exits."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from .util import file_sha256, json_sha256, load_config, now_iso, read_json


class ResourceDeferred(RuntimeError):
    def __init__(self, not_before, message='等待资源'):
        super().__init__(message)
        self.not_before = not_before


def result_path(job, jobs):
    return jobs.parent / 'stage-results' / f"{job['id']}-{job.get('attempt', 1)}.json"


def fingerprint(job, batches, root, *, source_digests=None, config=None):
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
        manifest = BatchManifest.model_validate(values['confirmed.json'])
        sources = {}
        for episode in download_plan(manifest)['required']:
            try:
                source = resolve_episode_video(root / 'projects' / manifest.drama_id, episode)
                sources[episode] = source_digests[episode] if source_digests is not None else digest_evidence(source)['sha256']
            except (OSError, RuntimeError, ValueError): sources[episode] = None
        values['sources'] = sources
    return json_sha256(values)


def artifacts(job, batches, root):
    path = batches / job.get('batch_id', '')
    files = []
    if job.get('phase') == 'render':
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


def verified(job, jobs, batches, root, *, config=None):
    file = result_path(job, jobs)
    if not file.exists(): return None
    try:
        result = read_json(file)
        if result['job_id'] != job['id'] or result['phase'] != job.get('phase') or result['attempt'] != job.get('attempt', 1): return None
        if result['input_fingerprint'] != fingerprint(job, batches, root, config=config): return None
        for artifact in result['artifacts']:
            path = Path(artifact['path'])
            if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact['bytes'] or file_sha256(path) != artifact['sha256']: return None
        if 'publication' in result and json_sha256(result['publication']) != result['publication_sha256']: return None
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError): return None



class EvidencePending(RuntimeError):
    """Only this job waits for bounded, lock-free evidence preparation."""


# A bounded number of reads, with no executor backlog. Completed immutable evidence
# is cached only under its full stat/metadata identity; cache size is also bounded.
_EVIDENCE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='exactcut-evidence')
_EVIDENCE_LOCK = threading.Lock()
_PENDING = {}
_READY = OrderedDict()


def file_identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('阶段文件缺失或为软链接')
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def digest_evidence(path):
    path = Path(path)
    before = file_identity(path)
    digest = file_sha256(path)
    if before != file_identity(path): raise ValueError('阶段文件在摘要计算期间发生变化')
    return {'path': str(path), 'sha256': digest, 'bytes': before[2], 'stat': before}


def source_paths(job, batches, root):
    path = batches / job.get('batch_id', '') / 'confirmed.json'
    if job.get('phase') != 'render' or not path.exists(): return {}
    from .media import resolve_episode_video
    from .workflow import download_plan
    from .models import BatchManifest
    manifest = BatchManifest.model_validate(read_json(path))
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
        for old, future in list(_PENDING.items()):
            if future.done():
                _READY[old] = future
                del _PENDING[old]
        while len(_READY) > 128: _READY.popitem(last=False)
        if key in _READY:
            future = _READY[key]
            _READY.move_to_end(key)
            return future.result()
        if key not in _PENDING and len(_PENDING) < 2:
            _PENDING[key] = _EVIDENCE_POOL.submit(operation)
        raise EvidencePending('阶段摘要正在锁外准备')


def source_snapshot(paths):
    return {episode: {'path': str(path), 'stat': file_identity(path)} if path is not None else None
            for episode, path in paths.items()}


def scheduler_fingerprint(job, batches, root, *, jobs=None):
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
    evidence = background(key, lambda: {episode: digest_evidence(path) if path is not None else None
                                        for episode, path in paths.items()})
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
    if not file.exists(): return None
    try:
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
            return verified(job, jobs, batches, root)
        snapshot = verification_snapshot(job, jobs, batches, root, result)
        config = load_config()
        key = ('verified', json_sha256({'job': job, 'snapshot': snapshot, 'config': config}))
        def validate():
            value = verified(job, jobs, batches, root, config=config)
            return value if snapshot == verification_snapshot(job, jobs, batches, root, result) else None
        value = background(key, validate)
        return value if snapshot == verification_snapshot(job, jobs, batches, root, result) else None
    except EvidencePending: raise
    except (OSError, ValueError, KeyError, TypeError, RuntimeError): return None

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
