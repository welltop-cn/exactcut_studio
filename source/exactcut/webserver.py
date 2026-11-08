"""Authenticated job console with explicit environment and data boundaries."""
from __future__ import annotations
import base64
import hmac
import hashlib
import gzip
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from urllib.parse import unquote, parse_qs
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .catalog import load_catalog
from . import workflow, batch_control, accounts, production_helper, concurrency, stage_results, storage
from .util import json_sha256, write_json_atomic, read_json, load_env_file

from .runtime import CODE_ROOT, data_root, require_publish, feature_flags, instance_marker, environment, release_id, maintenance_file, mutation_lock
from .runtime import project_env_file, claim_data_root
from . import diagnostics as diag
from . import lock_timing

ROOT = data_root()
STATE = ROOT / '.web-state'
JOBS = STATE / 'jobs'
WORK = queue.Queue()
LOCK = lock_timing.TimedLock(threading.Lock(), 'web')
WORKER_HEARTBEAT = 0.0
WORKER_FAILURES = 0
INLINE_RENDERS = set()


def web_base_path():
    from .util import load_config
    value = load_config().get('web', {}).get('base_path', '')
    if not isinstance(value, str) or (value and not re.fullmatch(r'(?:/[a-zA-Z0-9_-]+)+', value)):
        raise ValueError('web.base_path 必须为空或 /home 形式的路径，末尾不带斜线')
    return value


def project_path(project):
    if not isinstance(project, str) or not re.fullmatch(r'[0-9]{6}', project):
        raise ValueError('请选择六位剧 ID')
    path = ROOT / 'projects' / project
    if path.is_symlink() or not (path / 'project.json').is_file():
        raise ValueError('项目不存在')
    return path


def job_command(data):
    project = project_path(data.get('project'))
    action = data.get('action')
    if action == 'download':
        return [sys.executable, '-u', '-m', 'exactcut.material_download', project.name]
    if action not in ('prepare', 'compile', 'validate', 'execute', 'publish'):
        raise ValueError('不支持的操作')
    cmd = [sys.executable, '-u', '-m', 'exactcut', '--env-file', str(project_env_file()),
           'execute' if action == 'publish' else action, '--project', str(project)]
    if action != 'prepare':
        name = data.get('input', '')
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.xlsx'):
            raise ValueError('请选择 Excel 填写表')
        source = project / 'requests' / name
        if not source.is_file() or source.is_symlink():
            raise ValueError('填写表不存在')
        cmd += ['--input', str(source)]
    if action == 'publish':
        cmd += ['--publish', '--wait-ready']
    return cmd


def save(job):
    if diag.current().get('trace_id'):
        job.setdefault('trace_id', diag.current()['trace_id'])
    job.setdefault('lane', concurrency.lane(job))
    path = JOBS / (job['id'] + '.json')
    write_json_atomic(path, job)
    diag.throttled('job.state', (job['id'], job.get('status'), job.get('attempt'),
                                job.get('phase'), job.get('adopted_after_restart'), job.get('not_before')),
                   **{key: job.get(key) for key in ('trace_id', 'batch_id', 'request_id', 'actor', 'phase', 'lane',
                       'status', 'attempt', 'exit_code', 'not_before', 'adopted_after_restart', 'recovered_from_stage')},
                   job_id=job['id'], child_pid=job.get('pid'))


def stored_jobs():
    for file in JOBS.glob('*.json'):
        try:
            job = json.loads(file.read_text())
            if isinstance(job, dict) and isinstance(job.get('id'), str):
                yield job
        except (OSError, ValueError):
            logging.exception('Unreadable job record: %s', file.name)


def queue_size():
    count = 0
    for job in stored_jobs():
        if job.get('status') not in ('queued', 'running'):
            continue
        if job.get('batch_id'):
            path = workflow.BATCHES / job['batch_id']
            if not (path / 'draft.json').exists():
                continue
            control = batch_control.read(path)
            if control.get('paused') or control.get('archived'):
                continue
        count += 1
    return count


def queue_snapshot(user):
    """Download/render load is public to members; task metadata is owner-scoped.

    Tasks are ordered by submission time for display, not promised FIFO admission.
    Only explicit fields below cross the API boundary; never return raw job records.
    """
    limits = concurrency.settings()
    pools = {lane: {'running': 0, 'queued': 0, 'paused': 0,
                    'limit': min(limits[lane], limits['max_active_children']) if limits['enabled'] else 1}
             for lane in ('fetch', 'render')}
    tasks = []
    now = time.time()
    for job in sorted(stored_jobs(), key=lambda item: item.get('created', 0)):
        lane = concurrency.lane(job)
        if lane not in pools or job.get('status') not in {'queued', 'running'}:
            continue
        if job.get('instance') and not batch_control.belongs(job):
            continue
        bid = job.get('batch_id')
        path = workflow.BATCHES / bid if isinstance(bid, str) and re.fullmatch(r'[0-9a-f]{32}', bid) else None
        if bid and (path is None or not (path / 'draft.json').is_file()):
            continue
        control = batch_control.read(path) if path else {}
        paused = control.get('paused') or control.get('archived')
        status = 'paused' if paused else job['status']
        pools[lane][status] += 1
        execution_path = path
        if path and workflow.material_versions.internal(path):
            bid = workflow.material_versions.internal(path)['parent_batch_id']
            path = workflow.BATCHES / bid
        if user['role'] != 'admin' and (path is None or not accounts.allowed(path, user)):
            continue
        draft = read_json(path / 'draft.json') if path else {}
        name = draft.get('display_name') or draft.get('draft', {}).get('drama_title') or '未命名项目'
        reason = ('已归档，保留进度' if control.get('archived') else '已暂停，保留进度') if paused else (
            '等待资源重试' if job.get('not_before', 0) > now else '等待调度，按用户轮转分配空位') if status == 'queued' else ''
        task = {'id': job['id'], 'batch_id': bid, 'request_id': job.get('request_id'),
                'project_name': name, 'lane': lane, 'status': status,
                'created': job.get('created'), 'started': job.get('started'), 'wait_reason': reason}
        if job.get('created') is not None:
            task['queue_seconds'] = max(0, int((job.get('started') if status == 'running' and job.get('started') is not None else now) - job['created']))
        if status == 'running' and job.get('started') is not None:
            task['elapsed_seconds'] = max(0, int(now - job['started']))
        if path:
            from .progress import observation
            record = workflow.result_records(execution_path).get(job.get('request_id'), {}) if job.get('request_id') else read_json(execution_path / 'runtime.json')
            task['observation'] = observation(record, paused=bool(paused))
        if user['role'] == 'admin':
            task['owner'] = accounts.owner(path) if path else job.get('actor', 'admin')
        tasks.append(task)
    return {'pools': pools, 'tasks': tasks, 'scope': 'all' if user['role'] == 'admin' else 'own',
            'updated_at': now}


def asset_timing(request_id, jobs, *, now=None):
    """Wall time from preparation submission through the render worker's preview finish."""
    now = time.time() if now is None else now
    renders = [j for j in jobs if j.get('phase') == 'render' and j.get('request_id') == request_id]
    render = max(renders, key=lambda j: j.get('created', 0), default=None)
    parents = {j['id']: j for j in jobs}
    preparation = parents.get(render.get('parent_id')) if render else max(
        (j for j in jobs if j.get('phase') == 'fetch'), key=lambda j: j.get('created', 0), default=None)
    start = (preparation or render or {}).get('created')
    if start is None:
        return {}
    complete = bool(render and render.get('status') == 'completed')
    terminal = render or (preparation if preparation and preparation.get('status') in {'failed', 'interrupted'} else {})
    end = terminal.get('finished') or now
    timing = {'total_seconds': max(0, int(end - start)), 'complete': complete}
    if render and render.get('created') is not None:
        timing['queue_seconds'] = max(0, int((render.get('started') or end) - render['created']))
    return timing


def batch_response(batch_id, job=None):
    result = workflow.detail(batch_id)
    from .material_workers import parent_batch_id
    jobs = [item for item in stored_jobs() if item.get('batch_id') and parent_batch_id(item['batch_id']) == batch_id]
    media_state = workflow.material_versions.read(workflow.folder(batch_id)) if result.get('media_managed') else None
    for asset in result.get('assets', []):
        current_jobs = jobs
        if media_state:
            version = media_state['versions'].get(asset.get('version_id'), {})
            child_id = version.get('child_id') or (batch_id if version.get('legacy') else None)
            current_jobs = [j for j in jobs if child_id and j.get('batch_id') == child_id]
        asset['timing'] = asset_timing(asset['request_id'], current_jobs)
        if media_state and asset.get('candidate'):
            candidate = asset['candidate']
            child_id = media_state['versions'][candidate['version_id']]['child_id']
            candidate['timing'] = asset_timing(asset['request_id'], [j for j in jobs if j.get('batch_id') == child_id])
            candidate['timing']['complete'] = False  # A rendered pair is not current until its parent commit.
    if job is None:
        job = max(jobs, key=lambda j: j.get('created', 0), default=None)
    if result['runtime']['status'] == 'queued':
        from .source_prefetch import download_progress
        plan = result['runtime'].get('plan') or result.get('plan') or {}
        prefetch_jobs = [item for item in jobs if item.get('action') == 'prefetch'
                         and batch_control.belongs(item)]
        result['runtime'].update(download_progress(workflow.folder(batch_id), result['draft']['drama_id'],
                                                   plan.get('required', []), prefetch_jobs))
    if job:
        result['submission'] = {key: job.get(key) for key in ('id', 'action', 'status', 'created', 'request_id')}
        result['submission']['queue_position'] = job_response(job)['queue_position']
    return result


def prefetch_sources(batch_id, data, actor):
    from . import source_prefetch
    from .runtime import require_material_fetch
    path = workflow.folder(batch_id)
    source_prefetch.ensure_editable(path)
    if data.get('cancel') is True:
        write_json_atomic(path / 'source-prefetch.json', {'drama_id': '', 'episodes': []})
        return {'drama_id': '', 'episodes': [], 'timing_explanation': []}
    drama, episodes, manifest = source_prefetch.snapshot(data.get('draft'))
    # Index already-local frame sources even when external fetching is disabled.
    # Missing originals and legacy requests still require fetch authorization.
    if not episodes or any(ep not in manifest.frame_episodes or source_prefetch.signature(drama, ep) is None for ep in episodes):
        require_material_fetch()
    write_json_atomic(path / 'source-prefetch.json', {'drama_id': drama, 'episodes': episodes,
                                                    'frame_episodes': sorted(manifest.frame_episodes)})
    jobs = [job for job in stored_jobs() if job.get('batch_id') == batch_id
            and job.get('action') == 'prefetch' and (not job.get('instance') or batch_control.belongs(job))]
    result = source_prefetch.status(path, drama, episodes, jobs, manifest)
    missing = [e['episode'] for e in result['episodes'] if e['status'] == 'waiting'
               or (e['status'] == 'failed' and data.get('retry') is True)]
    if missing:
        job = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'project': drama, 'episodes': missing,
               'actor': actor, 'action': 'prefetch', 'created': time.time(), 'status': 'queued',
               'instance': instance_marker()}
        cmd = [sys.executable, '-u', '-m', 'exactcut.source_prefetch', batch_id, job['id'], instance_marker()]
        job['command'] = cmd
        save(job)
        WORK.put((job, cmd))
        for state in result['episodes']:
            if state['episode'] in missing:
                state.update(status='queued')
                state.pop('error_info', None)
        for timing in result['timing_explanation']:
            for segment in timing['segments']:
                if segment['episode'] in missing and segment.get('source_status') in {'waiting', 'failed'}:
                    segment['source_status'] = 'queued'
    return result


def existing_submission(batch_id, action, data, actor):
    token = data.get('submission_token')
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', token)):
        raise ValueError('无效提交标识，请刷新页面重试')
    payload = json_sha256({key: value for key, value in data.items() if key != 'submission_token'})
    for job in stored_jobs():
        if job.get('batch_id') != batch_id or job.get('action') != action or job.get('actor') != actor:
            continue
        if token and job.get('submission_token') == token:
            if job.get('submission_payload') != payload:
                raise ValueError('提交标识已用于其他内容，请刷新页面重试')
            return job
        if job.get('status') in ('queued', 'running') and (action == 'start' or job.get('request_id') == data.get('request_id')):
            return job
    return None


def stage_command(job, phase):
    command = [sys.executable, '-u', '-m', 'exactcut.workflow', phase, job['batch_id']]
    if phase in {'upload', 'poll'} or (phase == 'render' and job.get('request_id')):
        command.append(job['request_id'])
    return [*command, instance_marker()]


def prepare_staged_job(job):
    """Preserve public actions and submission tokens while selecting a short phase."""
    if job.get('action') == 'verify-delivery':
        job.update(phase='poll', lane='poll', command=stage_command(job, 'poll'))
        return job
    if not concurrency.settings()['enabled'] or not job.get('batch_id'):
        return job
    if job.get('action') == 'start':
        phase = 'fetch'
    elif job.get('action') == 'publish':
        path = workflow.folder(job['batch_id'])
        publications = read_json(path / 'publications.json')
        phase = 'poll' if publications.get(job.get('request_id'), {}).get('publication') else 'upload'
    else:
        return job
    job.update(phase=phase, lane=phase, command=stage_command(job, phase))
    return job


def advance_stage(job):
    """Persist deterministic successors before enqueue; repeated completion is safe."""
    phase = job.get('phase')
    path = workflow.BATCHES / job['batch_id']
    next_phase = 'render' if phase == 'fetch' else 'poll' if phase == 'upload' else None
    if phase == 'poll':
        publications = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
        if publications.get(job.get('request_id'), {}).get('status') == 'fbbm_queued':
            next_phase = 'poll'
    if next_phase is None:
        return
    interval = max(1, int(workflow.load_config()['publish']['poll_seconds']))
    if phase == 'poll':
        # A fresh process identity is assigned at the next launch.
        job.update(status='queued', not_before=time.time() + interval)
        for field in ('result_ref', 'input_fingerprint', 'admission'):
            job.pop(field, None)
        for field in ('pid', 'pgid', 'process_token', 'finished', 'exit_code'):
            job.pop(field, None)
        save(job)
        WORK.put((job, job['command']))
        return
    requests = [job.get('request_id')]
    if phase == 'fetch' and (path / 'confirmed.json').exists():
        legacy_id = uuid.uuid5(uuid.NAMESPACE_URL, job['id'] + ':render').hex
        if (JOBS / (legacy_id + '.json')).exists(): return
        requests = [request['request_id'] for request in read_json(path / 'confirmed.json')['requests']]
        workflow.update(path, request_parallel=True)
    for rid in requests:
        identity = job['id'] + ':' + next_phase
        if phase == 'fetch' and rid is not None: identity += ':' + rid
        successor_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        if (JOBS / (successor_id + '.json')).exists(): continue
        successor = {key: job[key] for key in ('batch_id', 'project', 'owner', 'actor', 'action', 'request_id', 'trace_id') if key in job}
        if rid is not None: successor['request_id'] = rid
        successor.update(id=successor_id, parent_id=job['id'], phase=next_phase, lane=next_phase,
                         created=time.time(), status='queued', instance=instance_marker())
        successor['command'] = stage_command(successor, next_phase)
        if next_phase == 'poll': successor['not_before'] = time.time() + interval
        save(successor)
        WORK.put((successor, successor['command']))


def delivery_uncertain(job):
    if job.get('phase') not in {'upload', 'poll'}: return False
    file = workflow.BATCHES / job.get('batch_id', '') / 'publications.json'
    entry = read_json(file).get(job.get('request_id'), {}) if file.exists() else {}
    return workflow.delivery_blocked(entry)


def requeue_stage(job):
    job.update(status='queued', recovered_from_stage=job['phase'])
    for field in ('pid', 'pgid', 'process_token', 'admission', 'input_fingerprint', 'result_ref'): job.pop(field, None)
    storage.release(job['id'], JOBS); save(job)
    if job.get('batch_id'):
        path = workflow.BATCHES / job['batch_id']
        workflow.end_execution(path, job.get('request_id'))
        workflow.update(path, 'queued', '执行中断，等待继续本阶段')


def resolve_verified_delivery(job, result):
    """Retain uncertain attempts as audited terminal history after a bound read-only proof."""
    if (job.get('action') != 'verify-delivery' or job.get('phase') != 'poll' or not result
            or result.get('status') != 'done' or result.get('exit_code') != 0): return
    entry = read_json(workflow.BATCHES / job['batch_id'] / 'publications.json').get(job['request_id'], {})
    if (entry.get('status') != 'fbbm_ready' or entry.get('intent', {}).get('status') != 'verified'
            or result.get('publication') != entry or not job.get('verification_only')): return
    try:
        binding = workflow.verification_binding(entry)
        if binding != job['verification_only']: return
    except (ValueError, TypeError, AttributeError): return
    for original in stored_jobs():
        if (original['id'] == job['id'] or original.get('batch_id') != job['batch_id']
                or original.get('request_id') != job['request_id'] or original.get('status') != 'unverified'
                or original.get('phase') not in {'upload', 'poll'} or job_alive(original)): continue
        try:
            evidence_path = stage_results.result_path(original, JOBS)
            if evidence_path.exists():
                evidence = read_json(evidence_path)
                if (evidence.get('job_id') != original['id'] or evidence.get('phase') != original['phase']
                        or evidence.get('attempt') != original.get('attempt') or evidence.get('status') != 'unverified'
                        or evidence.get('evidence_sha256') != json_sha256({key: value for key, value in evidence.items() if key != 'evidence_sha256'})
                        or workflow.verification_binding(evidence.get('publication', {})) != binding): continue
            elif original.get('action') == 'verify-delivery' and original.get('verification_only') == binding:
                # A local launcher failure has no child result, but already saved the exact read-only identity.
                pass
            else:
                # A child may die after its receipt but before writing its result. Bind the
                # serialized attempt to its admitted inputs AND an original historical receipt;
                # current ready state or a newly supplied material ID alone is insufficient.
                fingerprint = original.get('input_fingerprint')
                if (not original.get('attempt') or not isinstance(fingerprint, str)
                        or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                        or fingerprint != stage_results.fingerprint(original, workflow.BATCHES, workflow.ROOT)): continue
                matched = False
                for intent in entry.get('verification_history', []):
                    if intent.get('phase') != original['phase'] or intent.get('status') != 'pending': continue
                    receipt = intent.get('receipt') or {}
                    if not receipt.get('material_id') or not (receipt.get('record_id') or receipt.get('feishu_record_id')): continue
                    if workflow.verification_binding({'intent': intent, 'publication': entry.get('publication')}) == binding:
                        matched = True
                        break
                if not matched: continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError): continue
        original.update(status='interrupted', resolved_by=job['id'], resolved_at=workflow.now_iso(),
                        resolution='verified_delivery')
        save(original)


def current_managed_verification(job, *, local_failure=False):
    """Late callbacks cannot finish a replaced managed verification or attempt."""
    if job.get('action') != 'verify-delivery' or not job.get('batch_id'): return True
    m = workflow.material_versions
    child = workflow.BATCHES / job['batch_id']
    identity = m.internal(child)
    if not identity: return True  # Legacy verification keeps its existing contract.
    state = m.read(workflow.BATCHES / identity['parent_batch_id'])
    event = state['outbox'].get(job['id'])
    if (identity.get('kind') != 'delivery' or not event or event.get('kind') != 'verification'
            or event.get('status') == 'superseded' or event.get('child_id') != child.name
            or event.get('delivery_id') != identity.get('delivery_id')
            or event.get('asset_id') != job.get('request_id')
            or event.get('version_id') != identity.get('version_id')
            or event.get('binding') != job.get('verification_only')):
        return False
    file = JOBS / (job['id'] + '.json')
    if not file.exists(): return False
    actual = read_json(file)
    if any(actual.get(key) != job.get(key) for key in
           ('id', 'batch_id', 'request_id', 'phase', 'action', 'verification_only')):
        return False
    if all(actual.get(key) == job.get(key) for key in ('attempt', 'process_token')): return True
    # Admission increments attempt before opening the log / saving the launch.
    # A known local failure there has no process; retain normal failure handling
    # without accepting a callback older than the durable attempt.
    return (local_failure and actual.get('status') == job.get('status') == 'queued'
            and job.get('attempt') == actual.get('attempt', 0) + 1)


def finish_job(job, code=None, message='执行进程已结束，可查看日志后重试', *, local_failure=False, scheduler=False):
    # Lock-free authoritative snapshots; do not nest the media lock inside the
    # scheduler's legacy locks or hold it while validating stage evidence.
    if not current_managed_verification(job, local_failure=local_failure): return
    verify = stage_results.scheduler_verified if scheduler else stage_results.verified
    result = verify(job, JOBS, workflow.BATCHES, workflow.ROOT) if job.get('attempt') else None
    if result and result['status'] == 'waiting_resource':
        job['result_ref'] = str(stage_results.result_path(job, JOBS))
        job.update(status='queued', not_before=result['not_before'])
        for field in ('pid', 'pgid', 'process_token', 'admission', 'input_fingerprint'): job.pop(field, None)
        storage.release(job['id'], JOBS)
        save(job)
        if job.get('batch_id') and job.get('action') != 'prefetch':
            path = workflow.BATCHES / job['batch_id']
            workflow.end_execution(path, job.get('request_id'))
            workflow.update(path, 'queued', '等待资源，稍后继续本阶段', not_before=result['not_before'])
        return
    if job.get('phase') and job.get('attempt'):
        if result:
            job['result_ref'] = str(stage_results.result_path(job, JOBS))
            code = result['exit_code']
            if result['status'] == 'unverified': code = 2
        elif not local_failure and not delivery_uncertain(job):
            requeue_stage(job)
            return
        elif code == 0:
            code = 2
            message = '阶段结果缺失或校验失败，请重新执行本阶段'
    storage.release(job['id'], JOBS)
    job.update(status='interrupted' if code is None else 'completed' if code == 0 else 'failed',
               exit_code=code, finished=time.time())
    save(job)
    if not job.get('batch_id'):
        return
    path = workflow.BATCHES / job['batch_id']
    if not (path / 'draft.json').exists():
        return
    if job.get('action') == 'prefetch':
        return
    if job.get('phase'):
        workflow.end_execution(path, job.get('request_id'))
        if code == 0:
            advance_stage(job)
            resolve_verified_delivery(job, result)
        elif job.get('phase') in {'upload', 'poll'}:
            publications = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
            rid = job.get('request_id')
            entry = publications.get(rid, {})
            if workflow.delivery_blocked(entry) or entry.get('verification_only') or entry.get('verification_history'):
                entry.update(status='unverified', error=entry.get('error') or '待核实：交付阶段中断，只能核实原回执')
                publications = workflow.store_publication(path, rid, entry)
                job.update(status='unverified'); save(job)
            elif entry.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'}:
                entry.update(status='publish_failed', error=message)
                publications = workflow.store_publication(path, rid, entry)
            waiting = any(item.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'} for item in publications.values())
            workflow.update(path, 'publishing' if waiting else 'review', message)
        elif job.get('phase') == 'render' and job.get('request_id'):
            record = workflow.result_records(path).get(job['request_id'], {})
            workflow.end_execution(path, job['request_id'], status='failed', error=record.get('error') or message)
        elif workflow.runtime(path)['status'] in workflow.ACTIVE:
            workflow.update(path, 'interrupted', message)
        return
    workflow.end_execution(path, job.get('request_id'))
    if job.get('action') == 'publish' and code != 0:
        command = job.get('command', [])
        rid = job.get('request_id') or (command[-2] if command and command[-1].startswith('--instance=') else command[-1] if command else None)
        publications = workflow.read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
        entry = publications.get(rid, {})
        if entry.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'}:
            entry.update(status='publish_failed', error=message)
            publications = workflow.store_publication(path, rid, entry)
            pending = any(e.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'} for e in publications.values())
            workflow.update(path, 'publishing' if pending else 'review', message)
    elif workflow.runtime(path)['status'] in workflow.ACTIVE:
        workflow.update(path, 'interrupted', message)


def job_alive(job):
    if not batch_control.belongs(job):
        return False
    return batch_control.child_state(job) not in {None, 'Z'}


def recover_jobs(*, enqueue=False, owned=None, scheduler=False):
    verify = stage_results.scheduler_verified if scheduler else stage_results.verified
    jobs = list(stored_jobs())
    workflow.recover_verification_queue(jobs)
    owned = owned or {}
    live_ids = set(owned)
    live = {job.get('batch_id') for job, _, _ in owned.values()}
    live.update(path.name for path in workflow.BATCHES.glob('*') if batch_control.own_live_children(path))
    queued = set()
    evidence_waiting = set()
    for job in sorted(jobs, key=lambda j: j.get('created', 0)):
        try:
            if job.get('instance') and not batch_control.belongs(job):
                continue
            batch = job.get('batch_id')
            if job.get('status') == 'waiting_resource':
                # Retain the persisted due time; resource waits are reentrant,
                # and must never be treated as an orphaned verification handoff.
                job['status'] = 'queued'
                save(job)
            if job['id'] in owned:
                if batch and (workflow.BATCHES / batch / 'draft.json').exists():
                    path = workflow.BATCHES / batch
                    pid, token = batch_control.resident_identity(job, owned[job['id']][1])
                    recorded = batch_control.recorded_children(path)
                    if not any(child.get('pid') == pid and child.get('process_token') == token for child in recorded):
                        child = {**job, 'pid': pid, 'pgid': pid, 'process_token': token}
                        if batch_control.child_state(child) not in {None, 'Z'}:
                            batch_control.remember_child(path, {**child, 'lane': concurrency.lane(job)})
                continue
            if job.get('status') == 'completed' and job.get('phase') in {'fetch', 'upload'}:
                if job['phase'] == 'upload':
                    successor_id = uuid.uuid5(uuid.NAMESPACE_URL, job['id'] + ':poll').hex
                    if (JOBS / (successor_id + '.json')).exists(): continue
                else:
                    successor_id = uuid.uuid5(uuid.NAMESPACE_URL, job['id'] + ':render').hex
                    if (JOBS / (successor_id + '.json')).exists(): continue
                    manifest = workflow.BATCHES / batch / 'confirmed.json' if batch else None
                    if manifest and manifest.exists():
                        ids = [uuid.uuid5(uuid.NAMESPACE_URL, job['id'] + ':render:' + r['request_id']).hex
                               for r in read_json(manifest)['requests']]
                        if ids and all((JOBS / (identity + '.json')).exists() for identity in ids): continue
                if not job.get('attempt') or verify(job, JOBS, workflow.BATCHES, workflow.ROOT):
                    advance_stage(job)
                else:
                    message = '阶段结果缺失或校验失败，禁止无证据推进或重放交付'
                    job['error'] = message
                    finish_job(job, 2, message, local_failure=True, scheduler=scheduler)
                continue
            if job.get('status') == 'queued' and job.get('command'):
                if batch and not (workflow.BATCHES / batch / 'draft.json').exists():
                    finish_job(job, scheduler=scheduler)
                    continue
                children = batch_control.own_live_children(workflow.BATCHES / batch) if batch else []
                token = job.get('process_token')
                child = next((c for c in children if c.get('process_token') == token), None)
                if child is None and batch_control.valid_token(token):
                    found = batch_control.PROCESSES.find(token)
                    if found:
                        candidate = {**found, 'process_token': token, 'instance': instance_marker(),
                                     'lane': concurrency.lane(job), 'request_id': job.get('request_id')}
                        if batch_control.child_state(candidate) not in {None, 'Z'}:
                            child = candidate
                            if batch:
                                batch_control.remember_child(workflow.BATCHES / batch, child)
                if child:
                    job.update(status='running', adopted_after_restart=True, instance=child.get('instance'),
                               pid=child['pid'], pgid=child.get('pgid'), process_token=child['process_token'])
                    save(job)
                    live.add(batch); live_ids.add(job['id'])
                    continue
                if job.get('attempt') and job.get('process_token'):
                    result = verify(job, JOBS, workflow.BATCHES, workflow.ROOT)
                    if result:
                        finish_job(job, result['exit_code'], scheduler=scheduler)
                        if job.get('status') == 'queued': queued.add(batch)
                        continue
                queued.add(batch)
                if enqueue:
                    WORK.put((job, job['command']))
            elif job.get('status') == 'running' and job_alive(job):
                job.update(adopted_after_restart=True)
                save(job)
                if batch and (workflow.BATCHES / batch / 'draft.json').exists():
                    batch_control.remember_child(workflow.BATCHES / batch, {**job, 'lane': concurrency.lane(job)})
                live.add(batch); live_ids.add(job['id'])
                continue
            elif job.get('status') == 'running':
                if job.get('phase'):
                    result = verify(job, JOBS, workflow.BATCHES, workflow.ROOT)
                    if result:
                        finish_job(job, result['exit_code'], scheduler=scheduler)
                        if job.get('status') == 'queued': queued.add(batch)
                        continue
                    path = workflow.BATCHES / batch if batch else None
                    if job.get('phase') in {'upload', 'poll'}:
                        entry = read_json(path / 'publications.json').get(job.get('request_id'), {}) if path and (path / 'publications.json').exists() else {}
                        if workflow.delivery_blocked(entry):
                            finish_job(job, 2, '待核实：交付执行中断', scheduler=scheduler); continue
                        if job.get('attempt'):
                            requeue_stage(job); queued.add(batch)
                            if enqueue: WORK.put((job, job['command']))
                            continue
                    if job.get('phase') in {'fetch', 'render'} and (job.get('attempt') or not batch or not workflow.runtime(path).get('download_complete')):
                        job.update(status='queued', recovered_from_stage=job['phase'])
                        for field in ('pid', 'pgid', 'process_token', 'admission', 'input_fingerprint'): job.pop(field, None)
                        storage.release(job['id'], JOBS); save(job); queued.add(batch)
                        if batch:
                            workflow.end_execution(path, job.get('request_id'))
                            workflow.update(path, 'queued', '执行中断，等待继续本阶段')
                        if enqueue: WORK.put((job, job['command']))
                        continue
                if job.get('attempt') and not job.get('phase'):
                    result = verify(job, JOBS, workflow.BATCHES, workflow.ROOT)
                    if result and result['status'] == 'waiting_resource':
                        finish_job(job, result['exit_code'], scheduler=scheduler)
                        queued.add(batch)
                        continue
                code = None
                if batch and (workflow.BATCHES / batch / 'runtime.json').exists():
                    state = workflow.runtime(workflow.BATCHES / batch)['status']
                    if job.get('phase') == 'fetch':
                        code = 0 if workflow.runtime(workflow.BATCHES / batch).get('download_complete') else 2 if state == 'failed' else None
                    elif job.get('phase') in {'upload', 'poll'}:
                        file = workflow.BATCHES / batch / 'publications.json'
                        entries = read_json(file) if file.exists() else {}
                        status = entries.get(job.get('request_id'), {}).get('status')
                        code = 0 if status in {'fbbm_queued', 'fbbm_ready'} else 2 if status == 'publish_failed' else None
                    elif job.get('action') == 'start':
                        code = 0 if state in {'review', 'complete'} else 2 if state in {'partial', 'failed'} else None
                    elif job.get('action') == 'publish':
                        path = workflow.BATCHES / batch / 'publications.json'
                        publications = workflow.read_json(path) if path.exists() else {}
                        status = publications.get(job.get('request_id'), {}).get('status')
                        code = 0 if status == 'fbbm_ready' else 2 if status == 'publish_failed' else None
                    elif job.get('action') == 'prefetch':
                        from .source_prefetch import result_path
                        file = result_path(workflow.folder(batch), job['id'])
                        result = read_json(file) if file.exists() else {}
                        code = 0 if result.get('status') == 'completed' else 2 if result.get('status') == 'failed' else None
                finish_job(job, code, scheduler=scheduler)
        except stage_results.EvidencePending:
            if job.get('batch_id'):
                live.add(job['batch_id']); evidence_waiting.add(concurrency.scope(job))
            continue
    storage.rebuild_reservations(JOBS, live_ids, workflow.BATCHES, workflow.ROOT, inline_ids=INLINE_RENDERS, scheduler=scheduler)
    # Stage completion may have queued a successor after the recovery snapshot.
    latest_jobs = list(stored_jobs())
    queued.update(job.get('batch_id') for job in latest_jobs if job.get('status') == 'queued')
    for file in workflow.BATCHES.glob('*/runtime.json'):
        path = file.parent
        if workflow.material_versions.managed(path): continue
        if path.name in live or path.name in queued:
            continue
        if workflow.runtime(path, jobs=latest_jobs)['status'] in workflow.ACTIVE:
            # Includes a crash between confirming a batch and persisting its job.
            publications = workflow.read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
            for rid, entry in publications.items():
                if entry.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'}:
                    workflow.store_publication(path, rid, {**entry, 'status': 'publish_failed', 'error': '执行中断，可重试继续原记录'})
            workflow.update(path, 'interrupted', '执行已中断，可重试制作或继续上传')
            control = batch_control.read(path)
            control.update(paused=False, suspended=[])
            batch_control.write(path, control)

    return evidence_waiting


def fair_pending(pending):
    """Owner FIFO with a durable round-robin cursor across worker ticks."""
    def owner_for(entry):
        job = entry[0]
        if job.get('batch_id'):
            try:
                path = workflow.folder(job['batch_id'])
            except ValueError:
                # Old queued records can outlive a deleted batch in read responses.
                pass
            else:
                return accounts.owner(path)
        return job.get('owner') or job.get('actor') or 'admin'
    entries = sorted(pending, key=lambda item: (item[0].get('created', float('inf')), item[0]['id']))
    yield from concurrency.owner_round_robin(entries, owner_for, JOBS.parent / 'scheduler.json')


def job_response(job):
    """Queue rank is fair traversal priority, including currently blocked jobs."""
    position = None
    if job.get('status') == 'queued':
        pending = [(item, item.get('command')) for item in stored_jobs() if item.get('status') == 'queued']
        position = next((index for index, (_, entry) in enumerate(fair_pending(pending), 1)
                         if entry[0]['id'] == job['id']), None)
    return {**job, 'queue_position': position}


def resource_usage(owned=None):
    """Background job budgets exclude the independently bounded HTTP previews."""
    owned = owned or {}
    counts = {key: 0 for key in concurrency.LANES}
    batches = set()
    legacy_projects = {job.get('project') for job, _, _ in owned.values() if not job.get('batch_id') and job.get('project')}
    residents = set()
    for job, process, _ in owned.values():
        identity = batch_control.resident_identity(job, process)
        if identity not in residents:
            counts[concurrency.lane(job)] += 1
        residents.add(identity)
        if job.get('batch_id'):
            batches.add(concurrency.scope(job))
    for path in workflow.BATCHES.glob('*'):
        for child in batch_control.own_live_children(path):
            batches.add(concurrency.scope(child, path.name))
            identity = batch_control.resident_identity(child)
            if identity not in residents:
                counts[concurrency.lane(child)] += 1
            residents.add(identity)
    for job in stored_jobs():
        if job.get('status') == 'running' and not job.get('batch_id') and job_alive(job):
            if job.get('project'): legacy_projects.add(job['project'])
            identity = batch_control.resident_identity(job)
            if identity not in residents:
                counts[concurrency.lane(job)] += 1
            residents.add(identity)
    return counts, residents, batches, legacy_projects


def project_render_usage(owned=None):
    counts, residents = {}, set()
    for job, process, _ in (owned or {}).values():
        batch = job.get('batch_id')
        if not batch or concurrency.lane(job) != 'render': continue
        identity = batch_control.resident_identity(job, process)
        if identity not in residents: counts[batch] = counts.get(batch, 0) + 1
        residents.add(identity)
    for path in workflow.BATCHES.glob('*'):
        for child in batch_control.own_live_children(path):
            if concurrency.lane(child) != 'render': continue
            identity = batch_control.resident_identity(child)
            if identity not in residents: counts[path.name] = counts.get(path.name, 0) + 1
            residents.add(identity)
    return counts


def admit_hook_preview(project):
    """Reserve a dispatched Hook worker's temporary disk, without a submit quota."""
    from .util import load_config
    with LOCK, batch_control.LOCK:
        token = 'inline-' + uuid.uuid4().hex
        # Up to sixteen sampled frames, full-resolution overlays and copied JSON decisions.
        render = load_config()['render']
        peak = max(64 * 1024**2, int(render['width'] * render['height']) * 16 * 4)
        cache_bytes = sum(p.stat().st_size for name in ('hook-emphasis', 'hook-placement')
                          for p in (project / 'cache' / name).glob('*.json') if not p.is_symlink())
        paths = {str(project): peak, str(Path(tempfile.gettempdir())): peak + cache_bytes}
        if not storage.reserve(token, paths, JOBS):
            diag.event('hook_preview.admission_denied', level='warning', reason='disk_reservation',
                       active_previews=len(INLINE_RENDERS))
            raise stage_results.ResourceDeferred(time.time() + 1, '预览磁盘资源不足，请稍后重试')
        INLINE_RENDERS.add(token)
        diag.event('hook_preview.admitted', active_previews=len(INLINE_RENDERS))
        return token


def release_hook_preview(token):
    with LOCK:
        INLINE_RENDERS.discard(token)
        try:
            storage.release(token, JOBS)
        except (OSError, ValueError, RuntimeError) as exc:
            # Recovery drops orphaned inline reservations on the next worker tick.
            diag.event('hook_preview.cleanup_failed', level='warning', error=exc)
        diag.event('hook_preview.released', active_previews=len(INLINE_RENDERS))


def worker_tick(pending, owned):
    with lock_timing.phases() as timing:
        timing.phase('recover_outbox')
        workflow.material_versions.recover_outbox()
        timing.phase('drain')
        while True:
            try:
                entry = WORK.get_nowait()
                if any(item[0]['id'] == entry[0]['id'] for item in pending) or entry[0]['id'] in owned:
                    WORK.task_done()
                else:
                    pending.append(entry)
            except queue.Empty:
                break
        timing.phase('lock_wait')
        with LOCK, batch_control.LOCK:
            with timing.holding():
                for key, (job, process, log) in list(owned.items()):
                    code = process.poll()
                    if code is None:
                        continue
                    try:
                        if job.get('attempt'): stage_results.scheduler_verified(job, JOBS, workflow.BATCHES, workflow.ROOT)
                    except stage_results.EvidencePending:
                        # The executor has exited: evidence waiting consumes no execution slot.
                        log.close(); storage.release(job['id'], JOBS)
                        del owned[key]; WORK.task_done()
                        continue
                    try:
                        log.close()
                        finish_job(job, code, scheduler=True)
                    finally:
                        del owned[key]
                        WORK.task_done()
                timing.phase('recover_jobs')
                for entry in list(pending):
                    job, _ = entry
                    if job.get('batch_id') and not (workflow.BATCHES / job['batch_id'] / 'draft.json').exists():
                        pending.remove(entry)
                        WORK.task_done()
                # Adoption must register crash-window roots before any SIGCONT decision.
                evidence_waiting = recover_jobs(owned=owned, scheduler=True)
                timing.phase('external')
                external_active = {key: 0 for key in concurrency.LANES}
                external_identities = set()
                for job, process, _ in owned.values():
                    if not job.get('batch_id'):
                        identity = batch_control.resident_identity(job, process)
                        if identity not in external_identities:
                            external_active[concurrency.lane(job)] += 1
                        external_identities.add(identity)
                for job in stored_jobs():
                    if job.get('status') == 'running' and not job.get('batch_id') and job_alive(job):
                        identity = batch_control.resident_identity(job)
                        if identity not in external_identities:
                            external_active[concurrency.lane(job)] += 1
                        external_identities.add(identity)
                timing.phase('reconcile')
                if any(external_active.values()):
                    batch_control.reconcile(workflow.BATCHES, external_active=external_active,
                                            cursor_file=JOBS.parent / 'scheduler.json')
                else:
                    batch_control.reconcile(workflow.BATCHES, cursor_file=JOBS.parent / 'scheduler.json')
                timing.phase('durable_scan')
                while True:
                    try:
                        entry = WORK.get_nowait()
                        if any(item[0]['id'] == entry[0]['id'] for item in pending) or entry[0]['id'] in owned:
                            WORK.task_done()
                        else: pending.append(entry)
                    except queue.Empty: break
                known = {entry[0]['id'] for entry in pending} | set(owned)
                for durable in stored_jobs():
                    if durable.get('status') == 'queued' and durable.get('command') and durable['id'] not in known:
                        WORK.put((durable, durable['command']))
                        pending.append(WORK.get_nowait())
                        known.add(durable['id'])
                timing.phase('resource_usage')
                limits = concurrency.settings()
                counts, tokens, batches, legacy_projects = resource_usage(owned)
                diag.throttled('scheduler.snapshot', 'worker', pending=len(pending), running=len(owned),
                               pools=counts, limits=limits, resident_count=len(tokens), active_previews=len(INLINE_RENDERS),
                               oldest_wait_seconds=round(max((time.time()-e[0].get('created', time.time()) for e in pending), default=0)))
                batches.update(evidence_waiting)
                timing.phase('dispatch')
                for owner, entry in fair_pending(pending):
                    job, cmd = entry
                    file = JOBS / (job['id'] + '.json')
                    if file.exists():
                        job = read_json(file)
                        cmd = job.get('command', cmd)
                    if job.get('status') != 'queued':
                        pending.remove(entry)
                        WORK.task_done()
                        continue
                    if job.get('not_before', 0) > time.time():
                        log_wait(job, 'retry_backoff', not_before=job['not_before'])
                        continue
                    if (job.get('phase') in {'render', 'media-upload'} and job.get('attempt') and job.get('batch_id')
                            and workflow.material_versions.internal(workflow.BATCHES / job['batch_id'])):
                        continue  # Pre-tick recovery gives each render attempt a fresh container.
                    resource = concurrency.lane(job)
                    if counts[resource] >= limits[resource] or len(tokens) >= limits['max_active_children']:
                        log_wait(job, 'pool_full', pools=counts, resident_count=len(tokens))
                        continue
                    if not limits['enabled'] and sum(counts.values()):
                        log_wait(job, 'serial_mode')
                        continue
                    batch = job.get('batch_id')
                    if not batch and job.get('project') in legacy_projects: continue
                    if batch:
                        control = batch_control.read(workflow.folder(batch))
                        if control.get('paused') or control.get('archived') or concurrency.scope_blocked(concurrency.scope(job), batches):
                            log_wait(job, 'paused' if control.get('paused') else 'archived' if control.get('archived') else 'dependency_active')
                            continue
                    try:
                        fingerprint = stage_results.scheduler_fingerprint(job, workflow.BATCHES, workflow.ROOT, jobs=JOBS)
                    except stage_results.EvidencePending:
                        log_wait(job, 'evidence_pending')
                        continue
                    except (ValueError, RuntimeError, OSError) as exc:
                        finish_job(job, -1, str(exc), local_failure=True, scheduler=True); pending.remove(entry); WORK.task_done(); continue
                    try:
                        allocations = storage.scheduler_job_budget(job, workflow.BATCHES, workflow.ROOT)
                        if not storage.reserve(job['id'], allocations, JOBS):
                            log_wait(job, 'disk_reservation', requested_bytes=sum(allocations.values()))
                            continue
                        job['admission'] = {'paths': allocations, 'allocations': storage.device_allocations(allocations), 'at': time.time()}
                        job['attempt'] = job.get('attempt', 0) + 1
                        job['input_fingerprint'] = fingerprint
                    except stage_results.EvidencePending:
                        continue
                    except (ValueError, RuntimeError, OSError) as exc:
                        finish_job(job, -1, str(exc), local_failure=True, scheduler=True); pending.remove(entry); WORK.task_done(); continue
                    log = None
                    try:
                        log = (JOBS / (job['id'] + '.log')).open('ab')
                        token = uuid.uuid4().hex
                        if len(cmd) > 2 and cmd[1] == '-X' and cmd[2].startswith('exactcut_process_token='):
                            cmd = [cmd[0], *cmd[3:]]
                        cmd = [cmd[0], '-X', 'exactcut_process_token=' + token, *cmd[1:]]
                        job.update(process_token=token, command=cmd, instance=instance_marker(), lane=resource)
                        save(job)
                        process = subprocess.Popen(cmd, cwd=CODE_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    except Exception as exc:
                        diag.event('job.spawn_failed', level='error', job_id=job['id'], batch_id=batch, error=exc)
                        if log is not None: log.close()
                        # No process was spawned; this is a known local failure.
                        finish_job(job, -1, '启动失败，可重试：' + type(exc).__name__, local_failure=True, scheduler=True)
                        pending.remove(entry)
                        WORK.task_done()
                    else:
                        identity = batch_control.identify(process, process_token=token,
                                                         lane=resource, request_id=job.get('request_id'))
                        owned[job['id']] = (job, process, log)
                        pending.remove(entry)
                        job.update(status='running', started=time.time(), instance=instance_marker())
                        if identity:
                            job.update(pid=identity['pid'], pgid=identity['pgid'])
                        save(job)
                        diag.event('job.started', job_id=job['id'], batch_id=batch, request_id=job.get('request_id'),
                                   trace_id=job.get('trace_id'), phase=job.get('phase'), attempt=job.get('attempt'),
                                   child_pid=process.pid, queue_seconds=round(time.time()-job.get('created', time.time()), 3))
                        if identity and batch:
                            batch_control.remember_child(workflow.folder(batch), identity)
                        counts[resource] += 1
                        tokens.add(batch_control.resident_identity(job, process))
                        if batch:
                            batches.add(concurrency.scope(job))
                        elif job.get('project'):
                            legacy_projects.add(job['project'])
                        write_json_atomic(JOBS.parent / 'scheduler.json', {'owner': owner})


def log_wait(job, reason, **fields):
    diag.throttled('job.waiting', (job['id'], reason), job_id=job['id'], batch_id=job.get('batch_id'),
                   request_id=job.get('request_id'), trace_id=job.get('trace_id'), lane=concurrency.lane(job),
                   reason=reason, queue_seconds=round(time.time()-job.get('created', time.time()), 3), **fields)


def worker():
    global WORKER_HEARTBEAT, WORKER_FAILURES
    pending, owned = [], {}
    while True:
        WORKER_HEARTBEAT = time.monotonic()
        try:
            worker_tick(pending, owned)
            WORKER_FAILURES = 0
        except Exception:
            WORKER_FAILURES += 1
            logging.exception('Runner tick failed; retrying without dropping queued work')
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        self.trace_id = uuid.uuid4().hex
        self.diagnostic_status = None
        self.diagnostic_route = None
        self.user = None
        started = time.monotonic()
        with diag.context(trace_id=self.trace_id), lock_timing.observe() as lock_observation:
            try:
                return super().handle_one_request()
            except Exception as exc:
                diag.event('http.failed', level='error', route=self.diagnostic_route,
                           method=getattr(self, 'command', None), status=self.diagnostic_status,
                           duration_ms=round((time.monotonic()-started)*1000), error=exc,
                           **lock_observation.fields())
                raise
            finally:
                if (self.diagnostic_route and self.diagnostic_route != '/healthz'
                        and not (self.diagnostic_route == '/api/client-events' and (self.diagnostic_status or 500) < 400)):
                    fields = dict(route=self.diagnostic_route, method=self.command, status=self.diagnostic_status,
                                  actor=(self.user or {}).get('username'), duration_ms=round((time.monotonic()-started)*1000))
                    fields.update(lock_observation.fields())
                    # Polls are sampled; failures, writes and slow responses always retained.
                    if self.command == 'GET' and (self.diagnostic_status or 500) < 400 and fields['duration_ms'] < 2000:
                        diag.throttled('http.completed', (fields['route'], fields['actor']), interval=30, **fields)
                    else:
                        diag.event('http.completed', **fields)

    def parse_request(self):
        valid = super().parse_request()
        if valid:
            route = self.path.partition('?')[0]
            prefix = getattr(self.server, 'base_path', '')
            if prefix and route.startswith(prefix + '/'):
                route = route[len(prefix):]
            self.diagnostic_route = route if re.fullmatch(r'/[a-zA-Z0-9_./-]{0,240}', route) else '/unrecognized'
            lock_timing.set_owner(lock_timing.route_owner(self.command, self.diagnostic_route))
            client_id = self.headers.get('X-Client-Request-ID', '')
            if re.fullmatch(r'[0-9a-f]{32}', client_id):
                diag.bind(client_request_id=client_id)
            batch = re.match(r'/api/batches/([0-9a-f]{32})(?:/|$)', route)
            if batch:
                diag.bind(batch_id=batch[1])
                try:
                    document = read_json(workflow.folder(batch[1]) / 'draft.json') or {}
                    inner = document.get('draft') if isinstance(document.get('draft'), dict) else {}
                    name = document.get('display_name') or inner.get('drama_title')
                    if isinstance(name, str) and name.strip():
                        diag.bind(project_name=name)
                except Exception:
                    pass  # Optional diagnostics must never affect request handling.
            if self.command != 'GET' and self.diagnostic_route != '/api/client-events':
                diag.event('http.started', route=self.diagnostic_route, method=self.command)
        return valid

    def send_response(self, code, message=None):
        self.diagnostic_status = code
        super().send_response(code, message)
        if getattr(self, 'trace_id', None):
            self.send_header('X-Request-ID', self.trace_id)

    def public_url(self, path):
        return getattr(getattr(self, 'server', None), 'base_path', '') + path

    def normalize_path(self):
        self.query = parse_qs(self.path.partition('?')[2])
        self.asset_version = self.query.get('v', [''])[0]
        self.path = self.path.partition('?')[0]
        prefix = getattr(self.server, 'base_path', '')
        if prefix and self.path.startswith(prefix + '/'):
            self.path = self.path[len(prefix):]

    def web_page(self, name):
        body = (CODE_ROOT / 'exactcut/web' / name).read_bytes()
        if name == 'index.html':
            body = body.replace(b'<script src="app.js">', b'<script src="diagnostics.js"></script><script src="app.js">')
        if name == 'index.html':
            body = body.replace(b'<script src="cutting.js">', b'<script src="editor-source.js"></script><script src="cutting.js">')
            for script in ('app.js', 'helper.js', 'diagnostics.js', 'editor-source.js', 'editor-busy.js', 'cutting.js', 'mosaic.js', 'preview-subtitles.js'):
                version = hashlib.sha256((CODE_ROOT / 'exactcut/web' / script).read_bytes()).hexdigest()[:20]
                body = body.replace(f'src="{script}"'.encode(), f'src="{script}?v={version}"'.encode())
            app_version = hashlib.sha256((CODE_ROOT / 'exactcut/web/app.js').read_bytes()).hexdigest()[:20]
            image_version = hashlib.sha256((CODE_ROOT / 'exactcut/web/brand-style.jpg').read_bytes()).hexdigest()[:20]
            body = body.replace(b'src="brand-style.jpg"', f'src="brand-style.jpg?v={image_version}"'.encode())
            body = body.replace(b'<script src="diagnostics.js',
                                f'<script data-app-version="{app_version}" src="diagnostics.js'.encode())
        base = f'<base href="{self.public_url("/")}">'.encode()
        return body.replace(b'<meta', base + b'<meta', 1)

    def log_message(self, fmt, *args):
        # Do not log credentials, query strings or request bodies.
        pass

    def script(self, name):
        body = (CODE_ROOT / 'exactcut/web' / name).read_bytes()
        version = hashlib.sha256(body).hexdigest()[:20]
        cache = 'private, max-age=31536000, immutable' if self.asset_version == version else 'no-store'
        # Compress only static code: never mix account data or secrets into compression.
        accepted = self.headers.get('Accept-Encoding', '').lower().split(',')
        compressed = any(re.fullmatch(r'\s*gzip\s*(?:;\s*q=(?:1(?:\.0*)?|0\.0*[1-9]\d*))?\s*', item) for item in accepted)
        return self.respond(200, gzip.compress(body, mtime=0) if compressed else body,
                            'text/javascript; charset=utf-8', cache=cache, encoding='gzip' if compressed else None, vary=True)

    def respond(self, code, body, kind='application/json; charset=utf-8', *, cache='no-store', encoding=None, vary=False, retry_after=None):
        if retry_after is not None and (type(retry_after) is not int or not 1 <= retry_after <= 3600):
            raise ValueError('retry_after must be an integer between 1 and 3600')
        if code >= 400:
            diag.event('http.rejected', level='warning', status=code, route=getattr(self, 'diagnostic_route', None),
                       actor=(getattr(self, 'user', None) or {}).get('username'),
                       reason=body.get('error') if isinstance(body, dict) else None,
                       error_code=body.get('code') if isinstance(body, dict) else None,
                       resource=body.get('resource') if isinstance(body, dict) else None)
        if isinstance(body, dict) and body.get('error'):
            from .error_guidance import guidance, public_message
            body = {**body, 'error': public_message(body['error']),
                    'error_info': body.get('error_info') or guidance(body['error'])}
            if getattr(self, 'trace_id', None):
                body['trace_id'] = self.trace_id
                if not isinstance(body['error_info'], dict):
                    body['error_info'] = guidance(body['error'])
                body['error_info'] = {**body['error_info'],
                    'detail': str(body['error_info'].get('detail') or body['error']) + '\n诊断编号：' + self.trace_id}
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        if retry_after is not None:
            self.send_header('Retry-After', str(retry_after))
        if encoding:
            self.send_header('Content-Encoding', encoding)
        if vary:
            self.send_header('Vary', 'Accept-Encoding')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        cookies = list(getattr(self, 'response_cookies', None) or [])
        if getattr(self, 'response_cookie', None) and self.response_cookie not in cookies:
            cookies.append(self.response_cookie)
        for item in cookies:
            self.send_header('Set-Cookie', item)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self, token):
        proto = (self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        secure = '; Secure' if proto == 'https' else ''
        primary = f'exactcut_session={token}; Path={self.public_url("/")}; HttpOnly; SameSite=Strict; Max-Age=604800{secure}'
        self.response_cookie = primary
        cookies = [primary]
        # Older builds used Path=/; clear that leftover so it cannot override /home/ sessions.
        if getattr(getattr(self, 'server', None), 'base_path', ''):
            cookies.append(f'exactcut_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}')
        self.response_cookies = cookies

    def authorized(self):
        self.user = None
        self.session_token = ''
        db = getattr(self.server, 'auth_db', None)
        header = self.headers.get('Cookie', '') or ''
        tokens = []
        for part in header.split(';'):
            item = part.strip()
            if item.lower().startswith('exactcut_session='):
                tokens.append(item.split('=', 1)[1])
        if db and tokens:
            for token in tokens:
                user = accounts.session(db, token)
                if user:
                    self.session_token = token
                    self.user = user
                    break
        if not self.user:
            try:
                scheme, encoded = self.headers.get('Authorization','').split(' ',1)
                username, password = base64.b64decode(encoded,validate=True).decode().split(':',1)
                if scheme == 'Basic':
                    if db:
                        self.user = accounts.authenticate(db,username,password)
                    elif username=='admin' and hmac.compare_digest(password,self.server.password):
                        self.user = {'username':'admin','name':'管理员','role':'admin','enabled':1}
            except (ValueError, UnicodeError):
                pass
        if self.user:
            diag.bind(actor=self.user['username'])
            return True
        if self.path == '/':
            self.send_response(302)
            self.send_header('Location', self.public_url('/login'))
            self.send_header('Content-Length','0')
            self.end_headers()
        else:
            self.respond(401, {'error':'请登录后继续'})
        return False

    def can_access(self, batch_id):
        path = workflow.BATCHES / batch_id
        return (path / 'draft.json').exists() and not workflow.material_versions.internal(path) and accounts.allowed(path, self.user)

    def is_admin(self):
        if self.user['role'] == 'admin':
            return True
        self.respond(403, {'error':'此操作仅限管理员'})
        return False

    def new_batch(self, result):
        for item in result.get('imported_batches', [{'id':result['id']}]):
            path = workflow.folder(item['id'])
            accounts.assign(path, self.user)
            workflow.archive_draft(path)
            if workflow.material_versions.enabled():
                workflow.material_versions.enroll(path)
        fresh = workflow.detail(result['id'])
        if 'imported_batches' in result:
            fresh['imported_batches'] = result['imported_batches']
        return fresh

    def restored_workspace(self, state=None):
        if state is None:
            path = ROOT / '.web-state/workspaces' / (self.user['username'] + '.json')
            state = read_json(path) if path.exists() else {}
        chosen = state.get('last_batch_id')
        if state.get('initialized') and isinstance(chosen, str) and self.can_access(chosen):
            return {'batch_id': chosen}
        return None

    def workspace(self, data):
        """Called under the existing mutation lock; persist onboarding per account."""
        path = ROOT / '.web-state/workspaces' / (self.user['username'] + '.json')
        state = read_json(path) if path.exists() else {}
        if 'batch_id' in data:
            bid = data['batch_id']
            if not isinstance(bid, str) or not self.can_access(bid) or not (workflow.folder(bid) / 'draft.json').exists():
                raise ValueError('项目不存在或无权访问')
            state['last_batch_id'] = bid
            state['initialized'] = True
            write_json_atomic(path, state)
            return {'batch_id': bid}
        restored = self.restored_workspace(state)
        if restored is not None:
            return restored
        batches = workflow.summaries(self.can_access, compact=True)
        accessible = {b['id']: b for b in batches if self.can_access(b['id'])}
        owned = [b for b in batches if accounts.owner(workflow.folder(b['id'])) == self.user['username']]
        if 'batch_id' in data and (not isinstance(data['batch_id'], str) or data['batch_id'] not in accessible):
            raise ValueError('项目不存在或无权访问')
        if not state.get('initialized'):
            if not owned:
                created = workflow.create_blank(display_name='默认项目')
                accounts.assign(workflow.folder(created['id']), self.user)
                if workflow.material_versions.enabled():
                    workflow.material_versions.enroll(workflow.folder(created['id']))
                state['last_batch_id'] = created['id']
                accessible[created['id']] = created
            state['initialized'] = True
        chosen = data.get('batch_id', state.get('last_batch_id'))
        if chosen not in accessible:
            candidates = [b for b in owned if not b.get('archived')] or owned
            chosen = candidates[0]['id'] if candidates else None
        state['last_batch_id'] = chosen
        write_json_atomic(path, state)
        return {'batch_id': chosen}

    def do_GET(self):
        self.normalize_path()
        hook_route=re.fullmatch(r'/api/batches/([0-9a-f]{32})/hook-previews(?:/([0-9a-f]{32}))?',self.path)
        if hook_route:
            from .hook_preview import handle
            return handle(self,*hook_route.groups())
        subtitle_route = re.fullmatch(r'/api/batches/([0-9a-f]{32})/preview-subtitles/([^/]+)', self.path)
        if subtitle_route:
            from .preview_subtitles_http import handle
            return handle(self, *subtitle_route.groups(), 'GET')
        mosaic_route = re.fullmatch(r'/api/batches/([0-9a-f]{32})/mosaics/(.*)', self.path)
        if mosaic_route:
            from .mosaic_http import handle
            return handle(self, *mosaic_route.groups(), 'GET')
        cut_route = re.fullmatch(r'/api/batches/([0-9a-f]{32})/cuts/(.*)', self.path)
        if cut_route:
            from .cutting_http import handle
            return handle(self, *cut_route.groups(), 'GET')
        if self.path == '/login':
            return self.respond(200, self.web_page('login.html'), 'text/html; charset=utf-8')
        if self.path == '/healthz':
            runner = getattr(self.server, 'worker_thread', None)
            heartbeat_age = max(0, time.monotonic() - WORKER_HEARTBEAT) if runner else None
            healthy = runner is None or (runner.is_alive() and heartbeat_age < 30 and WORKER_FAILURES < 3)
            return self.respond(200 if healthy else 503, {'status': 'ok' if healthy else 'degraded', 'release': release_id(), 'environment': environment(), 'maintenance': maintenance_file().exists(), 'worker_alive': runner.is_alive() if runner else None, 'worker_heartbeat_age_seconds': round(heartbeat_age, 1) if runner else None})
        if not self.authorized():
            return
        if self.path == '/brand-style.jpg':
            body = (CODE_ROOT / 'exactcut/web/brand-style.jpg').read_bytes()
            version = hashlib.sha256(body).hexdigest()[:20]
            cache = 'private, max-age=31536000, immutable' if self.asset_version == version else 'no-store'
            return self.respond(200, body, 'image/jpeg', cache=cache)
        if self.path == '/api/queue':
            if not self.is_admin(): return
            try:
                return self.respond(200, queue_snapshot(self.user))
            except (OSError, ValueError, TypeError, KeyError):
                return self.respond(503, {'error': '任务队列暂时不可用，请稍后重试'})
        if self.path == '/api/me':
            from .cutting import enabled as cutting_enabled
            from .preview_subtitles import available as subtitles_enabled
            return self.respond(200, {**self.user, 'features': {**feature_flags(), 'cutting': cutting_enabled(), 'preview_subtitles': subtitles_enabled()}})
        if self.path == '/api/users':
            if not self.is_admin(): return
            return self.respond(200, {'users': accounts.users(self.server.auth_db)})
        if self.path == '/api/storage':
            if not self.is_admin():return
            from .storage import report
            return self.respond(200,report(ROOT))
        if self.path == '/api/usage':
            if not self.is_admin(): return
            try:
                return self.respond(200, storage.usage_report(ROOT))
            except storage.UsageUnavailable as exc:
                return self.respond(503, {'error': str(exc)})
        if self.path == '/':
            if getattr(self.server, 'auth_db', None) and not self.session_token:
                self.cookie(accounts.login(self.server.auth_db, self.user))
            return self.respond(200, self.web_page('index.html'), 'text/html; charset=utf-8')
        if self.path == '/helper.js':
            return self.script('helper.js')
        if self.path == '/mosaic.js':
            return self.script('mosaic.js')
        if self.path == '/editor-busy.js':
            return self.script('editor-busy.js')
        if self.path == '/editor-source.js':
            return self.script('editor-source.js')
        if self.path == '/preview-subtitles.js':
            return self.script('preview-subtitles.js')
        if self.path == '/cutting.js':
            return self.script('cutting.js')
        if self.path == '/app.js':
            return self.script('app.js')
        if self.path == '/diagnostics.js':
            return self.script('diagnostics.js')
        if self.path in {'/api/batches', '/api/batches/sidebar'}:
            with LOCK:
                batches = workflow.summaries(self.can_access, compact=self.path.endswith('/sidebar'))
                result = {'batches': [{**b, 'owner': accounts.owner(workflow.BATCHES / b['id'])} for b in batches]}
            return self.respond(200, result)
        if self.path == '/api/feedback':
            return self.respond(200, workflow.feedback_summary(self.can_access))
        match = re.fullmatch(r'/api/batches/([0-9a-f]{32})(?:/(.*))?', self.path)
        if match:
            try:
                batch_id, suffix = match.groups()
                if not self.can_access(batch_id):
                    return self.respond(404, {'error':'项目不存在或无权访问'})
                path = workflow.folder(batch_id)
                if not suffix:
                    with LOCK:
                        result = batch_response(batch_id)
                    return self.respond(200, result)
                if suffix == 'originals' or suffix.startswith('originals/'):
                    return self._originals(path, suffix)
                if suffix.startswith(('preview-url/', 'download/')):
                    from . import preview_oss
                    original = suffix.startswith('download/')
                    request_id = unquote(suffix.split('/', 1)[1])
                    output, record = workflow.preview_file(batch_id, request_id, lightweight=not original)
                    status_query = str((getattr(self, 'query', {}) or {}).get('status', [''])[0]) == '1'
                    if not preview_oss.enabled():
                        if original and status_query: return self.respond(200, {'status': 'ready'})
                        if original: return self.stream_video(output)
                        from urllib.parse import quote
                        return self.respond(200, {'url': f'/api/batches/{batch_id}/preview/{quote(request_id, safe="")}', 'expires_in': 0})
                    if original and status_query:
                        state = preview_oss.preparation_status(batch_id, request_id, output, record, original=True)
                        if state == 'ready': return self.respond(200, {'status': 'ready'})
                        retry = str((getattr(self, 'query', {}) or {}).get('retry', [''])[0]) == '1'
                        if state == 'failed' and not retry:
                            return self.respond(503, {'error': '下载准备失败，请重试',
                                                      'code': 'preview_prepare_failed', 'retryable': True})
                        job = preview_oss.prepare_async(batch_id, request_id, output, record,
                                                        original=True, actor=self.user['username'])
                        return self.respond(202, {'status': 'preparing', 'retry_after_ms': 1500,
                                                  'job_id': job.get('id')}, retry_after=2)
                    try:
                        receipt = record.get('original_oss') if original else (record.get('preview') or {}).get('oss')
                        options = {'receipt': receipt} if receipt else {}
                        if original: options['download_name'] = output.name
                        options['save_receipt'] = lambda receipt: preview_oss.save_media_receipt(path / 'production', request_id, record, output, receipt, original=original)
                        result = preview_oss.signed_preview(output, batch_id, request_id, wait=False, **options)
                    except preview_oss.PreviewBusy:
                        # No valid receipt: queue a bounded OSS copy and let the
                        # client poll; never upload, stream locally or sign here.
                        # A failed copy is retried only on an explicit user retry,
                        # never by polling.
                        retry = str((getattr(self, 'query', {}) or {}).get('retry', [''])[0]) == '1'
                        if not retry and preview_oss.preparation_status(batch_id, request_id, output, record, original=original) == 'failed':
                            return self.respond(503, {'error': '预览准备失败，请重试',
                                                      'code': 'preview_prepare_failed', 'retryable': True})
                        job = preview_oss.prepare_async(batch_id, request_id, output, record,
                                                        original=original, actor=self.user['username'])
                        diag.event('preview.oss_preparing', level='info', batch_id=batch_id)
                        return self.respond(202, {'status': 'preparing', 'retry_after_ms': 1500,
                                                  'job_id': job.get('id')}, retry_after=2)
                    except Exception as exc:
                        # Never include SDK request URLs/signatures in client errors or logs.
                        diag.event('preview.oss_failed', level='warning', batch_id=batch_id, error_type=type(exc).__name__)
                        return self.respond(503, {'error': '预览准备暂时失败，请稍后重试'})
                    if original:
                        self.send_response(302)
                        self.send_header('Location', result['url'])
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Referrer-Policy', 'no-referrer')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    return self.respond(200, result)
                if suffix.startswith('video/'):
                    output, _ = workflow.preview_file(batch_id, unquote(suffix[6:]))
                    return self.stream_video(output)
                if suffix.startswith('preview/'):
                    output, _ = workflow.preview_file(batch_id, unquote(suffix[8:]), lightweight=True)
                    return self.stream_video(output)
                if suffix == 'manifest':
                    manifest = path / 'confirmed.json'
                    return self.respond(200, manifest.read_bytes() if manifest.exists() else (path / 'draft.json').read_bytes())
                if suffix.startswith(('ending-image/', 'ending-thumbnail/')):
                    image = workflow.ending_image_file(batch_id, unquote(suffix.split('/', 1)[1]))
                    if suffix.startswith('ending-thumbnail/'):
                        from .ending import thumbnail
                        return self.respond(200, thumbnail(image), 'image/webp')
                    kind = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[image.suffix]
                    return self.respond(200, image.read_bytes(), kind)
                if suffix == 'download-log':
                    from .error_guidance import redact
                    log = path / 'download.log'
                    return self.respond(200, {'log': redact(log.read_text(errors='replace')[-64000:], limit=64000) if log.exists() else ''})
            except (ValueError, FileNotFoundError) as exc:
                return self.respond(400, {'error': str(exc)})
            return self.respond(404, {'error': '不存在'})
        if self.path == '/api/state':
            if not self.is_admin(): return
            projects = []
            for p in sorted((ROOT / 'projects').glob('[0-9]' * 6)):
                if p.is_symlink() or not (p / 'project.json').is_file():
                    continue
                meta = json.loads((p / 'project.json').read_text())
                projects.append({'id': p.name, 'title': meta.get('title', p.name),
                                 'videos': len(list((p / 'source/videos').glob('*.mp4'))),
                                 'indexes': len(list((p / 'data/episodes').glob('*.video.json'))),
                                 'inputs': [f.name for f in sorted((p / 'requests').glob('*.xlsx'))]})
            jobs = [job_response(job) for job in stored_jobs()]
            catalog = load_catalog()
            return self.respond(200, {'projects': projects, 'catalog': catalog['dramas'], 'catalog_updated': catalog['updated'], 'jobs': sorted(jobs, key=lambda j: j['created'], reverse=True)[:100]})
        match = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/log', self.path)
        if match:
            if not self.is_admin(): return
            path = JOBS / (match[1] + '.log')
            content = ''
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 64000))
                    content = stream.read().decode('utf-8', errors='replace')
            # Remove configured secrets from diagnostics.
            env = ROOT / '.env'
            if env.exists():
                for line in env.read_text().splitlines():
                    if '=' in line and not line.lstrip().startswith('#'):
                        key, value = line.split('=', 1)
                        value = value.strip().strip('\"\'')
                        if any(x in key for x in ('KEY', 'SECRET', 'TOKEN', 'PASSWORD')) and value:
                            content = content.replace(value, '[REDACTED]')
            return self.respond(200, {'log': content})
        if self.path == '/template.xlsx':
            return self.respond(200, (CODE_ROOT / 'templates/标准剪辑需求_模板.xlsx').read_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.respond(404, {'error': '不存在'})

    def _originals(self, path, suffix):
        from . import originals
        document = read_json(path / 'draft.json') or {}
        inner = document.get('draft') if isinstance(document.get('draft'), dict) else {}
        saved = str(inner.get('drama_id') or document.get('drama_id') or '').strip()
        requested = str((getattr(self, 'query', {}) or {}).get('drama_id', [''])[0] or '').strip()
        drama_id = requested or saved
        try:
            if suffix == 'originals':
                return self.respond(200, originals.load_originals(drama_id))
            rest = unquote(suffix.split('/', 1)[1])
            episode, _, extra = rest.partition('/')
            if not episode or extra not in ('', 'file'):
                return self.respond(400, {'error': '无效集号'})
            if originals.local_only():
                from .source_cache import shared_for
                source = originals.local_original(drama_id, episode)
                with shared_for(source):
                    return self.stream_video(source, download_name=episode + '.mp4')
            if extra == 'file':
                remote = originals.open_original(drama_id, episode)
            else:
                signed = originals.sign_original(drama_id, episode)
        except originals.OriginalsUnavailable as exc:
            return self.respond(exc.status, {'error': str(exc)})
        except RuntimeError as exc:
            return self.respond(400, {'error': str(exc)})
        if extra == 'file':
            return self._send_original(remote, resume=lambda sent: originals.open_original(drama_id, episode, start=sent))
        self.send_response(302)
        self.send_header('Location', signed['url'])
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _send_original(self, remote, resume=None):
        length = remote.headers.get('Content-Length')
        self.send_response(200)
        self.send_header('Content-Type', remote.headers.get('Content-Type') or 'video/mp4')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if length and str(length).isdigit():
            self.send_header('Content-Length', str(length))
        self.end_headers()
        sent, attempts = 0, 0
        while True:
            stream = remote
            try:
                for chunk in stream.iter_bytes():
                    if chunk:
                        self.wfile.write(chunk)
                        sent += len(chunk)
                stream.close()
                return
            except (BrokenPipeError, ConnectionResetError):
                stream.close()
                return
            except Exception as exc:
                # The response head is already sent, so the only way to finish it is
                # to continue the same body from the byte offset the client already has.
                stream.close()
                from requests.exceptions import RequestException
                if not isinstance(exc, RequestException) or resume is None or attempts >= 3:
                    raise
                attempts += 1
                try:
                    remote = resume(sent)
                except Exception:
                    raise exc from None
                if not remote:
                    raise exc from None

    def stream_video(self, path, *, download_name=None):
        started = time.monotonic()
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        requested = self.headers.get('Range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or not any(match.groups()):
                return self.respond(416, {'error': '无效视频范围'})
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            else:
                start = max(0, size - int(right))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            # Bound each range so proxies need not buffer an entire HD video.
            end = min(end, start + 2 * 1024 * 1024 - 1)
            code = 206
        self.send_response(code)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'private, no-store')
        if download_name is not None:
            self.send_header('Content-Disposition', f'attachment; filename="{download_name}"')
        self.send_header('Content-Length', str(end - start + 1))
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as stream:
            stream.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    chunk = stream.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                diag.event('preview.client_disconnected', level='warning')
            finally:
                diag.event('preview.stream', file_bytes=size, range_start=start, range_end=end,
                           sent_bytes=end-start+1-remaining, duration_ms=round((time.monotonic()-started)*1000))

    def do_POST(self):
        self.normalize_path()
        subtitle_route = re.fullmatch(r'/api/batches/([0-9a-f]{32})/preview-subtitles/([^/]+)', self.path)
        if subtitle_route:
            from .preview_subtitles_http import handle
            return handle(self, *subtitle_route.groups(), 'POST')
        mosaic_route = re.fullmatch(r'/api/batches/([0-9a-f]{32})/mosaics/(.*)', self.path)
        if mosaic_route:
            from .mosaic_http import handle
            return handle(self, *mosaic_route.groups(), 'POST')
        cut_route = re.fullmatch(r'/api/batches/([0-9a-f]{32})/cuts/(.*)', self.path)
        if cut_route:
            from .cutting_http import handle
            return handle(self, *cut_route.groups(), 'POST')
        match = re.fullmatch(r'/api/batches/([0-9a-f]{32})/([^/]+)', self.path)
        if match and match[2] == 'hook-preview' and not workflow.material_versions.internal(workflow.BATCHES / match[1]):
            return self.hook_preview(match[1])
        if match:
            path = workflow.BATCHES / match[1]
            if workflow.material_versions.managed(path) or workflow.material_versions.internal(path) or (workflow.material_versions.enabled() and match[2] in {'enroll', 'clone', 'start'}):
                with mutation_lock():
                    return self.material_submission(*match.groups())
        match = re.fullmatch(r'/api/batches/([0-9a-f]{32})/hook-preview', self.path)
        if match:
            return self.hook_preview(match[1])
        publication = re.fullmatch(r'/api/batches/([0-9a-f]{32})/(publish|verify-delivery)', self.path)
        if publication:
            return self.publication_submission(*publication.groups())
        # Parsing does not hold a global write transaction during provider I/O.
        # Excel publication acquires its own short mutation/naming transaction.
        if self.path in {'/api/help', '/api/client-events', '/api/clip-text', '/api/batches/import'}:
            return self.handle_post()
        with mutation_lock():
            self.handle_post()

    def material_submission(self, batch_id, action):
        """Managed CAS transactions run outside legacy mutation/thread locks."""
        m = workflow.material_versions
        if not self.authorized(): return
        if self.headers.get('X-Exactcut-Request') != '1':
            return self.respond(403, {'error': '无效请求来源'})
        if not self.can_access(batch_id):
            return self.respond(404, {'error': '项目不存在或无权访问'})
        if maintenance_file().exists():
            return self.respond(503, {'error': '系统正在发布维护，请稍后再试'})
        if os.environ.get('EXACTCUT_WORKER_ENABLED', '1') != '1':
            return self.respond(503, {'error': '验收实例禁止修改项目'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 1 <= size <= 15_000_000: raise ValueError('无效请求大小')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict): raise ValueError('无效请求')
            path = workflow.folder(batch_id)
            if not m.managed(path):
                if action == 'clone' and data.get('purpose') == 'copy':
                    return self.respond(201, self.new_batch(workflow.clone_batch(batch_id)))
                if not m.enabled(): raise ValueError('素材版本功能尚未启用')
                m.enroll(path)
            code = 200
            result = None
            if action == 'save':
                with workflow.naming_lock(): result = m.save_draft(path, data)
            elif action == 'start':
                with workflow.naming_lock(): result = m.confirm(path, data)
                code = 202
            elif action == 'publish':
                require_publish()
                result = m.queue_delivery(path, data); code = 202
            elif action == 'verify-delivery':
                if not self.is_admin(): return
                result = m.queue_verification(path, data); code = 202
            elif action == 'restore': result = m.restore(path, data.get('request_id'), data)
            elif action in {'delete', 'pause', 'resume', 'archive', 'unarchive'}: result = m.control(path, action, data)
            elif action == 'clone':
                if data.get('purpose', 'copy') == 'copy':
                    return self.respond(201, self.new_batch(workflow.clone_batch(batch_id, purpose='copy')))
                if data.get('purpose') != 'repair': raise ValueError('未知复制操作')
            elif action == 'ending-image':
                return self.respond(200, m.save_ending_image(path, data.get('request_id'), data.get('content')))
            elif action == 'sources':
                return self.respond(200, prefetch_sources(batch_id, data, self.user['username']))
            elif action == 'enroll': m.enroll(path)
            elif action == 'feedback': return self.respond(201, workflow.add_feedback(batch_id, data))
            else: raise ValueError('纳管项目不支持此旧写入操作')
            if getattr(self.server, 'auth_db', None):
                accounts.audit(self.server.auth_db, self.user['username'], action, batch_id)
            response = m.detail(path)
            if result and result.get('mutation'): response['mutation'] = result['mutation']
            return self.respond(code, response)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return self.respond(409 if '更新' in str(exc) or '幂等' in str(exc) else 400, {'error': str(exc)})

    def publication_submission(self, batch_id, action):
        """Prepare the full approval SHA outside LOCK, then fence before enqueue."""
        if not self.authorized(): return
        if self.headers.get('X-Exactcut-Request') != '1':
            return self.respond(403, {'error': '无效请求来源'})
        verification_started = False
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 1 <= size <= 10000: raise ValueError('无效上传确认请求')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict): raise ValueError('无效请求')
            if action == 'verify-delivery' and set(data) - {'request_id', 'sha256', 'submission_token'}:
                raise ValueError('核实仅接受原素材确认值，不接受外部 ID 或其他参数')
            def admit():
                if not self.authorized(): return False
                if maintenance_file().exists():
                    self.respond(503, {'error': '系统正在发布维护，请稍后再试'}); return False
                if os.environ.get('EXACTCUT_WORKER_ENABLED', '1') != '1':
                    self.respond(503, {'error': '验收实例禁止启动或控制生产任务'}); return False
                if action == 'verify-delivery' and not self.is_admin(): return False
                if not self.can_access(batch_id):
                    self.respond(404, {'error': '项目不存在或无权访问'}); return False
                batch_control.ensure_editable(workflow.folder(batch_id), execution=True)
                workflow.reject_review_sha(workflow.result_records(workflow.folder(batch_id)).get(data.get('request_id'), {}), data.get('sha256'))
                require_publish()
                return True
            def snapshot():
                path = workflow.folder(batch_id)
                rid = data.get('request_id')
                values = {name: read_json(path / name) if (path / name).exists() else None
                          for name in ('owner.json', 'access.json', 'confirmed.json')}
                control = batch_control.read(path)
                values['control'] = {key: control.get(key, False) for key in ('paused', 'archived')}
                state = read_json(path / 'runtime.json')
                if not state.get('request_parallel'):
                    values['runtime'] = {key: state.get(key) for key in ('status', 'message')}
                values['result'] = workflow.result_records(path).get(rid)
                values['publication'] = read_json(path / 'publications.json').get(rid) if (path / 'publications.json').exists() else None
                return json_sha256(values)
            with mutation_lock(), LOCK:
                if not admit(): return
                previous = existing_submission(batch_id, action, data, self.user['username'])
                if previous: return self.respond(200, batch_response(batch_id, previous))
                before = snapshot()
            evidence = workflow.approval_evidence(batch_id, data.get('request_id'), data.get('sha256'))
            with mutation_lock(), LOCK:
                if not admit(): return
                previous = existing_submission(batch_id, action, data, self.user['username'])
                if previous: return self.respond(200, batch_response(batch_id, previous))
                if snapshot() != before: return self.respond(409, {'error': '确认期间批次已变化，请重新确认'})
                if action == 'verify-delivery' and any(
                        concurrency.scope_blocked((batch_id, data.get('request_id')), {concurrency.scope(job)})
                        and job.get('status') not in {'completed', 'failed', 'interrupted', 'unverified'}
                        for job in stored_jobs()):
                    raise ValueError('请等待当前批次任务结束后核实')
                if getattr(self.server, 'auth_db', None):
                    accounts.audit(self.server.auth_db, self.user['username'], action, batch_id)
                operation = workflow.queue_verify_delivery if action == 'verify-delivery' else workflow.queue_publish
                job_id = uuid.uuid4().hex
                options = {'_evidence': evidence}
                if action == 'verify-delivery':
                    options['_job_id'] = job_id
                    verification_started = True
                cmd = operation(batch_id, data.get('request_id'), data.get('sha256'), **options)
                job = {'id': job_id, 'batch_id': batch_id, 'project': workflow.detail(batch_id)['draft']['drama_id'],
                       'actor': self.user['username'], 'action': action, 'created': time.time(), 'status': 'queued',
                       'request_id': data.get('request_id'), 'submission_token': data.get('submission_token'),
                       'submission_payload': json_sha256({key: value for key, value in data.items() if key != 'submission_token'}), 'command': cmd}
                if action == 'verify-delivery':
                    job['verification_only'] = read_json(workflow.folder(batch_id) / 'publications.json')[data['request_id']]['verification_only']
                prepare_staged_job(job)
                save(job)
                if action == 'verify-delivery': workflow.recover_verification_queue(list(stored_jobs()), batch_id=batch_id)
                WORK.put((job, job['command']))
                return self.respond(202, batch_response(batch_id, job))
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
            diag.event('http.publication_rejected', level='warning', batch_id=batch_id, error=exc)
            if verification_started:
                with mutation_lock(), LOCK:
                    workflow.recover_verification_queue(list(stored_jobs()), batch_id=batch_id)
            self.respond(400, {'error': str(exc)})

    def hook_preview(self, batch_id):
        from .hook_preview import submit
        from .intake import validate_draft
        from .runtime import protect_path
        if not self.authorized():
            return
        if self.headers.get('X-Exactcut-Request') != '1':
            return self.respond(403, {'error':'无效请求来源'})
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 1 <= size <= 10000:
                raise ValueError('无效预览请求')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError('无效预览请求')
            with mutation_lock(), LOCK:
                if maintenance_file().exists():
                    return self.respond(503,{'error':'正在发布维护，请稍后重试'})
                if not self.can_access(batch_id):
                    return self.respond(404,{'error':'项目不存在或无权访问'})
                snapshot = workflow.detail(batch_id)
                diag.event('hook_preview.request', revision=snapshot['revision'],
                           requested_revision=data.get('revision') if type(data.get('revision')) is int else None,
                           row_index=data.get('index') if type(data.get('index')) is int else None,
                           project_status=snapshot['runtime']['status'], media_managed=bool(snapshot.get('media_managed')),
                           archived=bool(snapshot['runtime'].get('archived')))
                if (not snapshot.get('media_managed') and snapshot['runtime']['status'] != 'draft') or snapshot['runtime'].get('archived'):
                    raise ValueError('请复制为新草稿后调整 Hook')
                if data.get('revision') != snapshot['revision']:
                    return self.respond(409,{'error':'草稿已变化，请重新保存再预览'})
                index = data.get('index')
                if type(index) is not int or not 0 <= index < len(snapshot['draft']['rows']):
                    raise ValueError('无效素材序号')
                manifest, errors = validate_draft(snapshot['draft'], str(workflow.folder(batch_id) / 'source.xlsx'))
                if errors:
                    raise ValueError('请先修正并保存制作内容：'+'；'.join(errors))
                if not re.fullmatch(r'[0-9]{6}',manifest.drama_id):
                    raise ValueError('请填写六位剧 ID')
                project = ROOT/'projects'/manifest.drama_id
                if project.is_symlink():
                    raise ValueError('无效项目目录')
                protect_path(project)
                request = manifest.requests[index]
                diag.bind(request_id=request.request_id, drama_id=manifest.drama_id, revision=snapshot['revision'])
                fingerprint = json_sha256(snapshot['draft'])
            job=submit(batch_id,snapshot,index,request,self.user['username'])
            return self.respond(200 if job['status']=='completed' else 202,job)
        except (ValueError,KeyError,TypeError,RuntimeError,OSError) as exc:
            diag.event('http.hook_preview_failed',level='warning',batch_id=batch_id,error=exc)
            return self.respond(400,{'error':str(exc)})

    def handle_post(self):
        if self.path != '/api/login' and not self.authorized():
            return
        if self.headers.get('X-Exactcut-Request') != '1':
            return self.respond(403, {'error': '无效请求来源'})
        if maintenance_file().exists() and self.path not in ('/api/login', '/api/logout'):
            return self.respond(503, {'error': '系统正在发布维护，请稍后再试；已提交任务继续执行'})
        if os.environ.get('EXACTCUT_WORKER_ENABLED', '1') != '1' and (self.path == '/api/jobs' or re.fullmatch(r'/api/batches/[0-9a-f]{32}/(start|publish|preview|sources|pause|resume|archive|unarchive|delete)', self.path)):
            return self.respond(503, {'error': '验收实例禁止启动或控制生产任务'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 15_000_000:
                raise ValueError('请求大小必须在 15 MB 以内')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('无效请求')
            if self.path == '/api/client-events':
                allowed = {'page_error', 'unhandled_rejection', 'network_error', 'slow_request', 'action',
                           'request_started', 'request_completed', 'media_error', 'media_stalled',
                           'media_waiting', 'media_playing', 'media_play_rejection', 'page_ready', 'visibility',
                           'editor_retry', 'editor_action_latency', 'editor_long_task'}
                if data.get('event') not in allowed or size > 4096:
                    return self.respond(400, {'error': '无效诊断事件'})
                fields = diag.browser_fields(data)
                if not diag.allow_browser_event(self.user['username']):
                    return self.respond(200, {'ok': True})
                key = tuple(fields.get(name) for name in ('event', 'page_id', 'route', 'batch_id', 'action',
                            'status', 'request_id', 'client_request_id', 'source', 'line', 'error_type', 'visibility', 'session_id'))
                diag.throttled('browser.event', (self.user['username'], key), interval=10,
                               level='warning' if fields['event'] in {'page_error', 'unhandled_rejection', 'network_error', 'media_error', 'media_play_rejection'} or fields.get('status', 0) >= 400 else 'info',
                               actor=self.user['username'], browser=fields)
                return self.respond(200, {'ok': True})
            if self.path == '/api/login':
                user = accounts.authenticate(self.server.auth_db, data.get('username',''), data.get('password',''))
                if not user:
                    diag.event('auth.login_rejected', level='warning')
                    return self.respond(401, {'error':'账户或密码错误，连续失败请5分钟后再试'})
                self.user = user
                diag.event('auth.login_succeeded', actor=user['username'])
                self.cookie(accounts.login(self.server.auth_db, user))
                return self.respond(200, user)
            if self.path == '/api/clip-text':
                from .llm_intake import read_clip_text
                return self.respond(200, read_clip_text(data.get('text'), time_format=data.get('time_format', '')))
            if self.path == '/api/help':
                batch_id = data.get('batch_id')
                detail = None
                if batch_id:
                    if not isinstance(batch_id, str) or not re.fullmatch(r'[0-9a-f]{32}', batch_id):
                        raise ValueError('无效项目')
                    with LOCK, batch_control.LOCK:
                        if not self.can_access(batch_id):
                            return self.respond(404, {'error':'项目不存在或无权访问'})
                        detail = workflow.detail(batch_id)
                return self.respond(200, production_helper.ask(self.user, data, detail))
            if self.path == '/api/logout':
                accounts.logout(self.server.auth_db, self.session_token)
                self.cookie('loggedout')
                return self.respond(200, {'ok':True})
            if self.path == '/api/users':
                if not self.is_admin(): return
                return self.respond(200, {'users':accounts.manage(self.server.auth_db,self.user['username'],data)})
            if self.path == '/api/workspace':
                result = self.restored_workspace() if not data else None
                if result is None:
                    with LOCK:
                        result = self.workspace(data)
                return self.respond(200, result)
            if self.path == '/api/storage/cleanup':
                if not self.is_admin():return
                from .storage import cleanup,report
                if not isinstance(data.get('token'),str):raise ValueError('请先查看缓存清单并确认')
                result=cleanup(ROOT,token=data['token'])
                return self.respond(200,{**report(ROOT),'result':result})
            if self.path in ('/api/projects','/api/jobs','/api/upload') and not self.is_admin():
                return
            if self.path == '/api/batches/blank':
                result = workflow.create_blank(self.user.get('name') or '', time_format=data.get('time_format', ''))
                with LOCK:
                    accounts.assign(workflow.folder(result['id']), self.user)
                    if workflow.material_versions.enabled():
                        workflow.material_versions.enroll(workflow.folder(result['id']))
                        result = workflow.detail(result['id'])
                    return self.respond(201, result)
            if self.path == '/api/batches/import':
                result = workflow.import_excel(data,publication_lock=mutation_lock)
                with mutation_lock(), LOCK:
                    return self.respond(201, self.new_batch(result))
            image_upload = re.fullmatch(r'/api/batches/([0-9a-f]{32})/ending-image', self.path)
            if image_upload:
                batch_id = image_upload[1]
                with LOCK:
                    if not self.can_access(batch_id):
                        return self.respond(404, {'error':'项目不存在或无权访问'})
                    return self.respond(200, workflow.save_ending_image(batch_id, data.get('request_id'), data.get('content')))
            match = re.fullmatch(r'/api/batches/([0-9a-f]{32})/(save|start|preview|sources|feedback|clone|pause|resume|archive|unarchive|delete)', self.path)
            if match:
                batch_id, action = match.groups()
                with LOCK:
                    if not self.can_access(batch_id):
                        return self.respond(404, {'error':'项目不存在或无权访问'})
                    if action == 'sources':
                        return self.respond(200, prefetch_sources(batch_id, data, self.user['username']))
                    if getattr(self.server, 'auth_db', None):
                        accounts.audit(self.server.auth_db,self.user['username'],action,batch_id)
                    if action == 'delete':
                        return self.respond(200, workflow.delete_batch(batch_id, data.get('confirmation')))
                    if action in ('pause', 'resume', 'archive', 'unarchive'):
                        batch_control.change(workflow.folder(batch_id), action)
                        return self.respond(200, workflow.detail(batch_id))
                    if action == 'save':
                        return self.respond(200, workflow.save_draft(batch_id, data))
                    if action == 'clone':
                        return self.respond(201, self.new_batch(workflow.clone_batch(batch_id, purpose=data.get('purpose', 'copy'))))
                    if action == 'feedback':
                        return self.respond(201, workflow.add_feedback(batch_id, data))
                    previous = existing_submission(batch_id, action, data, self.user['username'])
                    if previous:
                        return self.respond(200, batch_response(batch_id, previous))
                    if action == 'start':
                        cmd = workflow.start_batch(batch_id, data.get('revision'))
                    elif action == 'preview':
                        path = workflow.folder(batch_id)
                        batch_control.ensure_editable(path, execution=True)
                        if workflow.runtime(path)['status'] in workflow.ACTIVE:
                            raise ValueError('请等待当前制作完成，再生成预览')
                        cmd = [sys.executable, '-u', '-m', 'exactcut.workflow', 'preview', batch_id, instance_marker()]
                    else:
                        raise ValueError('不支持的批次任务')
                    job = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'project': workflow.detail(batch_id)['draft']['drama_id'],
                           'actor': self.user['username'], 'action': action, 'created': time.time(), 'status': 'queued',
                           'request_id': data.get('request_id'), 'submission_token': data.get('submission_token'),
                           'submission_payload': json_sha256({k: v for k, v in data.items() if k != 'submission_token'})}
                    job['command'] = cmd
                    prepare_staged_job(job)
                    cmd = job['command']
                    save(job)
                    WORK.put((job, cmd))
                    return self.respond(202, batch_response(batch_id, job))
            if self.path == '/api/projects':
                drama_id = data.get('project')
                entry = next((item for item in load_catalog()['dramas'] if item['id'] == drama_id), None)
                if entry is None:
                    raise ValueError('剧 ID 不在已核实候选中，请刷新剧目清单')
                project = ROOT / 'projects' / drama_id
                with LOCK:
                    if project.is_symlink():
                        raise ValueError('无效项目目录')
                    project.mkdir(exist_ok=True)
                    config = project / 'project.json'
                    if not config.exists():
                        config.write_text(json.dumps({'project_id': drama_id, 'title': entry['title'], 'excluded_episodes': []}, ensure_ascii=False, indent=2))
                    for folder in ('requests', 'source/videos', 'data/episodes', 'outputs'):
                        (project / folder).mkdir(parents=True, exist_ok=True)
                return self.respond(201, {'id': drama_id})
            if self.path == '/api/jobs':
                if data.get('action') == 'publish':
                    raise ValueError('请先通过新版流程制作并预览，再逐条确认上传 FBBM')
                if data.get('action') == 'refresh_catalog':
                    cmd = [sys.executable, '-u', '-m', 'exactcut.catalog']
                    data['project'] = 'catalog'
                else:
                    cmd = job_command(data)
                with LOCK:
                    job = {'id': uuid.uuid4().hex, 'project': data['project'], 'action': data['action'],
                           'input': data.get('input'), 'status': 'queued', 'created': time.time()}
                    job['command'] = cmd
                    save(job)
                    WORK.put((job, cmd))
                return self.respond(202, job_response(job))
            if self.path == '/api/upload':
                project = project_path(data.get('project'))
                content = base64.b64decode(data['content'], validate=True)
                if len(content) > 10_000_000 or not content.startswith(b'PK'):
                    raise ValueError('请上传 10 MB 以内的 XLSX 文件')
                folder = project / 'requests'
                folder.mkdir(exist_ok=True)
                name = 'upload-' + uuid.uuid4().hex[:12] + '.xlsx'
                (folder / name).write_bytes(content)
                return self.respond(201, {'name': name})
            self.respond(404, {'error': '不存在'})
        except stage_results.ResourceDeferred as exc:
            self.respond(429, {'error': str(exc), 'not_before': exc.not_before})
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            diag.event('http.validation_failed', level='warning', error=exc)
            from .error_guidance import guidance, public_message
            self.respond(400, {'error': public_message(exc), 'error_info': guidance(str(exc))})
        except Exception as exc:
            diag.event('http.handler_failed', level='error', error=exc)
            from .error_guidance import guidance, public_message
            self.respond(400, {'error': public_message(exc), 'error_info': guidance(str(exc))})


def main():
    import fcntl
    env_file = project_env_file()
    if env_file.is_file() or os.environ.get('EXACTCUT_ENV_FILE'):
        load_env_file(env_file)
    limits = concurrency.settings()
    logging.warning('Effective concurrency settings: %s', json.dumps(limits, sort_keys=True))
    base_path = web_base_path()
    claim_data_root()
    diag.configure()
    diag.install_logging()
    worker_enabled = os.environ.get('EXACTCUT_WORKER_ENABLED', '1') == '1'
    JOBS.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    worker_lock = None
    if worker_enabled:
        worker_lock = (STATE / 'worker.lock').open('a')
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('此数据目录已有任务执行器，禁止重复启动')
    password = (STATE / 'password').read_text().strip()
    if len(password) < 24:
        raise RuntimeError('控制台密码需至少 24 位')
    host = os.environ.get('EXACTCUT_BIND', '127.0.0.1').strip() or '127.0.0.1'
    if host not in {'127.0.0.1', '0.0.0.0'}:
        raise RuntimeError('EXACTCUT_BIND 只能是 127.0.0.1 或 0.0.0.0')
    server = ThreadingHTTPServer((host, int(os.environ.get('EXACTCUT_PORT', '8790'))), Handler)
    server.password = password
    server.base_path = base_path
    server.auth_db = STATE / 'accounts.sqlite3'
    diag.event('service.started', base_path=base_path, port=server.server_port, worker_enabled=worker_enabled,
               concurrency=limits, features=feature_flags())
    accounts.initialize(server.auth_db, password)
    # One background dispatcher per data root; it, not page polling, advances the queue.
    from .preview_subtitle_queue import Dispatcher
    from .mosaic_queue import Dispatcher as MosaicDispatcher
    from .interactive_queue import Dispatcher as InteractiveDispatcher
    import signal
    server.subtitle_dispatcher = Dispatcher()
    server.mosaic_dispatcher=MosaicDispatcher()
    server.interactive_dispatcher=InteractiveDispatcher()
    previous_signals={}
    try:
        if not server.subtitle_dispatcher.start():
            diag.event('preview_subtitles.dispatcher_owned_elsewhere', level='warning')
        server.mosaic_dispatcher.start()
        # Interactive HTTP admission is independent of the ordinary production worker switch.
        server.interactive_dispatcher.start()
        for path in workflow.BATCHES.glob('*/draft.json'):
            if not (path.parent / 'access.json').exists():
                workflow.write_json_atomic(path.parent / 'access.json', {'owner':'admin'})
        if worker_enabled:
            recover_jobs(enqueue=True, scheduler=True)
            server.worker_thread = threading.Thread(target=worker, daemon=True)
            server.worker_thread.start()
        def shutdown_signal(signum,frame):
            threading.Thread(target=server.shutdown,daemon=True).start()
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous_signals[sig]=signal.getsignal(sig)
            signal.signal(sig,shutdown_signal)
        server.serve_forever()
    finally:
        try:
            server.interactive_dispatcher.stop(timeout=None)
            server.mosaic_dispatcher.stop(timeout=None)
        finally:
            try:server.subtitle_dispatcher.stop(timeout=None)
            finally:
                server.server_close()
                for sig,handler in previous_signals.items():signal.signal(sig,handler)


if __name__ == '__main__':
    main()
