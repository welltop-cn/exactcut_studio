"""Durable review-first production workflow. Publishing never renders a video."""
from __future__ import annotations
import base64
import copy
import fcntl
from contextlib import contextmanager
import json
import os
import shutil
import signal
import re
import sys
import time
import threading
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from . import batch_control, result_store
from . import diagnostics as diag
from .intake import validate_draft, download_plan
from .llm_intake import read_excel
from .material_download import validate_downloaded_video
from .models import BatchManifest
from .preview import make_preview, available_preview
from .util import read_json, write_json_atomic, load_config, load_env_file, file_sha256, now_iso

from .runtime import CODE_ROOT, data_root, instance_marker, environment, release_id, maintenance_file, source_root
from .runtime import project_env_file

ROOT = data_root()
BATCHES = ROOT / '.web-state/batches'
FEEDBACK = ROOT / 'feedback'
ACTIVE = {'queued', 'downloading', 'indexing', 'producing', 'publishing'}
SAFE = {'qc_passed', 'safety_passed', 'publish_failed', 'fbbm_queued', 'fbbm_ready'}


def folder(batch_id):
    if not isinstance(batch_id, str) or not re.fullmatch(r'[a-f0-9]{32}', batch_id):
        raise ValueError('无效批次 ID')
    path = BATCHES / batch_id
    if not (path / 'draft.json').exists():
        raise ValueError('批次不存在')
    return path


@contextmanager
def publication_lock(path):
    with (path / 'publications.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def store_publication(path, request_id, entry):
    with publication_lock(path):
        all_entries = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
        all_entries[request_id] = entry
        write_json_atomic(path / 'publications.json', all_entries)
        diag.throttled('publication.state', (path.name, request_id, entry.get('status'), entry.get('intent', {}).get('status')),
                       batch_id=path.name, request_id=request_id, status=entry.get('status'),
                       intent_status=entry.get('intent', {}).get('status'), error=entry.get('error'),
                       record_id=(entry.get('publication') or {}).get('feishu_record_id'),
                       platform_action=(entry.get('fbbm') or {}).get('action'),
                       stage_seconds=entry.get('stage_seconds'))
        return all_entries


def runtime(path, *, jobs=None):
    data = read_json(path / 'runtime.json')
    batch_executor = bool(data.get('executor_running'))
    records = result_records(path)
    executing = [r for r in records.values() if r.get('executor_running')]
    if any('executor_running' in r for r in records.values()):
        data['executor_running'] = bool(data.get('executor_running') or executing)
    if executing:
        data['execution_ended_at'] = None
        for field in ('heartbeat_at', 'last_progress_at'):
            values = [r[field] for r in executing if r.get(field)]
            if values: data[field] = max(values)
    observed = executing or ([] if batch_executor else [r for r in records.values() if r.get('execution_started_at')])
    if not executing:
        observed = [r for r in observed if r['execution_started_at'] >= (data.get('execution_started_at') or '')]
    if observed:
        latest = max(observed, key=lambda r: r.get('last_progress_at') or r.get('execution_started_at') or '')
        for field in ('stage', 'stage_started_at', 'progress_details', 'execution_started_at'):
            if field in latest: data[field] = latest[field]
        if not executing:
            for field in ('execution_ended_at', 'heartbeat_at', 'last_progress_at'):
                if field in latest: data[field] = latest[field]
    data.update(production_summary(path, {**data, 'executor_running': batch_executor}, records, jobs=jobs))
    return data


def production_summary(path, data, records=None, *, jobs=None):
    """One rule for persisted summaries and reads; count only confirmed requests."""
    if not data.get('request_parallel') or not (path / 'confirmed.json').exists():
        return {}
    requests = read_json(path / 'confirmed.json')['requests']
    ids = {request['request_id'] for request in requests}
    records = result_records(path) if records is None else records
    publications = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
    terminal = SAFE | {'failed', 'publish_dry_run', 'unverified'}
    pending = {rid for rid in ids if records.get(rid, {}).get('status') not in terminal}
    render_executors = set()
    for rid in ids:
        record = records.get(rid, {})
        if not record.get('executor_running'): continue
        phase = record.get('execution_phase')
        # Old records have no phase. An active result with no delivery entry is
        # a render; old upload/poll records have the saved publication and stage.
        if phase == 'render' or (phase is None and not publications.get(rid)):
            render_executors.add(rid)
    if data.get('executor_running') and data.get('execution_phase') == 'render':
        render_executors.update(ids)  # Legacy whole-batch executor.
    pending.update(render_executors)
    queued = set()
    if jobs is None:
        jobs = (read_json(file) for file in (ROOT / '.web-state/jobs').glob('*.json'))
    for job in jobs:
        if (job.get('batch_id') == path.name and job.get('phase') == 'render'
                and job.get('status') in {'queued', 'running', 'waiting_resource'}):
            rid = job.get('request_id')
            queued.update(ids if rid is None else ids & {rid})
    # A QC-passed render may still be finishing its preview/evidence. Queued
    # retries can also retain a terminal shard from the preceding attempt.
    pending.update(queued)
    passed = sum(records.get(rid, {}).get('status') in SAFE | {'publish_dry_run'} for rid in ids)
    failed = sum(records.get(rid, {}).get('status') == 'failed' for rid in ids)
    complete = bool(ids) and not pending
    if not complete:
        if data.get('status') in {'failed', 'interrupted'} and not render_executors and not queued:
            return {'production_complete': False}  # Preserve explicit recovery failures.
        return {'status': 'producing', 'production_complete': False,
                'message': f'正在制作，{passed} 条可预览，{failed} 条失败，{len(pending)} 条待完成'}
    states = [publications.get(rid, {}).get('status') or records.get(rid, {}).get('status') for rid in ids]
    message = f'制作结束，{passed} 条可预览，{failed} 条失败'
    if failed or any(state in {'publish_failed', 'unverified'} for state in states):
        status = 'partial'
        if any(state in {'publish_failed', 'unverified'} for state in states): message += '；部分交付失败或待核实'
    elif all(state == 'fbbm_ready' for state in states):
        status = 'complete'
        message += '；全部素材 FBBM 已就绪'
    elif any(state in {'upload_queued', 'uploading', 'fbbm_queued'} for state in states):
        status = 'publishing'
        message += '；素材正在交付'
    else:
        status = 'review'
    return {'status': status, 'message': message, 'production_complete': True}


def update(path, status=None, message=None, **fields):
    with (path / 'runtime.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _update_locked(path, status, message, **fields)


def _update_locked(path, status=None, message=None, **fields):
    data = read_json(path / 'runtime.json')
    previous_status = data.get('status')
    was_complete = data.get('production_complete', False)
    if status:
        data['status'] = status
    if message:
        data['message'] = message
        data.setdefault('events', []).append({'at': now_iso(), 'message': message})
    data.update(fields, updated_at=now_iso())
    # Re-read durable shards while holding runtime.lock. Never commit a render's
    # private snapshot, and never let a later heartbeat/message regress the sum.
    data.update(production_summary(path, data))
    if data.get('production_complete') and not was_complete and data.get('message') != message:
        data.setdefault('events', []).append({'at': now_iso(), 'message': data['message']})
    write_json_atomic(path / 'runtime.json', data)
    if data.get('status') != previous_status:
        diag.event('batch.state', batch_id=path.name, previous_status=previous_status, status=data.get('status'),
                   reason=message)
    return data


def checkpoint(path, stage, *, advanced=True, request_id=None, **details):
    diag.throttled('pipeline.checkpoint', (path.name, request_id, stage), interval=30,
                   batch_id=path.name, request_id=request_id, stage=stage, advanced=advanced, details=details)
    if request_id is not None:
        record = result_records(path).get(request_id, {})
        fields = {'stage': stage, 'progress_details': details}
        if record.get('stage') != stage: fields['stage_started_at'] = now_iso()
        if advanced: fields['last_progress_at'] = now_iso()
        result_store.patch(path / 'production', request_id, **fields)
        if stage in {'upload', 'fbbm'}:
            return
        from .progress import STAGES
        prefix = f'{request_id}：'
        message = prefix + STAGES.get(stage, stage)
        # The renderer has already persisted its stage before this callback.
        # Deduplicate against the timeline under the same lock as event writes.
        with (path / 'runtime.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            events = read_json(path / 'runtime.json').get('events', [])
            previous = next((e['message'] for e in reversed(events)
                             if e.get('message', '').startswith(prefix)), None)
            if previous != message:
                _update_locked(path, message=message)
        return
    fields = {'stage': stage, 'progress_details': details}
    if runtime(path).get('stage') != stage:
        fields['stage_started_at'] = now_iso()
    if advanced:
        fields['last_progress_at'] = now_iso()
    update(path, **fields)


@contextmanager
def execution(path, request_id=None, interval=3, *, phase=None):
    if request_id is not None:
        result_store.patch(path / 'production', request_id, execution_started_at=now_iso(),
                           execution_ended_at=None, executor_running=True, execution_phase=phase, heartbeat_at=now_iso(),
                           last_progress_at=now_iso(), stage=None, stage_started_at=None, progress_details={}, error=None)
    else:
        update(path, execution_started_at=now_iso(), execution_ended_at=None,
           executor_running=True, execution_phase=phase, heartbeat_at=now_iso(), last_progress_at=now_iso(),
           stage=None, stage_started_at=None, progress_details={}, error=None,
           active_request=request_id)
    if request_id is not None and phase == 'render':
        update(path, message=f'{request_id}：开始制作')
    stop = threading.Event()
    def pulse():
        while not stop.wait(interval):
            if request_id is None: update(path, heartbeat_at=now_iso())
            else: result_store.patch(path / 'production', request_id, heartbeat_at=now_iso())
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        end_execution(path, request_id)


def end_execution(path, request_id=None, **fields):
    if request_id is None:
        update(path, executor_running=False, execution_ended_at=now_iso(), **fields)
    else:
        result_store.patch(path / 'production', request_id, executor_running=False, execution_ended_at=now_iso(), **fields)
        update(path)  # Last render exit persists the full, current batch summary.


@contextmanager
def naming_lock():
    file = ROOT / '.web-state/naming.lock'
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def naming_scope(draft):
    from .compiler import production_date
    from .naming import build_material_filename, validate_material_filename
    date = production_date() if draft.get('standard_template_version') == 2 else str(draft.get('business_date', '')).replace('-', '').replace('/', '')
    name = build_material_filename(date, draft.get('material_type') or 'typezxraw', draft.get('author', ''), 'agent', 1, draft.get('drama_id', ''))
    parsed = validate_material_filename(name)
    return tuple(parsed[key] for key in ('date', 'type', 'creator1', 'creator2', 'novel_id'))


def allocate_sequences(draft, batch_id, *, all_new=False):
    from .naming import validate_material_filename
    try:
        scope = naming_scope(draft)
    except (ValueError, RuntimeError):
        return False  # Missing metadata stays editable; validation still blocks execution.
    used = set()
    for file in BATCHES.glob('*/draft.json'):
        if file.parent.name == batch_id:
            continue
        other = read_json(file)['draft']
        try:
            if naming_scope(other) == scope:
                used.update(int(row['seq']) for row in other.get('rows', []) if str(row.get('seq', '')).isdigit())
        except (ValueError, RuntimeError):
            continue
    for file in BATCHES.glob('*/confirmed.json'):
        if file.parent.name == batch_id:
            continue
        for request in read_json(file)['requests']:
            parsed = validate_material_filename(request['output_filename'])
            if tuple(parsed[key] for key in ('date', 'type', 'creator1', 'creator2', 'novel_id')) == scope:
                used.add(int(parsed['seq']))
    local = [int(row['seq']) for row in draft['rows'] if str(row.get('seq', '')).isdigit()]
    next_seq = max([0, *used, *local]) + 1
    changed = False
    for row in draft['rows']:
        if not str(row.get('seq', '')).isdigit() or int(row['seq']) < 1:
            continue
        original = int(row['seq'])
        if all_new or original in used:
            row.setdefault('original_seq', str(original))
            row['seq'] = str(next_seq)
            next_seq += 1
            changed = True
        used.add(int(row['seq']))
    return changed


def create_blank(author='', *, display_name=''):
    """Empty copy of the current editor. No spreadsheet and no model call."""
    from .intake import blank_clip
    batch_id = uuid.uuid4().hex
    path = BATCHES / batch_id
    path.mkdir(parents=True)
    row = {'request_id': 'R' + batch_id[:8], 'seq': 1, 'title_cn': '', 'hook_text': '', 'structure': '顺叙',
           'notes': '', 'timeline_text': '', 'source': '网页填写',
           'hook_options': {'mode': 'ai', 'style': 'auto', 'position': 'auto', 'duration_ms': None, 'manual_ranges': []},
           'clips': [blank_clip()]}
    draft = {'drama_id': '', 'drama_title': '', 'business_date': '', 'author': '',
             'creator2': 'agent', 'material_type': 'typezxraw', 'time_notation': 'seconds', 'rows': [row]}
    write_json_atomic(path / 'draft.json', {'id': batch_id, 'filename': '网页填写', 'display_name': display_name,
                                            'created_at': now_iso(), 'revision': 1,
                                            'draft': draft, 'warnings': []})
    write_json_atomic(path / 'runtime.json', {'status': 'draft', 'message': '请填写剧集、日期和剪辑时间',
                                              'updated_at': now_iso(), 'events': [{'at': now_iso(), 'message': '项目已创建，等待确认内容'}]})
    return detail(batch_id)


def save_ending_image(batch_id, request_id, payload):
    if not isinstance(request_id, str) or not re.fullmatch(r'[\w-]{1,80}', request_id):
        raise ValueError('需求编号无效')
    raw = base64.b64decode(payload or '', validate=True)
    if not raw or len(raw) > 4_000_000:
        raise ValueError('结尾图片需在 4 MB 以内')
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        ext = '.png'
    elif raw.startswith(b'\xff\xd8\xff'):
        ext = '.jpg'
    elif raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        ext = '.webp'
    else:
        raise ValueError('结尾图片只接受 PNG、JPEG 或 WebP')
    folder_path = folder(batch_id)
    batch_control.ensure_editable(folder_path, execution=False)
    if runtime(folder_path)['status'] != 'draft' or (folder_path / 'confirmed.json').exists():
        raise ValueError('已确认批次不可修改；请复制为新草稿后调整')
    destination = folder_path / 'endings'
    destination.mkdir(exist_ok=True)
    for old in destination.glob(request_id + '.*'):
        old.unlink()
    name = request_id + ext
    target = destination / name
    temporary = target.with_suffix(target.suffix + '.part')
    temporary.write_bytes(raw)
    os.replace(temporary, target)
    return {'image': name}


def ending_image_file(batch_id, request_id):
    if not re.fullmatch(r'[\w-]{1,80}', request_id or ''):
        raise ValueError('需求编号无效')
    folder_path = folder(batch_id) / 'endings'
    matches = [path for path in folder_path.glob(request_id + '.*') if path.suffix in {'.png', '.jpg', '.webp'}]
    if len(matches) != 1:
        raise ValueError('还没有结尾图片')
    return matches[0]


def import_excel(data):
    payload = base64.b64decode(data.get('content', ''), validate=True)
    name = Path(str(data.get('filename', 'upload.xlsx'))).name
    if not name.lower().endswith('.xlsx') or len(payload) > 10_000_000 or not payload.startswith(b'PK'):
        raise ValueError('请上传 10 MB 以内的 .xlsx 文件')
    batch_id = uuid.uuid4().hex
    path = BATCHES / batch_id
    path.mkdir(parents=True)
    source = path / 'source.xlsx'
    source.write_bytes(payload)
    try:
        groups = read_excel(source, grouped=True)
    except Exception:
        source.unlink()
        path.rmdir()
        raise
    with naming_lock():
        imported = []
        for index, (draft, warnings) in enumerate(groups):
            group_id = batch_id if index == 0 else uuid.uuid4().hex
            group_path = BATCHES / group_id
            group_path.mkdir(exist_ok=True)
            if index:
                (group_path / 'source.xlsx').write_bytes(payload)
            draft['source_author'] = draft.get('author', '')
            draft['creator2'] = 'agent'
            if allocate_sequences(draft, group_id):
                warnings.append('已有同名素材，已分配新的素材序号；原序号和原表保留，请确认最终文件名')
            write_json_atomic(group_path / 'draft.json', {'id': group_id, 'filename': name, 'created_at': now_iso(),
                                                         'revision': 1, 'draft': draft, 'warnings': warnings})
            write_json_atomic(group_path / 'runtime.json', {'status': 'draft', 'message': 'Excel 已整理，请检查并修改标准化内容',
                                                           'updated_at': now_iso(), 'events': [{'at': now_iso(), 'message': '项目已创建，等待确认内容'}]})
            archive_draft(group_path)
            imported.append({'id': group_id, 'drama_id': draft['drama_id'], 'count': len(draft['rows'])})
    response = detail(batch_id)
    response['imported_batches'] = imported
    return response


def archive_draft(path):
    """Archive source and independent segment columns without granting production."""
    data = read_json(path / 'draft.json')
    draft = data['draft']
    drama_id = str(draft.get('drama_id', ''))
    if not re.fullmatch(r'\d{6}', drama_id) or not 101050 <= int(drama_id) <= 999999:
        return
    project = ROOT / 'projects' / drama_id
    if project.is_symlink():
        raise ValueError('无效项目目录')
    target = project / 'requests' / ('import-' + data['id'])
    target.mkdir(parents=True, exist_ok=True)
    original = target / 'original.xlsx'
    source_xlsx = path / 'source.xlsx'
    if not original.exists() and source_xlsx.is_file():
        original.write_bytes(source_xlsx.read_bytes())
    from .intake import normalized_timeline
    rows = []
    for row in draft['rows']:
        segments = normalized_timeline(row.get('timeline_text', ''), draft.get('time_notation', 'seconds')).splitlines()
        rows.append({**row, **{f'片段{i}': value for i, value in enumerate(segments, 1)},
                     '片尾时长': '待读取片尾时长' if any('全集' in s or '-结尾' in s for s in segments) else ''})
    write_json_atomic(target / 'normalized.json', {**data, 'draft': {**draft, 'rows': rows}})


def result_records(path):
    return result_store.read(path / 'production')['requests']


def detail(batch_id):
    path = folder(batch_id)
    data = read_json(path / 'draft.json')
    manifest, errors = validate_draft(data['draft'], str(path / 'source.xlsx'))
    if (path / 'confirmed.json').exists():
        manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
        errors = []
    data.update(runtime=batch_control.present(path, runtime(path)), errors=errors, plan=download_plan(manifest) if manifest else None)
    from .intake import timing_explanation
    project=ROOT/'projects'/manifest.drama_id if manifest else None
    data['timing_explanation'] = timing_explanation(manifest,project,wait=False)
    data['errors'].extend(s['error'] for t in data['timing_explanation'] for s in t['segments'] if s.get('error'))
    records = result_records(path)
    publish = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
    assets = []
    for request in (manifest.requests if manifest else []):
        record = records.get(request.request_id, {})
        publication = publish.get(request.request_id, {})
        asset = {'request_id': request.request_id, 'title': request.title_cn or request.request_id,
                 'filename': request.output_filename, 'status': publication.get('status') or record.get('status', 'pending'),
                 'stage': record.get('stage'), 'error': publication.get('error') or record.get('error'),
                 'duration_ms': record.get('duration_ms'), 'sha256': record.get('output_sha256'),
                 'video_id': (publication.get('fbbm') or {}).get('video_id')}
        asset['planned_duration_ms'] = record.get('planned_duration_ms', (record.get('qc') or {}).get('expected_duration_ms'))
        asset['unit_checks'] = record.get('unit_checks', [])
        asset['stage_seconds'] = {**record.get('stage_seconds', {}), **publication.get('stage_seconds', {})}
        asset['base_cache_reused'] = record.get('base_cache_reused', False)
        asset['hook_warnings'] = record.get('hook_warnings', [])
        emphasis = record.get('hook_emphasis')
        if isinstance(emphasis, dict):
            asset['hook_emphasis'] = {'source': emphasis.get('source'), 'reason': emphasis.get('reason', ''),
                                      'highlights': [{'phrase': span['phrase'], 'role': span['role']}
                                                     for span in emphasis.get('highlights', [])]}
        output = path / 'production/videos' / request.output_filename
        if record.get('status') in SAFE and output.is_file():
            endpoint = 'preview' if available_preview(path, request, record) else 'video'
            asset['preview'] = f'/api/batches/{batch_id}/{endpoint}/{quote(request.request_id, safe="")}'
            asset['original'] = f'/api/batches/{batch_id}/download/{quote(request.request_id, safe="")}'
            asset['preview_kind'] = 'lightweight' if endpoint == 'preview' else 'original'
        assets.append(asset)
    data['assets'] = assets
    data['feedback'] = sorted([read_json(f) for f in FEEDBACK.glob(f'{batch_id}-*.json')], key=lambda e: e['created_at'], reverse=True) if FEEDBACK.exists() else []
    data['feedback_count'] = len(data['feedback'])
    from .progress import observation
    from .error_guidance import guidance, redact
    r = data['runtime']
    r['observation'] = observation(r, paused=r.get('paused', False))
    r['error_info'] = guidance(r.get('message'), r.get('stage')) if r['status'] in {'failed', 'interrupted'} else None
    r['message'] = redact(r.get('message'))
    r['events'] = [{**e, 'message': redact(e.get('message'))} for e in r.get('events', [])]
    for asset in assets:
        record = records.get(asset['request_id'], {})
        pub = publish.get(asset['request_id'], {})
        asset['can_continue_fbbm'] = bool((pub.get('publication') or {}).get('feishu_record_id'))
        asset['error_info'] = guidance(asset.get('error'), 'fbbm' if pub.get('error') else record.get('stage'),
                                      continuation=asset['can_continue_fbbm'])
        asset['error'] = redact(asset.get('error'))
        asset['preview_warning'] = guidance(record.get('preview_error') or record.get('preview_oss_error'), 'preview')
        asset['observation'] = observation(record)
    return data


def summaries(can_access=None, *, compact=False):
    if not BATCHES.exists():
        return []
    result = []
    jobs = [read_json(file) for file in (ROOT / '.web-state/jobs').glob('*.json')] if compact else None
    for p in BATCHES.glob('*/draft.json'):
        if can_access is not None and not can_access(p.parent.name):
            continue
        item = read_json(p)
        result.append({'id': item['id'], 'filename': item['filename'], 'display_name': item.get('display_name', ''), 'created_at': item['created_at'],
                       'drama_id': item['draft'].get('drama_id'), 'drama_title': item['draft'].get('drama_title'),
                       'count': len(item['draft'].get('rows', [])), **batch_control.present(p.parent, runtime(p.parent, jobs=jobs))})
    if compact:
        # Sidebar contract: histories and execution details belong to the selected project.
        fields = ('id', 'filename', 'display_name', 'created_at', 'drama_id', 'drama_title', 'count', 'status', 'archived')
        return sorted(({key: item.get(key) for key in fields} for item in result),
                      key=lambda item: item['created_at'], reverse=True)
    from .error_guidance import redact
    for item in result:
        item['message'] = redact(item.get('message'))
        item['events'] = [{**e, 'message': redact(e.get('message'))} for e in item.get('events', [])]
    return sorted(result, key=lambda item: item['created_at'], reverse=True)


def delete_batch(batch_id, confirmation):
    """Permanently remove one archived batch, never drama-level shared media."""
    path = folder(batch_id)
    if confirmation != batch_id:
        raise ValueError('请确认永久删除此项目，删除后不可恢复')
    with batch_control.LOCK:
        if not batch_control.read(path).get('archived'):
            raise ValueError('请先归档项目，再永久删除')
        jobs_dir = ROOT / '.web-state/jobs'
        jobs = [p for p in jobs_dir.glob('*.json') if read_json(p).get('batch_id') == batch_id]
        identities = batch_control.recorded_children(path)
        for identity in identities:
            batch_control.signal_identity(identity, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            alive = any(batch_control.child_state(identity) not in {None, 'Z'} for identity in identities)
            if not alive:
                break
            if time.monotonic() > deadline:
                raise ValueError('执行进程正在退出，请稍后再次删除')
            time.sleep(.05)
        for target in (ROOT / 'projects').glob('*/requests/import-' + batch_id):
            if target.is_symlink() or any(p.is_symlink() for p in [target.parent, target.parent.parent]):
                raise ValueError('项目目录无效，未删除')
        for target in (ROOT / 'projects').glob('*/requests/import-' + batch_id):
            shutil.rmtree(target)
        for target in FEEDBACK.glob(batch_id + '-*.json'):
            target.unlink()
        for job in jobs:
            job.with_suffix('.log').unlink(missing_ok=True)
            job.unlink(missing_ok=True)
        shutil.rmtree(path)
    return {'deleted': batch_id}


def save_draft(batch_id, data):
    with naming_lock():
        return _save_draft(batch_id, data)


def _save_draft(batch_id, data):
    path = folder(batch_id)
    batch_control.ensure_editable(path, execution=False)
    if (path / 'confirmed.json').exists() or runtime(path)['status'] != 'draft':
        raise ValueError('已确认批次不可修改；请复制为新草稿后调整')
    original = read_json(path / 'draft.json')
    if data.get('revision') != original['revision']:
        raise ValueError('草稿已在其他页面更新，请刷新后重试')
    draft = data.get('draft')
    if not isinstance(draft, dict) or not isinstance(draft.get('rows'), list) or not all(isinstance(row, dict) for row in draft['rows']):
        raise ValueError('标准化内容格式错误')
    from .intake import compile_clips, compile_composition
    for row in draft['rows']:
        compile_composition(row)
        compile_clips(row)
    if len(json.dumps(draft)) > 1_000_000:
        raise ValueError('草稿内容过大')
    changed = allocate_sequences(draft, batch_id)
    original.update(draft=draft, revision=original['revision'] + 1)
    if changed:
        original.setdefault('warnings', []).append('素材命名冲突，已分配新序号；请确认最终文件名')
    original['draft']['creator2'] = 'agent'
    original['warnings'] = [
        '按已确认写法整理：点号为分秒；单独集号为全集；单个时间点为该点到片尾。下载后读取实际片尾时长。'
        if '创意脚本按「分.秒」' in warning else warning
        for warning in original.get('warnings', [])
    ]
    write_json_atomic(path / 'draft.json', original)
    archive_draft(path)
    return detail(batch_id)


def clone_batch(batch_id, *, purpose='copy'):
    with naming_lock():
        return _clone_batch(batch_id, purpose=purpose)


def _clone_batch(batch_id, *, purpose='copy'):
    source = folder(batch_id)
    if purpose not in {'copy', 'repair'}:
        raise ValueError('无效草稿用途')
    if purpose == 'repair' and runtime(source)['status'] not in {'failed', 'partial', 'interrupted', 'review'}:
        raise ValueError('请等待当前制作结束，再修改后重新制作')
    data = read_json(source / 'draft.json')
    source_revision = data['revision']
    batch_id = uuid.uuid4().hex
    path = BATCHES / batch_id
    path.mkdir()
    source_xlsx = source / 'source.xlsx'
    if source_xlsx.is_file():
        (path / 'source.xlsx').write_bytes(source_xlsx.read_bytes())
    data.update(id=batch_id, created_at=now_iso(), revision=1)
    data.update(source_batch_id=source.name, source_revision=source_revision, clone_purpose=purpose)
    data.pop('imported_batches', None)
    data['draft']['source_author'] = data['draft'].get('author', '')
    data['draft']['creator2'] = 'agent'
    allocate_sequences(data['draft'], batch_id, all_new=True)
    data['warnings'] = ['由原项目创建新草稿，原任务、日志与成片保留；素材序号已重新分配，请确认文件名与内容']
    write_json_atomic(path / 'draft.json', data)
    write_json_atomic(path / 'runtime.json', {'status': 'draft', 'message': '新草稿待确认', 'events': [{'at': now_iso(), 'message': '项目已创建，等待确认内容'}], 'updated_at': now_iso()})
    archive_draft(path)
    return detail(batch_id)


def start_batch(batch_id, revision):
    with naming_lock():
        return _start_batch(batch_id, revision)


def _start_batch(batch_id, revision):
    path = folder(batch_id)
    batch_control.ensure_editable(path, execution=True)
    state = runtime(path)
    pending_render = any(job.get('batch_id') == batch_id and job.get('phase') in {'fetch', 'render'}
                         and job.get('status') in {'queued', 'running', 'waiting_resource'}
                         for file in (ROOT / '.web-state/jobs').glob('*.json')
                         for job in [read_json(file)])
    if state['status'] in ACTIVE or state.get('executor_running') or pending_render:
        raise ValueError('此批次正在执行，请勿重复提交')
    data = read_json(path / 'draft.json')
    if data['revision'] != revision:
        raise ValueError('请先保存最新草稿，再确认执行')
    manifest, errors = validate_draft(data['draft'], str(path / 'source.xlsx'))
    if errors:
        raise ValueError('\n'.join(errors))
    if state['status'] == 'interrupted' and state.get('active_request') and (path / 'publications.json').exists():
        raise ValueError('上次中断于上传阶段，请在成片预览中重试对应素材上传')
    if (path / 'confirmed.json').exists():
        if state['status'] not in {'failed', 'partial', 'interrupted'}:
            raise ValueError('此批次已制作，请直接预览或复制新草稿')
        manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
    else:
        names = {r.output_filename for r in manifest.requests}
        for other in BATCHES.glob('*/confirmed.json'):
            if other.parent != path:
                overlap = names & {r['output_filename'] for r in read_json(other)['requests']}
                if overlap:
                    raise ValueError('素材命名重复，请修改素材序号后重试')
    project = ROOT / 'projects' / manifest.drama_id
    if project.is_symlink():
        raise ValueError('无效项目目录')
    from .intake import timing_explanation
    from .storage import require_space
    require_space(ROOT)
    require_space(source_root())
    timing=timing_explanation(manifest,project)
    errors=[s['error'] for t in timing for s in t['segments'] if s.get('error')]
    if errors:raise ValueError('\n'.join(errors))
    for directory in ('requests', 'source/videos', 'data/episodes', 'outputs'):
        (project / directory).mkdir(parents=True, exist_ok=True)
    if not (project / 'project.json').exists():
        write_json_atomic(project / 'project.json', {'project_id': manifest.drama_id, 'title': manifest.drama_title, 'excluded_episodes': []})
    if not (path / 'confirmed.json').exists():
        write_json_atomic(path / 'confirmed.json', manifest.model_dump(mode='json'))
        write_json_atomic(path / 'confirmation.json', {'at': now_iso(), 'revision': revision})
    update(path, 'queued', '内容已确认，等待下载与制作', plan=download_plan(manifest),
           executor_running=False, execution_started_at=None, execution_ended_at=None,
           heartbeat_at=None, last_progress_at=None, stage=None, progress_details={}, error=None)
    return [sys.executable, '-u', '-m', 'exactcut.workflow', 'produce', batch_id, instance_marker()]


def request_for(path, request_id):
    manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
    request = next((r for r in manifest.requests if r.request_id == request_id), None)
    if request is None:
        raise ValueError('素材不存在')
    return manifest, request


def preview_file(batch_id, request_id, *, lightweight=False):
    path = folder(batch_id)
    _, request = request_for(path, request_id)
    record = result_records(path).get(request_id, {})
    if record.get('status') not in SAFE:
        raise ValueError('素材尚未通过检查，暂不可预览或上传')
    output = path / 'production/videos' / request.output_filename
    if not output.is_file():
        raise ValueError('成片文件不存在')
    if lightweight:
        output = available_preview(path, request, record) or output
    return output, record


def make_batch_previews(batch_id):
    with execution(folder(batch_id)):
        return _make_batch_previews(batch_id)


def _make_batch_previews(batch_id):
    path = folder(batch_id)
    manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
    results = result_store.read(path / 'production')
    failures = 0
    for request in manifest.requests:
        record = results['requests'].get(request.request_id, {})
        if record.get('status') not in SAFE:
            continue
        from .preview_oss import upload_generated_preview, upload_generated_original
        if available_preview(path, request, record):
            upload_generated_preview(path / 'production', request, record)
            upload_generated_original(path / 'production', request, record)
            result_store.put(path / 'production', request.request_id, record)
            continue
        master = path / 'production/videos' / request.output_filename
        checkpoint(path, 'preview')
        update(path, active_request=request.request_id)
        try:
            record['preview'] = make_preview(master, path / 'production/previews' / request.output_filename, record['output_sha256'])
            record.pop('preview_error', None)
        except Exception as exc:
            record['preview_error'] = str(exc)
            failures += 1
        upload_generated_preview(path / 'production', request, record)
        upload_generated_original(path / 'production', request, record)
        result_store.put(path / 'production', request.request_id, record)
    return 2 if failures else 0


def reject_review_sha(record, sha256):
    review = record.get('review') if isinstance(record.get('review'), dict) else {}
    review_sha = review.get('sha256')
    if review_sha and sha256 == review_sha and sha256 != record.get('output_sha256'):
        raise ValueError('不能使用审片文件的校验值上传，请确认正式成片')


def approval_evidence(batch_id, request_id, sha256, evidence=None):
    from .stage_results import digest_evidence, file_identity
    video, record = preview_file(batch_id, request_id)
    reject_review_sha(record, sha256)
    evidence = digest_evidence(video) if evidence is None else evidence
    if (sha256 != record.get('output_sha256') or evidence.get('sha256') != sha256
            or evidence.get('path') != str(video) or evidence.get('stat') != file_identity(video)):
        raise ValueError('成片版本已变化，请重新预览后再确认')
    return evidence


def queue_publish(batch_id, request_id, sha256, *, _evidence=None):
    path = folder(batch_id)
    batch_control.ensure_editable(path, execution=True)
    if not runtime(path).get('request_parallel') and runtime(path)['status'] in ACTIVE - {'publishing'}:
        raise ValueError('当前批次正在制作，请完成后再提交上传')
    reject_review_sha(result_records(path).get(request_id, {}), sha256)
    video, record = preview_file(batch_id, request_id)
    approval_evidence(batch_id, request_id, sha256, _evidence)
    with publication_lock(path):
        publications = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
        if publications.get(request_id, {}).get('verification_history') or publications.get(request_id, {}).get('verification_only') or publications.get(request_id, {}).get('status') == 'unverified' or publications.get(request_id, {}).get('intent', {}).get('status') == 'pending':
            raise ValueError('外部交付结果待核实，禁止重复提交；请先核实原对象和记录')
        if publications.get(request_id, {}).get('status') == 'fbbm_ready':
            raise ValueError('此素材已上传 FBBM，无需重复提交')
        if publications.get(request_id, {}).get('status') in {'upload_queued', 'uploading', 'fbbm_queued'}:
            raise ValueError('此素材已在上传队列中，无需重复提交')
        publications.setdefault(request_id, {}).update(status='upload_queued', approved_sha256=sha256, approved_at=now_iso())
        publications[request_id].pop('error', None)
        publications[request_id].pop('poll_started', None)
        write_json_atomic(path / 'publications.json', publications)
    update(path, 'publishing', f'{request_id} 已确认预览，等待上传 FBBM', active_request=request_id)
    return [sys.executable, '-u', '-m', 'exactcut.workflow', 'publish', batch_id, request_id, instance_marker()]


def verification_binding(entry):
    """Fail closed on ambiguous identity; only durable saved receipts are trusted."""
    publication = entry.get('publication') or {}
    intent = entry.get('intent') or {}
    fbbm = entry.get('fbbm') or {}
    existing = entry.get('verification_only') or {}
    intents = [intent, *entry.get('verification_history', [])]
    receipts = [value.get('receipt') or {} for value in intents]
    def known(values, name):
        values = {str(value) for value in values if value not in (None, '')}
        if len(values) != 1: raise ValueError('交付回执缺失或冲突：' + name)
        return values.pop()
    record_id = known([publication.get('feishu_record_id'), fbbm.get('record_id'), existing.get('record_id'),
                       *[value.get('feishu_record_id') for value in intents],
                       *[value.get('feishu_record_id') for value in receipts], *[value.get('record_id') for value in receipts]], 'record_id')
    material_id = known([fbbm.get('material_id'), existing.get('material_id'), *[value.get('material_id') for value in receipts]], 'material_id')
    sha = known([entry.get('approved_sha256'), publication.get('sha256'), existing.get('approved_sha256'),
                 *[value.get('approved_sha256') for value in intents], *[value.get('sha256') for value in receipts]], 'sha256')
    url = known([publication.get('oss_url'), existing.get('oss_url'), *[value.get('oss_url') for value in receipts]], 'oss_url')
    key = known([publication.get('oss_key'), existing.get('oss_key'), *[value.get('oss_key') for value in intents],
                 *[value.get('oss_key') for value in receipts]], 'oss_key')
    return {'record_id': record_id, 'material_id': material_id, 'approved_sha256': sha, 'oss_url': url, 'oss_key': key}


def queue_verify_delivery(batch_id, request_id, sha256, *, _evidence=None, _job_id=None):
    path = folder(batch_id)
    batch_control.ensure_editable(path, execution=True)
    if not runtime(path).get('request_parallel') and runtime(path)['status'] in ACTIVE - {'publishing'}:
        raise ValueError('当前批次正在制作，请完成后再核实')
    approval_evidence(batch_id, request_id, sha256, _evidence)
    with publication_lock(path):
        publications = read_json(path / 'publications.json')
        entry = publications[request_id]
        if not delivery_blocked(entry): raise ValueError('仅待核实交付可继续查询')
        binding = verification_binding(entry)
        if binding['approved_sha256'] != sha256: raise ValueError('确认校验值与原交付不一致')
        if _job_id:
            # A same-file write-ahead handoff: only a durable matching poll job
            # commits this transition. A crash before save restores the prior intent.
            entry['verification_queue'] = {'job_id': _job_id, 'previous': copy.deepcopy(entry)}
        entry.setdefault('verification_history', []).append(entry.get('intent') or {})
        entry.update(verification_only=binding, status='fbbm_queued', poll_started=time.time())
        entry['intent'] = {'phase': 'verification', 'status': 'verified', **binding, 'created_at': now_iso()}
        entry.pop('error', None)
        publications[request_id] = entry
        write_json_atomic(path / 'publications.json', publications)
    update(path, 'publishing', f'{request_id}：只读核实原记录，等待查询位置', active_request=request_id)
    return [sys.executable, '-u', '-m', 'exactcut.workflow', 'poll', batch_id, request_id, instance_marker()]


def recover_verification_queue(jobs, *, batch_id=None):
    """Commit saved verification jobs; compensate orphaned current/legacy handoffs."""
    paths = [folder(batch_id)] if batch_id else [file.parent for file in BATCHES.glob('*/publications.json')]
    for path in paths:
        file = path / 'publications.json'
        if not file.exists(): continue
        with publication_lock(path):
            publications = read_json(file)
            changed = repaired = False
            for rid, entry in publications.items():
                transaction = entry.get('verification_queue')
                legacy = (entry.get('status') in {'fbbm_queued', 'publish_failed'} and entry.get('verification_only')
                          and entry.get('intent', {}).get('phase') == 'verification')
                if not transaction and not legacy: continue
                matched = any(job.get('batch_id') == path.name and job.get('request_id') == rid
                              and job.get('action') == 'verify-delivery' and job.get('phase') == 'poll'
                              and job.get('verification_only') == entry.get('verification_only')
                              # Existence commits the handoff, including durable
                              # resource/due-time waits and future wait states.
                              # A terminal job is also evidence of a saved handoff;
                              # only a missing job warrants compensation.
                              and (not transaction or job.get('id') == transaction.get('job_id')) for job in jobs)
                if matched:
                    if transaction: entry.pop('verification_queue'); changed = True
                    continue
                if transaction:
                    entry = transaction['previous']
                else:
                    entry.update(status='unverified', error='核实任务未落盘，可再次只读核实')
                    entry['intent']['status'] = 'pending'
                publications[rid] = entry
                changed = repaired = True
            if changed: write_json_atomic(file, publications)
        if repaired:
            update(path, 'interrupted', '核实交接未完成，已恢复待核实；禁止上传重放', executor_running=False)


def add_feedback(batch_id, data):
    path = folder(batch_id)
    request_id = data.get('request_id')
    manifest, request = request_for(path, request_id)
    message = str(data.get('message', '')).strip()
    if not 1 <= len(message) <= 5000:
        raise ValueError('请填写 1～5000 字的反馈')
    category = str(data.get('category', '其他'))
    if category not in {'剪辑节奏', '剧情衔接', 'Hook 字幕', '画面声音', '安全检查', '其他'}:
        raise ValueError('未知反馈类型')
    current_record = result_records(path).get(request_id, {})
    entry = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'request_id': request_id, 'drama_id': manifest.drama_id,
             'drama_title': manifest.drama_title, 'filename': request.output_filename, 'sha256': current_record.get('output_sha256'),
             'category': category, 'message': message, 'created_at': now_iso(), 'status': '待复盘'}
    FEEDBACK.mkdir(exist_ok=True)
    write_json_atomic(FEEDBACK / f"{batch_id}-{entry['id']}.json", entry)
    return entry


def feedback_summary(can_access=None):
    entries = [read_json(p) for p in FEEDBACK.glob('*.json')] if FEEDBACK.exists() else []
    if can_access:
        entries = [e for e in entries if can_access(e['batch_id'])]
    entries.sort(key=lambda row: row['created_at'], reverse=True)
    return {'total': len(entries), 'categories': dict(Counter(row['category'] for row in entries)), 'entries': entries[:500]}


def download(path, manifest, project):
    plan = download_plan(manifest)
    update(path, 'downloading', f"准备正片：本批需要 {'、'.join(plan['required'])}", plan=plan,
           download_current_episode=None, download_current_received=0,
           download_current_total=0, download_speed_bps=None)
    checkpoint(path, 'download')
    from .errors import ValidationError
    from .material_download import validate_downloaded_video as check_video
    from .media import resolve_episode_video, validate_manifest_sources
    from .source_cache import ensure, reused_episodes
    found = {}
    skipped = []
    missing = []
    for episode in plan['required']:
        try:
            video = resolve_episode_video(project, episode)
            check_video(video)
        except (ValidationError, RuntimeError, OSError):
            missing.append(episode)
        else:
            found[episode] = video
            skipped.append(episode)
    if missing:
        from .runtime import source_root
        last_sample = None

        def report_download(episode, received, expected):
            nonlocal last_sample
            total_files = 0
            total_bytes = received
            directory = source_root() / manifest.drama_id
            for required in plan['required']:
                video = directory / f'{required}.mp4'
                try:
                    total_bytes += video.stat().st_size
                    total_files += 1
                except OSError:
                    continue
            now = time.monotonic()
            speed = runtime(path).get('download_speed_bps') if last_sample and last_sample[0] == episode else None
            if last_sample and last_sample[0] == episode and received > last_sample[1]:
                elapsed = now - last_sample[2]
                if elapsed > 0:
                    speed = (received - last_sample[1]) / elapsed
            if not last_sample or last_sample[0] != episode or received > last_sample[1]:
                last_sample = (episode, received, now)
            update(path, download_files=total_files, download_bytes=total_bytes,
                   download_current_episode=episode, download_current_received=received,
                   download_current_total=expected, download_speed_bps=speed,
                   download_activity_at=now_iso(), last_progress_at=now_iso())

        found.update(ensure(manifest.drama_id, missing, on_progress=report_download))
        skipped.extend(reused_episodes())
    validate_manifest_sources(manifest, project)
    fields = {'download_complete': True, 'downloaded_episodes': list(found),
              'download_current_episode': None, 'download_speed_bps': None}
    if skipped:
        fields['reused_local_episodes'] = skipped
    update(path, message=f"正片准备完成：{len(found)} 集，所需集数全部到齐", **fields)


def produce(batch_id):
    with execution(folder(batch_id), phase='render'):
        return _produce(batch_id)


def _produce(batch_id, *, render_only=False, request_id=None):
    from .orchestrator import execute_batch
    path = folder(batch_id)
    manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
    if request_id is not None:
        manifest, request = request_for(path, request_id)
        manifest = manifest.model_copy(update={'requests': [request]})
        update(path, request_parallel=True)
    project = ROOT / 'projects' / manifest.drama_id
    try:
        preparation_started = time.monotonic()
        checkpoint(path, 'source_check', request_id=request_id)
        required = download_plan(manifest)['required']
        from .media import resolve_episode_video
        available = True
        try:
            for ep in required:
                validate_downloaded_video(resolve_episode_video(project, ep))
        except (RuntimeError, OSError, ValueError) as exc:
            available = False
            update(path, message=str(exc), source_repair_needed=True)
        if available:
            from .media import validate_manifest_sources
            validate_manifest_sources(manifest, project)
            update(path, message='所需正片已在本地并通过时间检查，直接复用', download_complete=True, reused_local_episodes=required)
        elif render_only:
            raise RuntimeError('正片尚未准备完成，请重新排队准备正片')
        else:
            download(path, manifest, project)
        update(path, preparation_seconds=round(time.monotonic()-preparation_started,3))
        update(path, 'producing', '开始制作：按 Excel 时间线渲染，再检查格式与时长')
        def progress(stage, request_id):
            from .progress import STAGES
            details = result_records(path).get(request_id, {}).get('progress_details') or {}
            checkpoint(path, stage, request_id=request_id if render_only else None, **details)
            if not render_only: update(path, message=f"{request_id}：{STAGES.get(stage, stage)}", active_request=request_id)
        options = {'request_ids': [request_id]} if request_id is not None else {}
        results = execute_batch(project, manifest, load_config(), output_dir=path / 'production', progress=progress, **options)
        selected = {rid: record for rid, record in results['requests'].items()
                    if request_id is None or rid == request_id}
        failed = [r for r in selected.values() if r.get('status') == 'failed']
        if request_id is None:
            update(path, 'partial' if failed else 'review', f'制作结束，{len(results["requests"])-len(failed)} 条可预览，{len(failed)} 条失败', production_complete=not failed)
        else:
            update(path)  # The local results only determine this executor's exit code.
        return 2 if failed else 0
    except Exception as exc:
        from .stage_results import ResourceDeferred
        if isinstance(exc, ResourceDeferred): raise
        diag.event('production.failed', level='error', batch_id=batch_id, request_id=request_id, error=exc)
        if request_id is None: update(path, 'failed', str(exc), error=type(exc).__name__)
        else:
            result_store.patch(path / 'production', request_id, status='failed', error=str(exc), error_type=type(exc).__name__)
            update(path, message=f'{request_id}：制作失败：{exc}')
        return 2


def fetch(batch_id):
    """Prepare required sources using the same cache as draft prefetch."""
    path = folder(batch_id)
    with execution(path):
        try:
            manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
            download(path, manifest, ROOT / 'projects' / manifest.drama_id)
            update(path, 'queued', '正片已齐，等待制作位置')
            return 0
        except Exception as exc:
            from .stage_results import ResourceDeferred
            if isinstance(exc, ResourceDeferred): raise
            diag.event('source.batch_failed', level='error', batch_id=batch_id, error=exc)
            update(path, 'failed', str(exc), error=type(exc).__name__)
            return 2


def render(batch_id, request_id=None):
    with execution(folder(batch_id), request_id, phase='render'):
        return _produce(batch_id, render_only=True, request_id=request_id)


def publication_state(path, manifest, request_id, entry):
    publications = store_publication(path, request_id, entry)
    complete = all(publications.get(r.request_id, {}).get('status') == 'fbbm_ready' for r in manifest.requests)
    pending = any(e.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'} for e in publications.values())
    message = 'FBBM 已就绪' if entry['status'] == 'fbbm_ready' else entry.get('error', '等待 FBBM 就绪')
    update(path, 'publishing' if pending else 'complete' if complete else 'review', f'{request_id}：{message}')


def delivery_blocked(entry):
    return entry.get('status') == 'unverified' or entry.get('intent', {}).get('status') == 'pending'


def delivery_intent(path, request_id, entry, phase, video=None):
    from .publish import OssPublisher
    publication = entry.get('publication') or {}
    target_key = OssPublisher().key_for(video)[0] if video is not None else publication.get('oss_key')
    entry['intent'] = {'id': uuid.uuid4().hex, 'phase': phase, 'status': 'pending',
                       'approved_sha256': entry.get('approved_sha256'), 'oss_key': target_key,
                       'feishu_record_id': publication.get('feishu_record_id'), 'created_at': now_iso()}
    store_publication(path, request_id, entry)


def upload(batch_id, request_id):
    """Persist intent before remote writes; ambiguous writes are never replayed."""
    from .publish import publish_to_queue, guarded_delivery
    path = folder(batch_id)
    with execution(path, request_id, phase='upload'):
        manifest, request = request_for(path, request_id)
        entry = read_json(path / 'publications.json')[request_id]
        started = time.monotonic()
        attempted = False
        try:
            if delivery_blocked(entry) or entry.get('verification_only') or entry.get('verification_history'):
                raise ValueError('外部交付结果待核实，只允许查询原回执，禁止上传')
            video, record = preview_file(batch_id, request_id)
            if record.get('output_sha256') != entry['approved_sha256'] or file_sha256(video) != entry['approved_sha256']:
                raise ValueError('预览确认后成片已变化，停止上传')
            entry['status'] = 'uploading'
            store_publication(path, request_id, entry)
            checkpoint(path, 'upload', request_id=request_id)
            if not entry.get('publication'):
                delivery_intent(path, request_id, entry, 'upload', video)
                attempted = True
                def receipt(value):
                    entry['intent']['receipt'] = value
                    store_publication(path, request_id, entry)
                with guarded_delivery(receipt=receipt, oss_source=record.get('original_oss')):
                    publication = publish_to_queue(video, manifest, request)
                entry['intent']['receipt'] = publication
                store_publication(path, request_id, entry)
                if (not publication.get('feishu_record_id') or publication.get('sha256') != entry['approved_sha256']
                        or publication.get('oss_key') != entry['intent']['oss_key']):
                    raise ValueError('上传返回的对象、校验值或飞书记录无法核实')
                entry['publication'] = publication
                entry['intent'].update(status='verified', verified_at=now_iso(), feishu_record_id=publication['feishu_record_id'])
            entry.update(status='fbbm_queued')
            entry.setdefault('poll_started', time.time())
            entry.pop('error', None)
        except Exception as exc:
            unknown = attempted or delivery_blocked(entry) or bool(entry.get('verification_only') or entry.get('verification_history'))
            diag.event('publication.failed', level='error', batch_id=batch_id, request_id=request_id,
                       unknown_delivery=unknown, error=exc)
            entry.update(status='unverified' if unknown else 'publish_failed', error=('待核实：' if unknown else '') + str(exc))
        metrics = entry.setdefault('stage_seconds', {})
        metrics['upload'] = round(metrics.get('upload', 0) + time.monotonic() - started, 3)
        publication_state(path, manifest, request_id, entry)
        return 0 if entry['status'] in {'fbbm_queued', 'fbbm_ready'} else 2


def poll(batch_id, request_id):
    """One guarded sync (which may ingest/resync/write), then release the lane."""
    from .publish import sync_record_once, verify_record_once, guarded_delivery
    path = folder(batch_id)
    with execution(path, request_id, phase='poll'):
        manifest, request = request_for(path, request_id)
        entry = read_json(path / 'publications.json')[request_id]
        started = time.monotonic()
        attempted = False
        try:
            if delivery_blocked(entry):
                raise ValueError('外部交付结果待核实，禁止自动重放')
            if entry.get('verification_history') and not entry.get('verification_only'):
                raise ValueError('只读核实标识缺失，禁止回退到普通同步')
            if entry.get('status') == 'fbbm_ready': return 0
            config = load_config()['publish']
            entry.setdefault('poll_started', time.time())
            store_publication(path, request_id, entry)
            if time.time() >= entry['poll_started'] + int(config['wait_timeout_seconds']):
                raise RuntimeError('等待 FBBM 就绪超时，可重试继续查询原记录')
            binding = verification_binding(entry) if entry.get('verification_only') else None
            if binding and binding != entry['verification_only']: raise ValueError('只读核实身份已变化')
            record_id = binding['record_id'] if binding else str(entry['publication']['feishu_record_id'])
            delivery_intent(path, request_id, entry, 'poll')
            attempted = True
            def receipt(value):
                entry['intent'].setdefault('receipt', {}).update(value)
                store_publication(path, request_id, entry)
            with guarded_delivery(receipt=receipt):
                result = verify_record_once(binding) if binding else sync_record_once(record_id)
            if result.get('record_id') != record_id:
                raise ValueError('同步返回记录与交付意图不一致')
            entry['intent'].update(status='verified', verified_at=now_iso())
            previous = entry.get('fbbm') or {}
            changed = any(result.get(key) != previous.get(key) for key in ('action', 'video_id', 'material_id', 'fb_status'))
            entry['fbbm'] = result
            diag.event('fbbm.state', batch_id=batch_id, request_id=request_id, record_id=record_id,
                       action=result.get('action'), material_id=result.get('material_id'),
                       video_id=result.get('video_id'), fb_status=result.get('fb_status'),
                       waiting_seconds=round(time.time()-entry['poll_started'], 1))
            checkpoint(path, 'fbbm', request_id=request_id, advanced=changed, platform_action=result.get('action', 'syncing'))
            if result.get('action') == 'ready' and result.get('video_id'):
                entry.update(status='fbbm_ready', completed_at=now_iso()); entry.pop('error', None)
            elif result.get('action') == 'failed':
                attempted = False
                raise RuntimeError('FBBM 同步失败，请查看平台原因后继续查询原记录')
            else: entry['status'] = 'fbbm_queued'
        except Exception as exc:
            unknown = attempted or delivery_blocked(entry) or bool(entry.get('verification_only') or entry.get('verification_history'))
            entry.update(status='unverified' if unknown else 'publish_failed', error=('待核实：' if unknown else '') + str(exc))
            diag.event('fbbm.failed', level='error', batch_id=batch_id, request_id=request_id,
                       unknown_delivery=unknown, error=exc)
        metrics = entry.setdefault('stage_seconds', {})
        metrics['fbbm'] = round(metrics.get('fbbm', 0) + time.monotonic() - started, 3)
        publication_state(path, manifest, request_id, entry)
        return 2 if entry['status'] in {'publish_failed', 'unverified'} else 0


def publish(batch_id, request_id):
    """Compatibility adapter: old CLI waits, but shares delivery insurance."""
    if upload(batch_id, request_id): return 2
    path = folder(batch_id)
    while True:
        if poll(batch_id, request_id): return 2
        if read_json(path / 'publications.json')[request_id]['status'] == 'fbbm_ready': return 0
        time.sleep(max(1, int(load_config()['publish']['poll_seconds'])))


if __name__ == '__main__':
    load_env_file(project_env_file())
    diag.configure()
    diag.install_logging()
    if sys.argv[1] in {'fetch', 'render'}:
        from .stage_results import run_child
        args = [sys.argv[2]]
        if sys.argv[1] == 'render' and len(sys.argv) > 3 and not sys.argv[3].startswith('--instance='):
            args.append(sys.argv[3])
        raise SystemExit(run_child(globals()[sys.argv[1]], *args))
    if sys.argv[1] in {'upload', 'poll'}:
        from .stage_results import run_child
        raise SystemExit(run_child(globals()[sys.argv[1]], sys.argv[2], sys.argv[3]))
    if sys.argv[1] == 'produce':
        from .stage_results import run_child
        raise SystemExit(run_child(produce, sys.argv[2]))
    if sys.argv[1] == 'preview':
        raise SystemExit(make_batch_previews(sys.argv[2]))
    raise SystemExit(publish(sys.argv[2], sys.argv[3]))
