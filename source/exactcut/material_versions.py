"""Version-1 authoritative media lifecycle. No external effects under the media lock.

The external enrollment registry is deliberately independent of a batch directory.
Once either marker exists legacy writers must fail closed, regardless of feature gate.
"""
from __future__ import annotations

import copy
import fcntl
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from .util import read_json, write_json_atomic, json_sha256, file_sha256, now_iso, load_config

DOCUMENT = 'material-media-state'
BLOCKING_DELIVERY = {'pending', 'upload_queued', 'uploading', 'fbbm_queued', 'unverified'}


def enabled():
    from .runtime import environment
    return environment() == 'production' and load_config().get('material_lifecycle', {}).get('enabled', False)


def registry(path):
    return path.parent.parent / 'media-registry' / (path.name + '.json')


def managed(path):
    return (path / 'media-state.json').exists() or registry(path).exists()


def internal(path):
    file = path / 'draft.json'
    return read_json(file).get('media_internal') if file.is_file() else None


def reject_legacy_write(path):
    if managed(path):
        raise ValueError('此项目已纳管，必须使用素材版本操作；禁止旧写入或物理删除')


def durable_write(file, value):
    write_json_atomic(file, value)
    sync_directory(file.parent)


@contextmanager
def locked(path):
    file = path.parent.parent / 'media-locks' / (path.name + '.lock')
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read(path):
    try:
        marker, state = read_json(registry(path)), read_json(path / 'media-state.json')
        if (marker['status'] != 'active' or marker['batch_id'] != path.name
                or marker['enrollment_id'] != state['enrollment_id']
                or state['document_type'] != DOCUMENT or state['schema_version'] != 1
                or state['batch_id'] != path.name or not isinstance(state['revision'], int)
                or not all(isinstance(state[k], dict) for k in ('draft', 'assets', 'versions', 'outbox', 'deliveries', 'mutations'))):
            raise ValueError()
        rows = state['draft']['rows']
        ids = [r['request_id'] for r in rows]
        if len(ids) != len(set(ids)) or any(i not in state['assets'] for i in ids):
            raise ValueError()
        return state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('纳管记录缺失或损坏，已停止写入；请核验登记记录') from exc


def read(path):
    # Atomic replace permits lock-free snapshots; mutations reread under flock.
    return _read(path)


def _write(path, state):
    state['updated_at'] = now_iso()
    durable_write(path / 'media-state.json', state)


def _asset_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[\w-]{1,80}', value):
        raise ValueError('无效素材 ID')
    return value


def _new_asset(row):
    return {'asset_id': row['request_id'], 'row': copy.deepcopy(row), 'desired_generation': 1,
            'deleted_at': None, 'current_version': None, 'candidate_version': None, 'history': []}


def _quiescent(path):
    from . import batch_control
    from . import workflow as w
    state = read_json(path / 'runtime.json')
    if state.get('executor_running') or state.get('status') in w.ACTIVE:
        raise ValueError('项目仍在执行，不能纳管')
    for file in (path.parent.parent / 'jobs').glob('*.json'):
        job = read_json(file)
        if job.get('batch_id') == path.name and job.get('status') in {'queued', 'running', 'waiting_resource'}:
            raise ValueError('项目仍有作业，不能纳管')
    for child in batch_control.recorded_children(path):
        if not batch_control.belongs(child):
            raise ValueError('跨实例执行身份尚无法核实，不能纳管')
        if batch_control.child_state(child) not in {None, 'Z'}:
            raise ValueError('项目执行身份尚未退出，不能纳管')


def enroll(path):
    return _enroll(path)


def _enrollment_files(path):
    # Compare metadata after cold hash verification without reading media while
    # holding the shared naming lock. New/deleted result shards also invalidate.
    files = [path / name for name in ('draft.json', 'confirmed.json', 'runtime.json', 'control.json', 'publications.json', 'production/results.json')]
    for directory in ('production/results', 'production/videos', 'production/previews'):
        files.extend((path / directory).glob('*'))
    identities = {}
    for file in files:
        try: stat = file.lstat()
        except FileNotFoundError: continue
        identities[str(file)] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return identities


def _enroll(path):
    """Explicit, quiescent enrollment. Hashing and legacy inspection occur unlocked."""
    from . import result_store
    from . import workflow as w
    if internal(path):
        raise ValueError('内部执行项目不可纳管')
    if managed(path):
        # A prepared document is committed before the active registry. Recovery
        # only completes that exact pair; missing/corrupt data never falls back.
        with w.naming_lock(), locked(path):
            marker = read_json(registry(path)) if registry(path).exists() else {}
            if marker.get('status') == 'pending':
                state = read_json(path / 'media-state.json') if (path / 'media-state.json').exists() else marker.get('prepared_state')
                if marker.get('prepared_sha256') != json_sha256(state):
                    raise ValueError('纳管准备记录校验失败')
                if not isinstance(state, dict) or state.get('batch_id') != path.name or state.get('document_type') != DOCUMENT:
                    raise ValueError('纳管准备记录格式错误')
                if not (path / 'media-state.json').exists(): durable_write(path / 'media-state.json', state)
                durable_write(registry(path), {k: v for k, v in {**marker, 'status': 'active'}.items() if k != 'prepared_state'})
            return _read(path)
    _quiescent(path)
    identities = _enrollment_files(path)
    document = read_json(path / 'draft.json')
    before = json_sha256(document)
    rows = document['draft']['rows']
    ids = [_asset_id(row.get('request_id')) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('重复素材 ID，不能纳管')
    records = result_store.read(path / 'production')['requests']
    legacy = path / 'production/results.json'
    if legacy.exists():
        for rid, record in read_json(legacy).get('requests', {}).items():
            shard = path / 'production/results' / (rid + '.json')
            if shard.exists() and record.get('output_sha256') != read_json(shard).get('output_sha256'):
                raise ValueError('历史结果与分片校验值冲突，不能纳管')
    publications = read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
    if any(p.get('status') in BLOCKING_DELIVERY or w.delivery_blocked(p) for p in publications.values()):
        raise ValueError('历史交付结果待核实，不能纳管')
    state = {'schema_version': 1, 'document_type': DOCUMENT, 'batch_id': path.name,
             'enrollment_id': uuid.uuid4().hex, 'revision': document['revision'],
             'document': document, 'draft': copy.deepcopy(document['draft']),
             'assets': {r['request_id']: _new_asset(r) for r in rows}, 'versions': {},
             'outbox': {}, 'deliveries': {}, 'mutations': {}, 'deleted_at': None,
             'control': {'paused': False, 'archived': False}, 'updated_at': now_iso()}
    control = read_json(path / 'control.json') if (path / 'control.json').exists() else {}
    state['control'].update({k: bool(control.get(k)) for k in ('paused', 'archived')})
    if (path / 'confirmed.json').exists():
        manifest = read_json(path / 'confirmed.json')
        names = [r['output_filename'] for r in manifest['requests']]
        if len(set(names)) != len(names):
            raise ValueError('重复成片文件名，不能纳管')
        for request in manifest['requests']:
            rid = _asset_id(request['request_id'])
            if rid not in state['assets']:
                raise ValueError('历史素材身份不一致，不能纳管')
            record = copy.deepcopy(records.get(rid, {}))
            if record.get('status') not in w.SAFE:
                continue
            master = path / 'production/videos' / request['output_filename']
            if not master.is_file() or master.is_symlink() or file_sha256(master) != record.get('output_sha256'):
                raise ValueError('历史成片校验失败，不能纳管')
            pub = publications.get(rid, {})
            if pub.get('publication') and pub['publication'].get('sha256') != record['output_sha256']:
                raise ValueError('历史交付与成片校验值不一致，不能纳管')
            preview = path / 'production/previews' / request['output_filename']
            meta = record.get('preview') or {}
            if meta and (not preview.is_file() or preview.is_symlink() or meta.get('master_sha256') != record['output_sha256'] or file_sha256(preview) != meta.get('sha256')):
                raise ValueError('历史预览校验失败，不能纳管')
            vid = uuid.uuid4().hex
            state['versions'][vid] = {'version_id': vid, 'asset_id': rid, 'generation': 1, 'status': 'current',
                                     'legacy': True, 'manifest': {**manifest, 'requests': [request]},
                                     'draft': {**copy.deepcopy(state['draft']), 'rows': [copy.deepcopy(state['assets'][rid]['row'])]},
                                     'record': record, 'master_path': str(master),
                                     'preview_path': str(preview) if meta else None, 'created_at': now_iso()}
            state['assets'][rid]['current_version'] = vid
            if pub:
                did = uuid.uuid4().hex
                state['deliveries'][did] = {'delivery_id': did, 'asset_id': rid, 'version_id': vid,
                                            'status': pub.get('status', 'publish_failed'), 'publication': pub, 'legacy': True}
    with w.naming_lock(), locked(path):
        if managed(path):
            return _read(path)
        _quiescent(path)
        if _enrollment_files(path) != identities or json_sha256(read_json(path / 'draft.json')) != before:
            raise ValueError('项目已更新，请重试纳管')
        marker = {'schema_version': 1, 'batch_id': path.name, 'enrollment_id': state['enrollment_id'],
                  'status': 'pending', 'prepared_sha256': json_sha256(state), 'prepared_state': state}
        durable_write(registry(path), marker)
        durable_write(path / 'media-state.json', state)
        durable_write(registry(path), {k: v for k, v in {**marker, 'status': 'active'}.items() if k != 'prepared_state'})
    return state


def _mutation(path, data, action, operation):
    key = data.get('mutation_id') or data.get('submission_token')
    if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', key):
        raise ValueError('缺少有效幂等标识，请刷新重试')
    digest = json_sha256({'action': action, 'payload': data})
    with locked(path):
        state = _read(path)
        old = state['mutations'].get(key)
        if old:
            if old['payload_sha256'] != digest:
                raise ValueError('幂等标识已用于其他内容')
            return copy.deepcopy({**state, 'mutations': {}, 'mutation': old['result']})
        if data.get('revision') != state['revision']:
            raise ValueError('项目已在其他页面更新，请刷新后重试')
        before_versions, before_deliveries, before_assets = set(state['versions']), set(state['deliveries']), set(state['assets'])
        operation(state)
        state['revision'] += 1
        state['updated_at'] = now_iso()
        message = {'confirm': '内容已确认，等待制作', 'pause': '已请求暂停',
                   'resume': '已请求继续', 'archive': '已归档', 'unarchive': '已取消归档'}.get(action)
        if message:
            state.setdefault('events', []).append({'at': state['updated_at'], 'message': message})
        # The operation receipt is immutable; live project detail is deliberately
        # current on replay. Never store another full version/history snapshot.
        receipt = {'revision': state['revision'], 'action': action,
                   'asset_ids': sorted(set(state['assets']) - before_assets),
                   'version_ids': sorted(set(state['versions']) - before_versions),
                   'delivery_ids': sorted(set(state['deliveries']) - before_deliveries)}
        state['mutations'][key] = {'payload_sha256': digest, 'result': receipt}
        durable_write(path / 'media-state.json', state)
        return copy.deepcopy({**state, 'mutations': {}, 'mutation': receipt})


def _editable(state, *, execution=False):
    if state['deleted_at'] or state['control']['archived']:
        raise ValueError('项目已删除或归档，请先恢复')
    if execution and state['control']['paused']:
        raise ValueError('项目已暂停，请先继续')


def _blocked(state, rid=None):
    return any(d['status'] in BLOCKING_DELIVERY and (rid is None or d['asset_id'] == rid)
               for d in state['deliveries'].values())


def _invalidate(asset, state):
    asset['desired_generation'] += 1
    if asset['candidate_version']:
        state['versions'][asset['candidate_version']]['status'] = 'superseded'
    asset['candidate_version'] = None


def _semantic(draft, row):
    return json_sha256({'metadata': {k: v for k, v in draft.items() if k != 'rows'},
                       'row': {k: v for k, v in row.items() if k not in {'notes', 'source', 'client_id'}}})


def save_draft(path, data):
    from . import workflow as w
    from .intake import compile_clips, compile_composition
    draft = copy.deepcopy(data.get('draft'))
    if not isinstance(draft, dict) or not isinstance(draft.get('rows'), list) or not all(isinstance(r, dict) for r in draft['rows']):
        raise ValueError('标准化内容格式错误')
    if len(str(draft)) > 1_000_000:
        raise ValueError('草稿内容过大')
    for row in draft['rows']:
        compile_composition(row); compile_clips(row)
    draft['creator2'] = 'agent'
    def apply(state):
        _editable(state)
        # HTTP callers hold the same naming lock as legacy draft saves.
        if w.allocate_sequences(draft, path.name):
            state['document'].setdefault('warnings', []).append('素材命名冲突，已分配新序号；请确认最终文件名')
        ids = set()
        for row in draft['rows']:
            rid = row.get('request_id')
            if rid is None or rid == '':
                rid = row['request_id'] = 'R' + uuid.uuid4().hex
                state['assets'][rid] = _new_asset(row)
            else:
                _asset_id(rid)
                if rid not in state['assets'] or state['assets'][rid]['deleted_at']:
                    raise ValueError('素材 ID 不属于本项目或已删除；新增素材由服务端分配')
            if rid in ids:
                raise ValueError('重复素材 ID')
            ids.add(rid)
            asset = state['assets'][rid]
            if _semantic(state['draft'], asset['row']) != _semantic(draft, row):
                _invalidate(asset, state)
            asset['row'] = copy.deepcopy(row)
        for rid, asset in state['assets'].items():
            if rid not in ids and not asset['deleted_at']:
                if _blocked(state, rid):
                    raise ValueError('素材仍有交付待核实，不能删除')
                asset['deleted_at'] = now_iso(); _invalidate(asset, state)
        state['draft'] = copy.deepcopy(draft)
    return _mutation(path, data, 'save', apply)


def restore(path, rid, data):
    def apply(state):
        if rid is None:
            state['deleted_at'] = None
            for asset in state['assets'].values(): _invalidate(asset, state)
        else:
            _editable(state)
            if rid not in state['assets'] or not state['assets'][rid]['deleted_at']:
                raise ValueError('已删除素材不存在')
            asset = state['assets'][rid]
            asset['deleted_at'] = None; _invalidate(asset, state)
            state['draft']['rows'].append(copy.deepcopy(asset['row']))
    return _mutation(path, data, 'restore:' + str(rid), apply)


def control(path, action, data):
    def apply(state):
        if action == 'delete':
            if _blocked(state): raise ValueError('项目仍有交付待核实，不能删除')
            state['deleted_at'] = now_iso()
            for asset in state['assets'].values(): _invalidate(asset, state)
        elif action == 'pause': state['control']['paused'] = True
        elif action == 'resume':
            if state['control']['archived']: raise ValueError('请先取消归档')
            state['control']['paused'] = False
        elif action == 'archive': state['control'].update(archived=True, paused=True)
        elif action == 'unarchive': state['control']['archived'] = False
        else: raise ValueError('未知操作')
    return _mutation(path, data, action, apply)


def reserved_names(path):
    """Read-only reservations include retained versions, never internal aliases."""
    if managed(path):
        state = read(path)
        return {r['output_filename'] for v in state['versions'].values() for r in v['manifest']['requests']}
    file = path / 'confirmed.json'
    return {r['output_filename'] for r in read_json(file)['requests']} if file.exists() else set()


def confirm(path, data):
    from . import workflow as w
    from .intake import validate_draft
    snapshot = read(path)
    manifest, errors = validate_draft(snapshot['draft'], str(path / 'source.xlsx'))
    if errors or manifest is None: raise ValueError('\n'.join(errors))
    serialized = manifest.model_dump(mode='json')
    from .material_workers import local_pair_valid
    retryable = {rid for rid, asset in snapshot['assets'].items()
                 if asset['candidate_version'] and snapshot['versions'][asset['candidate_version']]['status'] == 'failed'
                 and local_pair_valid(path.parent / snapshot['versions'][asset['candidate_version']]['child_id'], rid)}
    # Caller holds global naming lock; no media lock is held while scanning.
    names = {r['output_filename'] for r in serialized['requests']}
    if len(names) != len(serialized['requests']): raise ValueError('素材命名重复')
    for file in w.BATCHES.glob('*/draft.json'):
        if file.parent != path and not internal(file.parent) and names & reserved_names(file.parent):
            raise ValueError('素材命名重复，请修改素材序号')
    def apply(state):
        _editable(state, execution=True)
        if state['draft'] != snapshot['draft']: raise ValueError('草稿已更新')
        for request in serialized['requests']:
            rid = request['request_id']; asset = state['assets'][rid]
            current = state['versions'].get(asset['current_version'], {})
            candidate = state['versions'].get(asset['candidate_version'], {})
            if candidate.get('status') in {'queued', 'processing'}: continue
            if current.get('generation') == asset['desired_generation']: continue
            if rid in retryable and candidate.get('generation') == asset['desired_generation']:
                job_id = uuid.uuid4().hex
                candidate.update(status='queued', upload_job_id=job_id)
                candidate.pop('execution', None)
                candidate.pop('error', None)
                state['outbox'][job_id] = {**copy.deepcopy(candidate), 'kind': 'media-upload', 'job_id': job_id, 'status': 'pending'}
                continue
            vid, child_id = uuid.uuid4().hex, uuid.uuid4().hex
            version = {'version_id': vid, 'asset_id': rid, 'generation': asset['desired_generation'],
                       'status': 'queued', 'child_id': child_id, 'manifest': {**serialized, 'requests': [request]},
                       'draft': {**copy.deepcopy(state['draft']), 'rows': [copy.deepcopy(asset['row'])]},
                       'config_sha256': json_sha256(load_config()), 'created_at': now_iso()}
            state['versions'][vid] = version; asset['candidate_version'] = vid
            state['outbox'][child_id] = {**copy.deepcopy(version), 'kind': 'candidate', 'status': 'pending', 'job_id': uuid.uuid5(uuid.NAMESPACE_URL, child_id + ':fetch').hex}
    return _mutation(path, data, 'confirm', apply)


def queue_delivery(path, data):
    if not delivery_enabled(): raise ValueError('新版交付尚未启用；当前成片可预览下载')
    target = delivery_target()
    rid, vid = data.get('request_id'), data.get('version_id')
    snapshot = read(path)
    version = snapshot['versions'].get(vid, {})
    master = Path(version.get('master_path', ''))
    if not master.is_file() or file_sha256(master) != data.get('sha256'):
        raise ValueError('成片校验失败，请重新预览')
    def apply(state):
        _editable(state, execution=True)
        asset = state['assets'].get(rid)
        if not asset or asset['deleted_at'] or asset['current_version'] != vid or state['versions'][vid]['record']['output_sha256'] != data.get('sha256'):
            raise ValueError('成片版本已变化，请重新预览确认')
        if _blocked(state, rid): raise ValueError('以前的交付仍待核实，禁止重复交付')
        filename = version['manifest']['requests'][0]['output_filename']
        for previous in state['deliveries'].values():
            old = state['versions'][previous['version_id']]
            if old['manifest']['requests'][0]['output_filename'] == filename and old['record']['output_sha256'] != data['sha256']:
                raise ValueError('交付文件名冲突：该名称已绑定其他成片，请修改素材序号后重新制作')
            if previous['version_id'] == vid and previous['status'] == 'fbbm_ready':
                raise ValueError('此版本已交付')
        did, child_id = uuid.uuid4().hex, uuid.uuid4().hex
        delivery = {'delivery_id': did, 'asset_id': rid, 'version_id': vid, 'child_id': child_id,
                    'status': 'pending', 'sha256': data['sha256'], 'target_sha256': target, 'created_at': now_iso()}
        state['deliveries'][did] = delivery
        state['outbox'][child_id] = {**delivery, 'kind': 'delivery', 'generation': version['generation'],
                                   'manifest': copy.deepcopy(version['manifest']), 'status': 'pending',
                                   'job_id': uuid.uuid5(uuid.NAMESPACE_URL, child_id + ':upload').hex}
    return _mutation(path, data, 'delivery', apply)


def recover_outbox():
    from .material_workers import recover_outbox as recover
    return recover()


def current_file(path, rid, *, lightweight=False):
    state = read(path); asset = state['assets'].get(rid)
    if state['deleted_at'] or not asset or asset['deleted_at'] or not asset['current_version']:
        raise ValueError('当前素材不存在或尚未制作完成')
    version = state['versions'][asset['current_version']]
    file = Path(version['preview_path'] if lightweight and version.get('preview_path') else version['master_path'])
    if not file.resolve().is_relative_to(path.resolve()) or file.is_symlink() or not file.is_file():
        raise ValueError('当前媒体路径无效')
    record = copy.deepcopy(version['record'])
    record['media_version_id'] = asset['current_version']
    return file, record


def save_receipt(path, rid, record, video, receipt, *, original=False):
    from .preview_oss import _identity
    identity = _identity(video)
    with locked(path):
        state = _read(path); asset = state['assets'].get(rid)
        if state['deleted_at'] or not asset or asset['deleted_at'] or asset['current_version'] != record.get('media_version_id'):
            raise ValueError('当前媒体版本已变化')
        version = state['versions'][asset['current_version']]
        selected = version['master_path'] if original else version.get('preview_path') or version['master_path']
        expected = version['record']['output_sha256'] if original or not version.get('preview_path') else version['record']['preview']['sha256']
        if str(video) != selected or receipt.get('identity') != identity or receipt.get('sha256') != expected:
            raise ValueError('当前媒体回执不匹配')
        if original or not version.get('preview_path'): version['record']['original_oss'] = receipt
        else: version['record']['preview']['oss'] = receipt
        _write(path, state)


def detail(path):
    from . import workflow as w
    from .intake import validate_draft, download_plan
    from .error_guidance import guidance, redact
    from urllib.parse import quote
    state = read(path)
    document = copy.deepcopy(state['document'])
    manifest, errors = validate_draft(state['draft'], str(path / 'source.xlsx'))
    requests = {r.request_id: r for r in manifest.requests} if manifest else {}
    from .progress import observation
    assets, candidates, downloads = [], [], []
    for row in state['draft']['rows']:
        rid = row['request_id']; asset = state['assets'][rid]
        current = state['versions'].get(asset['current_version'], {})
        candidate = state['versions'].get(asset['candidate_version'], {})
        record = current.get('record', {})
        deliveries = [d for d in state['deliveries'].values() if d['asset_id'] == rid]
        latest = deliveries[-1] if deliveries else {}
        publication = latest.get('publication', {})
        request = requests.get(rid)
        filename = current['manifest']['requests'][0]['output_filename'] if current else request.output_filename if request else ''
        item = {'request_id': rid, 'title': row.get('title_cn') or rid, 'filename': filename,
                'status': record.get('status', 'pending'), 'sha256': record.get('output_sha256'),
                'version_id': asset['current_version'], 'desired_generation': asset['desired_generation'],
                'duration_ms': record.get('duration_ms'), 'stage_seconds': record.get('stage_seconds', {}),
                'unit_checks': record.get('unit_checks', []), 'hook_warnings': record.get('hook_warnings', []),
                'preview_warning': None, 'error': '', 'error_info': None, 'can_continue_fbbm': False,
                'deliveries': [{'delivery_id': d['delivery_id'], 'version_id': d['version_id'], 'status': d['status']} for d in deliveries]}
        if latest.get('version_id') == asset['current_version']:
            item['status'] = latest.get('status', item['status'])
            item['video_id'] = (publication.get('fbbm') or {}).get('video_id')
        if current and not state['deleted_at']:
            endpoint = 'preview' if current.get('preview_path') else 'video'
            item.update(preview=f'/api/batches/{path.name}/{endpoint}/{quote(rid, safe="")}',
                        original=f'/api/batches/{path.name}/download/{quote(rid, safe="")}',
                        preview_kind='lightweight' if current.get('preview_path') else 'original')
        if candidate:
            progress, child_runtime = {}, {}
            child = w.BATCHES / candidate['child_id']
            if (child / 'draft.json').exists():
                progress = w.result_records(child).get(rid, {})
                child_runtime = w.runtime(child)
            item['candidate'] = {'version_id': candidate['version_id'], 'status': candidate['status'],
                                 'error': redact(candidate.get('error')), 'stage': progress.get('stage'),
                                 'generation': candidate['generation'],
                                 'observation': observation(progress or child_runtime, paused=state['control']['paused']),
                                 'stage_seconds': progress.get('stage_seconds', {}),
                                 'error_info': guidance(candidate.get('error'), progress.get('stage')) if candidate.get('error') else None}
            downloads.append(bool(child_runtime.get('download_complete')))
            candidates.append(candidate['status'])
            if not current:
                item['status'] = 'failed' if candidate['status'] == 'failed' else 'processing'
            item['error'] = redact(candidate.get('error'))
            item['error_info'] = guidance(candidate.get('error'), progress.get('stage')) if candidate.get('error') else None
        else:
            downloads.append(bool(current))
        assets.append(item)
    status = 'producing' if 'processing' in candidates else 'queued' if 'queued' in candidates else 'partial' if 'failed' in candidates else 'review' if any(a.get('preview') for a in assets) else 'draft'
    if _blocked(state): status = 'publishing'
    control = state['control']
    events = list(state.get('events', []))
    history_paths = {path, *(w.BATCHES / v['child_id'] for v in state['versions'].values() if v.get('child_id'))}
    for history_path in history_paths:
        file = history_path / 'runtime.json'
        if file.exists():
            events.extend(read_json(file).get('events', []))
    events.sort(key=lambda event: event['at'])
    runtime = {'status': 'paused' if control['paused'] and status in w.ACTIVE else status,
               'underlying_status': status, 'paused': control['paused'], 'archived': control['archived'] or bool(state['deleted_at']),
               'message': '当前成片保留；可在本项目编辑并制作新版本', 'updated_at': state['updated_at'],
               'events': events, 'request_parallel': True, 'executor_running': 'processing' in candidates,
               'observation': {}, 'error_info': None,
               'download_complete': bool(downloads) and all(downloads)}
    feedback = sorted([read_json(f) for f in w.FEEDBACK.glob(path.name + '-*.json')], key=lambda e: e['created_at'], reverse=True)
    document.update(revision=state['revision'], draft=copy.deepcopy(state['draft']), assets=assets,
                    media_managed=True, media_delivery_enabled=delivery_enabled(), deleted_at=state['deleted_at'], runtime=runtime, errors=errors,
                    plan=download_plan(manifest) if manifest else None, timing_explanation=[], feedback=feedback,
                    feedback_count=len(feedback), deleted_assets=[{'request_id': a['asset_id'], 'title': a['row'].get('title_cn') or a['asset_id']}
                        for a in state['assets'].values() if a['deleted_at']])
    return document


def save_ending_image(path, rid, payload):
    import base64
    import hashlib
    state = read(path); _editable(state)
    if rid not in state['assets'] or state['assets'][rid]['deleted_at']: raise ValueError('素材不存在')
    raw = base64.b64decode(payload or '', validate=True)
    if not raw or len(raw) > 4_000_000: raise ValueError('结尾图片需在 4 MB 以内')
    if raw.startswith(b'\x89PNG\r\n\x1a\n'): ext = '.png'
    elif raw.startswith(b'\xff\xd8\xff'): ext = '.jpg'
    elif raw.startswith(b'RIFF') and raw[8:12] == b'WEBP': ext = '.webp'
    else: raise ValueError('结尾图片只接受 PNG、JPEG 或 WebP')
    name = hashlib.sha256(raw).hexdigest() + ext
    target = path / 'endings' / name
    target.parent.mkdir(exist_ok=True)
    try:
        with target.open('xb') as stream: stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    except FileExistsError:
        if target.read_bytes() != raw: raise ValueError('不可变图片校验失败')
    return {'image': name}


def queue_verification(path, data):
    from . import workflow as w
    state = read(path); did = data.get('delivery_id'); delivery = state['deliveries'].get(did)
    if not delivery or delivery.get('legacy') or delivery['status'] != 'unverified':
        raise ValueError('仅待核实的独立交付可以查询')
    child = w.BATCHES / delivery['child_id']
    entry = read_json(child / 'publications.json')[delivery['asset_id']]
    binding = w.verification_binding(entry)
    if binding['approved_sha256'] != delivery['sha256']: raise ValueError('交付校验值冲突')
    def apply(state):
        _editable(state, execution=True)
        if state['deliveries'][did]['status'] != 'unverified': raise ValueError('交付状态已更新')
        if any(e['kind'] == 'verification' and e['delivery_id'] == did for e in state['outbox'].values()):
            raise ValueError('已存在核实任务，请等待原任务结果')
        job_id = uuid.uuid4().hex
        state['outbox'][job_id] = {**copy.deepcopy(delivery), 'kind': 'verification', 'job_id': job_id,
                                  'binding': binding, 'status': 'pending'}
    return _mutation(path, data, 'verify-delivery', apply)


def delivery_enabled():
    return enabled() and load_config().get('material_lifecycle', {}).get('delivery_enabled', False)


def delivery_target():
    """Fingerprint documented publisher destination/account settings, never secrets."""
    from .publish import OssPublisher
    publisher = OssPublisher()
    values = {'oss_bucket': publisher.bucket_name, 'oss_endpoint': publisher.endpoint,
              'oss_prefix': publisher.prefix, 'oss_public_base': publisher.public_base}
    for key in ('FEISHU_BITABLE_APP_TOKEN', 'FEISHU_BITABLE_TABLE_ID', 'FEISHU_APP_ID',
                'CF_ADS_BASE_URL', 'CF_ADS_USERNAME', 'OPENCLAW_SOURCE_USER'):
        values[key] = os.getenv(key)
    return json_sha256(values)


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
