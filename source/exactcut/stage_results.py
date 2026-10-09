"""Immutable per-attempt evidence, written before a staged child exits."""
import json
import os
from pathlib import Path
import sys
import tempfile
from .util import file_sha256, json_sha256, load_config, now_iso, read_json


class ResourceDeferred(RuntimeError):
    def __init__(self, not_before, message='等待资源'):
        super().__init__(message)
        self.not_before = not_before


def result_path(job, jobs):
    return jobs.parent / 'stage-results' / f"{job['id']}-{job.get('attempt', 1)}.json"


def fingerprint(job, batches, root):
    path = batches / job.get('batch_id', '')
    values = {'phase': job.get('phase'), 'request_id': job.get('request_id'), 'config': {key: value for key, value in load_config().items() if job.get('phase') == 'render' and key in {'render', 'qc', 'safety'}}}
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
            try: sources[episode] = file_sha256(resolve_episode_video(root / 'projects' / manifest.drama_id, episode))
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
    return [{'path': str(p), 'sha256': file_sha256(p), 'bytes': p.stat().st_size} for p in files]


def complete(job, jobs, batches, root, code, *, status=None, not_before=None):
    target = result_path(job, jobs)
    if target.exists(): return read_json(target)
    result = {'version': 1, 'job_id': job['id'], 'phase': job.get('phase'), 'attempt': job.get('attempt', 1),
              'input_fingerprint': job.get('input_fingerprint') or fingerprint(job, batches, root),
              'status': status or ('done' if code == 0 else 'failed'), 'exit_code': code,
              'ended_at': now_iso(), 'artifacts': artifacts(job, batches, root) if code == 0 else []}
    if not_before is not None: result['not_before'] = not_before
    if job.get('phase') in {'upload', 'poll'}:
        entry = read_json(batches / job['batch_id'] / 'publications.json').get(job.get('request_id'), {})
        result['publication'] = entry
        result['publication_sha256'] = json_sha256(entry)
        if entry.get('status') == 'unverified': result['status'] = 'unverified'
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix='.result-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
        try: os.link(temp, target)
        except FileExistsError: pass
    finally: os.unlink(temp)
    return read_json(target)


def verified(job, jobs, batches, root):
    file = result_path(job, jobs)
    if not file.exists(): return None
    try:
        result = read_json(file)
        if result['job_id'] != job['id'] or result['phase'] != job.get('phase') or result['attempt'] != job.get('attempt', 1): return None
        if result['input_fingerprint'] != fingerprint(job, batches, root): return None
        for artifact in result['artifacts']:
            path = Path(artifact['path'])
            if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact['bytes'] or file_sha256(path) != artifact['sha256']: return None
        if 'publication' in result and json_sha256(result['publication']) != result['publication_sha256']: return None
        return result
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
