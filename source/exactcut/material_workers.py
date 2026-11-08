"""Isolated execution containers and outbox adapter for managed media parents."""
from __future__ import annotations
import copy
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from . import material_versions as m
from .util import read_json, file_sha256, now_iso
from .runtime import instance_marker


def lineage(parent, event):
    value = {'parent_batch_id': parent.name, 'asset_id': event['asset_id'], 'version_id': event['version_id'],
             'generation': event['generation'], 'kind': event['kind']}
    if event['kind'] == 'delivery': value['delivery_id'] = event['delivery_id']
    return value


def materialize(parent, event):
    from . import workflow as w
    child = w.BATCHES / event['child_id']
    identity = lineage(parent, event)
    state = m.read(parent)
    version = state['versions'][event['version_id']]
    document = copy.deepcopy(state['document'])
    document.update(id=child.name, revision=1, media_internal=identity,
                    draft=copy.deepcopy(version.get('draft', state['draft'])))
    document['draft']['rows'] = [copy.deepcopy(state['assets'][event['asset_id']]['row'])] if 'draft' not in version else version['draft']['rows']
    child.mkdir(parents=True, exist_ok=True)
    if (child / 'draft.json').exists():
        if m.internal(child) != identity: raise ValueError('内部执行身份冲突')
    else: m.durable_write(child / 'draft.json', document)
    if (child / 'confirmed.json').exists():
        if read_json(child / 'confirmed.json') != event['manifest']: raise ValueError('内部确认快照冲突')
    else: m.durable_write(child / 'confirmed.json', event['manifest'])
    if not (child / 'runtime.json').exists():
        m.durable_write(child / 'runtime.json', {'status': 'queued', 'request_parallel': True, 'message': '等待执行', 'events': [], 'updated_at': now_iso()})
    access = parent / 'access.json'
    if access.exists() and not (child / 'access.json').exists(): m.durable_write(child / 'access.json', read_json(access))
    if event['kind'] == 'delivery':
        rid = event['asset_id']; request = event['manifest']['requests'][0]
        target = child / 'production/videos' / request['output_filename']
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name('.' + uuid.uuid4().hex + '.tmp')
            shutil.copyfile(m.media_file(parent, version['master_path']), temporary)
            if file_sha256(temporary) != event['sha256']: raise ValueError('交付副本校验失败')
            try: os.link(temporary, target)
            except FileExistsError: pass
            finally: temporary.unlink(missing_ok=True)
        if file_sha256(target) != event['sha256']: raise ValueError('交付副本身份冲突')
        result_file = child / 'production/results' / (rid + '.json')
        if not result_file.exists():
            record = copy.deepcopy(version['record']); record['output'] = str(target)
            # Delivery owns a verified local copy. Parent edit paths must never
            # escape into the child resolver (which deliberately rejects them).
            record.pop('edited_media', None)
            if record.get('original_oss'):
                from .preview_oss import _identity
                record['original_oss']['identity'] = _identity(target)
            m.durable_write(result_file, record)
        pubs = child / 'publications.json'
        if not pubs.exists():
            m.durable_write(pubs, {rid: {'status': 'upload_queued', 'approved_sha256': event['sha256'], 'approved_at': now_iso()}})
    return child


def _job(parent, event, child):
    from .accounts import owner
    phase = 'fetch' if event['kind'] == 'candidate' else 'upload'
    command = [sys.executable, '-u', '-m', 'exactcut.workflow', phase, child.name]
    if phase == 'upload': command.append(event['asset_id'])
    return {'id': event['job_id'], 'batch_id': child.name, 'project': event['manifest']['drama_id'],
            'owner': owner(parent), 'actor': owner(parent), 'action': 'start' if phase == 'fetch' else 'publish',
            'request_id': event['asset_id'], 'phase': phase, 'lane': phase, 'created': time.time(),
            'status': 'queued', 'instance': instance_marker(), 'command': [*command, instance_marker()]}


def _merge_delivery(parent, event, entry, *, verification_job_id=None):
    with m.locked(parent):
        state = m._read(parent)
        if verification_job_id:
            verification = state['outbox'].get(verification_job_id)
            if not verification or verification['status'] == 'superseded': return
        delivery = state['deliveries'][event['delivery_id']]
        status = entry.get('status', delivery['status'])
        intent = entry.get('intent', {})
        preparation_failed = (status == 'publish_failed' and intent.get('phase') == 'upload'
                              and intent.get('status') == 'preparation_failed'
                              and not any(entry.get(k) for k in ('publication', 'fbbm', 'verification_only', 'verification_history'))
                              and not intent.get('receipt') and not intent.get('feishu_record_id'))
        if delivery['status'] == 'unverified' and status != 'fbbm_ready' and not preparation_failed:
            status = 'unverified'
        # Active uploads/polls already fence mutations; pending intent alone is
        # not a failure. The scheduler classifies an executor loss as unverified.
        if intent.get('status') == 'pending' and status not in {'uploading', 'fbbm_queued'}:
            status = 'unverified'
        if delivery['status'] == status and delivery.get('publication') == entry: return
        delivery.update(status=status, publication=copy.deepcopy(entry))
        state['revision'] += 1; m._write(parent, state)


def recover_outbox():
    """Called outside old locks. A replay never repairs or replays external intent."""
    from . import workflow as w
    jobs = w.ROOT / '.web-state/jobs'
    from . import job_records
    snapshot = []
    for file in jobs.glob('*.json'):
        job = job_records.load(file, jobs, w.BATCHES, isolate=True, reader=read_json)
        if job is not None: snapshot.append((file, job))
    from .finished_skip import Check, JobSnapshot, finish_checks
    snapshot = JobSnapshot(snapshot)
    finished_checks = []
    for marker in (w.BATCHES.parent / 'media-registry').glob('*.json'):
        parent = w.BATCHES / marker.stem
        check = Check('outbox', parent, jobs, snapshot)
        if check.skip:
            continue
        try:
            _recover_parent(parent, jobs, snapshot)
        except (OSError, ValueError, KeyError) as exc:
            from . import diagnostics as diag
            diag.event('media.recovery_failed', level='warning', batch_id=parent.name, error=exc)
        else:
            finished_checks.append(check)
    finish_checks(finished_checks)


def _recover_parent(parent, jobs, snapshot):
    """单个父项目的原恢复顺序；文件依赖由 finished_skip.outbox_inputs 维护。"""
    from . import workflow as w
    cancel_invalid_children(parent, snapshot=snapshot)
    reconcile_candidates(parent, jobs, snapshot=snapshot)
    state = m.read(parent)
    for event in list(state['outbox'].values()):
        if event['kind'] == 'media-upload':
            if not state['deleted_at'] and not state['control']['paused'] and not state['control']['archived']:
                recover_media_upload(parent, event)
            continue
        if event['kind'] == 'verification':
            if not state['deleted_at'] and not state['control']['paused'] and not state['control']['archived']:
                recover_verification(parent, event)
            continue
        child = w.BATCHES / event['child_id']
        if event['kind'] == 'delivery' and (child / 'publications.json').exists():
            entry = read_json(child / 'publications.json').get(event['asset_id'], {})
            _merge_delivery(parent, event, entry)
            if (entry.get('intent') or entry.get('status') != 'upload_queued'
                    or m.read(parent)['deliveries'][event['delivery_id']]['status'] == 'unverified'): continue
        if state['deleted_at'] or state['control']['paused'] or state['control']['archived']: continue
        if event['kind'] == 'candidate':
            asset = state['assets'][event['asset_id']]
            if asset['deleted_at'] or asset['candidate_version'] != event['version_id']: continue
            version = state['versions'][event['version_id']]
            if version['status'] not in {'queued', 'processing'} or version.get('upload_job_id'): continue
        child = materialize(parent, event)
        job = _job(parent, event, child)
        target = jobs / (job['id'] + '.json')
        if not target.exists(): m.durable_write(target, job)
        with m.locked(parent):
            latest = m._read(parent)
            if latest['outbox'][event['child_id']]['status'] != 'materialized':
                latest['outbox'][event['child_id']]['status'] = 'materialized'
                m._write(parent, latest)


def require_oss():
    from .runtime import environment
    return environment() == 'production'


def _context(job):
    from . import workflow as w
    child = w.BATCHES / job['batch_id']
    identity = m.internal(child)
    if not identity: return None
    parent = w.BATCHES / identity['parent_batch_id']
    state = m.read(parent)
    event = state['outbox'].get(child.name)
    if not event or lineage(parent, event) != identity:
        raise ValueError('内部执行身份未登记')
    return parent, child, identity


def _actual(job):
    from . import workflow as w
    file = w.ROOT / '.web-state/jobs' / (job['id'] + '.json')
    actual = read_json(file)
    if any(actual.get(k) != job.get(k) for k in ('id', 'batch_id', 'request_id', 'phase', 'attempt', 'process_token')):
        raise ValueError('执行尝试已被替代')
    if not job.get('process_token') or job.get('attempt') != 1:
        raise ValueError('候选必须使用独立执行尝试')
    return {'job_id': job['id'], 'attempt': job['attempt'], 'process_token': job['process_token']}


def _eligible(state, identity, child):
    asset = state['assets'][identity['asset_id']]
    version = state['versions'][identity['version_id']]
    return (not state['deleted_at'] and not asset['deleted_at'] and not state['control']['paused']
            and not state['control']['archived'] and asset['candidate_version'] == identity['version_id']
            and asset['desired_generation'] == identity['generation'] and version['child_id'] == child.name
            and version['status'] in {'queued', 'processing'})


def stage_enter(job):
    context = _context(job)
    if not context: return
    parent, child, identity = context
    if identity['kind'] == 'delivery':
        state = m.read(parent)
        if job.get('action') == 'verify-delivery':
            event = state['outbox'].get(job['id'])
            if not event or event['status'] == 'superseded': raise ValueError('核实任务已被替代')
        delivery = state['deliveries'][identity['delivery_id']]
        if delivery.get('target_sha256') != m.delivery_target():
            raise ValueError('交付目标配置已变化，停止执行；请核对原目标')
        return
    if job.get('phase') not in {'render', 'media-upload'}: return
    actual = _actual(job)
    with m.locked(parent):
        state = m._read(parent)
        if not _eligible(state, identity, child): raise ValueError('候选已取消、暂停或被替代')
        version = state['versions'][identity['version_id']]
        if job.get('phase') == 'media-upload':
            if version.get('upload_job_id') != job['id']: raise ValueError('媒体补传尝试已失效')
        elif version.get('upload_job_id'): raise ValueError('原渲染尝试已转为媒体补传')
        elif version.get('execution') and version['execution'] != actual: raise ValueError('候选执行尝试冲突')
        version.update(execution=actual, status='processing')
        m._write(parent, state)


def _receipt(receipt, path, digest, parent, rid):
    from .preview_oss import object_prefix, _identity
    from urllib.parse import quote
    if (not isinstance(receipt, dict) or receipt.get('sha256') != digest
            or receipt.get('bytes') != path.stat().st_size or receipt.get('identity') != _identity(path)
            or receipt.get('key') != f'{object_prefix()}/{parent.name}/{quote(rid, safe="")}/{digest}.mp4'
            or not receipt.get('bucket') or not receipt.get('endpoint')):
        raise ValueError('正式成片与预览均需完成 OSS 校验')


def freeze(parent, child, identity, job):
    from . import workflow as w
    from .preview_oss import _identity
    rid, vid = identity['asset_id'], identity['version_id']
    version = m.read(parent)['versions'][vid]
    name = version['manifest']['requests'][0]['output_filename']
    record = copy.deepcopy(w.result_records(child).get(rid, {}))
    master, preview = child / 'production/videos' / name, child / 'production/previews' / name
    digest, meta = record.get('output_sha256'), record.get('preview') or {}
    if record.get('status') not in w.SAFE or meta.get('master_sha256') != digest:
        raise ValueError('候选成片与预览未成对完成')
    for file, expected in ((master, digest), (preview, meta.get('sha256'))):
        if not file.is_file() or file.is_symlink() or file_sha256(file) != expected:
            raise ValueError('候选媒体校验失败')
    if require_oss():
        _receipt(record.get('original_oss'), master, digest, parent, rid)
        _receipt(meta.get('oss'), preview, meta.get('sha256'), parent, rid)
    destination = parent / 'production/versions' / rid / vid / job['process_token']
    seal = destination / 'seal.json'
    if seal.exists():
        saved = read_json(seal)
        if saved['execution'] != version['execution']: raise ValueError('封存执行身份冲突')
        for file, expected in ((destination / 'master' / name, digest), (destination / 'preview' / name, meta['sha256'])):
            if file_sha256(file) != expected: raise ValueError('封存媒体校验失败')
        return saved
    destination.parent.mkdir(parents=True, exist_ok=True)
    for directory in (destination.parent, *destination.parent.parents):
        m.sync_directory(directory)
        if directory == parent: break
    temporary = destination.parent / ('.freeze-' + uuid.uuid4().hex)
    temporary.mkdir()
    for source, directory, expected in ((master, 'master', digest), (preview, 'preview', meta['sha256'])):
        target = temporary / directory / name; target.parent.mkdir()
        before = _identity(source)
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing); outgoing.flush(); os.fsync(outgoing.fileno())
        if _identity(source) != before or file_sha256(target) != expected or file_sha256(source) != expected:
            raise ValueError('封存期间媒体已变化')
    record['output'] = str(destination / 'master' / name)
    frozen = {'record': record, 'master_path': record['output'], 'preview_path': str(destination / 'preview' / name),
              'execution': version['execution']}
    # Renaming keeps copied inodes: identity receipts are rebound only after
    # source, copied bytes and original OSS receipts were checked above.
    for key, target in (('original_oss', temporary / 'master' / name), ('preview', temporary / 'preview' / name)):
        receipt = record.get(key) if key == 'original_oss' else record['preview'].get('oss')
        if receipt: receipt['identity'] = _identity(target)
    m.sync_directory(temporary / 'master'); m.sync_directory(temporary / 'preview')
    m.durable_write(temporary / 'seal.json', frozen)
    try: os.rename(temporary, destination)
    except OSError:
        if not seal.exists(): raise
        saved = read_json(seal)
        if saved != frozen: raise ValueError('封存目标已存在，禁止覆盖')
    m.sync_directory(destination.parent)
    # Rename can update ctime on some filesystems. Rebinding to destination is
    # safe only for this new frozen copy, never for a writable render output.
    return read_json(seal)


def stage_finish(job, code):
    context = _context(job)
    if not context: return
    parent, child, identity = context
    if identity['kind'] == 'delivery':
        if job.get('action') == 'verify-delivery':
            event = m.read(parent)['outbox'].get(job['id'])
            if not event or event['status'] == 'superseded': return
        file = child / 'publications.json'
        if file.exists():
            event = m.read(parent)['outbox'][child.name]
            _merge_delivery(parent, event, read_json(file).get(identity['asset_id'], {}),
                            verification_job_id=job['id'] if job.get('action') == 'verify-delivery' else None)
        return
    if job.get('phase') not in {'fetch', 'render', 'media-upload'}: return
    state = m.read(parent); version = state['versions'][identity['version_id']]
    if version['status'] == 'current': return
    if not _eligible(state, identity, child): return
    if job.get('phase') == 'fetch' and code == 0: return
    if job.get('phase') in {'render', 'media-upload'}:
        try: actual = _actual(job)
        except (ValueError, OSError): return
        if version.get('execution') != actual: return
    frozen, error = None, None
    try:
        if code: raise ValueError('候选执行失败；原成片已保留')
        actual = _actual(job)
        if version.get('execution') != actual: raise ValueError('执行尝试已被替代')
        frozen = freeze(parent, child, identity, job)
    except (ValueError, OSError, KeyError) as exc:
        error = str(exc)
    with m.locked(parent):
        state = m._read(parent)
        if not _eligible(state, identity, child): return
        version = state['versions'][identity['version_id']]
        if frozen:
            try: actual = _actual(job)
            except (ValueError, OSError): return
            if version.get('execution') != actual: return
            asset = state['assets'][identity['asset_id']]
            if asset['current_version']:
                asset['history'].append(asset['current_version'])
                state['versions'][asset['current_version']]['status'] = 'history'
            version.update(frozen, status='current', committed_at=now_iso())
            asset.update(current_version=identity['version_id'], candidate_version=None)
        else:
            version.update(status='failed', error=error)
        state['revision'] += 1; m._write(parent, state)


def reconcile_candidates(parent, jobs, snapshot=None):
    """Fence a replacement before it can enter the legacy scheduler."""
    state = m.read(parent)
    entries = ((file, read_json(file)) for file in jobs.glob('*.json')) if snapshot is None else snapshot
    for file, job in entries:
        child_id = job.get('batch_id'); event = state['outbox'].get(child_id)
        if not event or event['kind'] != 'candidate': continue
        if job.get('status') == 'queued' and job.get('phase') == 'render' and job.get('attempt'):
            # Keep the original durable wait until it is due; replacing the
            # container earlier would create a fetch job without that deadline.
            if job.get('not_before', 0) > time.time(): continue
            with m.locked(parent):
                latest = m._read(parent)
                version = latest['versions'][event['version_id']]
                asset = latest['assets'][event['asset_id']]
                if (asset['candidate_version'] == event['version_id'] and version['child_id'] == child_id
                        and version['status'] in {'processing', 'queued'} and not version.get('upload_job_id')):
                    new_id = uuid.uuid4().hex
                    replacement = {**copy.deepcopy(event), 'child_id': new_id, 'status': 'pending',
                                   'job_id': uuid.uuid5(uuid.NAMESPACE_URL, new_id + ':fetch').hex}
                    latest['outbox'][new_id] = replacement
                    latest['outbox'][child_id]['status'] = 'superseded'
                    version.update(child_id=new_id, status='queued')
                    version.pop('execution', None)
                    latest['revision'] += 1; m._write(parent, latest)
            job.update(status='interrupted', finished=time.time(), error='由独立候选尝试恢复')
            m.durable_write(file, job)
        elif (job.get('status') in {'failed', 'interrupted'}
              or (job.get('phase') == 'media-upload' and job.get('status') == 'queued' and job.get('attempt'))
              or (job.get('phase') in {'render', 'media-upload'} and job.get('status') == 'completed')):
            # A paused finalizer may have successful child evidence without a
            # parent commit. Never rerun an upload process over the same mutable
            # files automatically: explicit confirmation verifies the pair and
            # allocates a fresh upload job while preserving the previous current.
            with m.locked(parent):
                latest = m._read(parent); version = latest['versions'][event['version_id']]
                if (version['child_id'] == child_id and version['status'] in {'processing', 'queued'}
                        and (not version.get('upload_job_id') or job['id'] == version['upload_job_id'])):
                    version.update(status='failed', error='执行进程退出；原成片已保留，可重新确认制作')
                    latest['revision'] += 1; m._write(parent, latest)
            if job.get('phase') == 'media-upload' and job.get('status') == 'queued':
                job.update(status='interrupted', finished=time.time(), error='媒体补传中断；请重新确认补传')
                m.durable_write(file, job)


def parent_batch_id(batch_id):
    from . import workflow as w
    identity = m.internal(w.BATCHES / batch_id)
    return identity['parent_batch_id'] if identity else batch_id


def recover_verification(parent, event):
    """Only an explicit CAS-approved verification may run after unknown intent."""
    from . import workflow as w
    latest = m.read(parent)['outbox'].get(event['job_id'])
    if not latest or latest['status'] == 'superseded': return
    target = w.ROOT / '.web-state/jobs' / (event['job_id'] + '.json')
    if target.exists(): return
    # Hash large media before taking the parent lock. queue_verify_delivery
    # rechecks the cheap file identity against this evidence under the lock.
    evidence = w.approval_evidence(event['child_id'], event['asset_id'], event['sha256'])
    # Serialize local publication/job handoffs with explicit retry approval, so
    # a recovery snapshot cannot reset the publication after being superseded.
    with m.locked(parent):
        return _recover_verification(parent, event, evidence)


def _recover_verification(parent, event, evidence):
    from . import workflow as w
    latest = m.read(parent)['outbox'].get(event['job_id'])
    if not latest or latest['status'] == 'superseded': return
    child = w.BATCHES / event['child_id']; rid = event['asset_id']
    file = child / 'publications.json'; entry = read_json(file)[rid]
    if w.verification_binding(entry) != event['binding']: raise ValueError('交付核实身份变化')
    if entry.get('status') == 'fbbm_ready': return
    target = w.ROOT / '.web-state/jobs' / (event['job_id'] + '.json')
    if target.exists(): return
    w.approval_evidence(child.name, rid, event['sha256'], evidence)
    if (entry.get('verification_queue') or {}).get('job_id') != event['job_id']:
        w.queue_verify_delivery(child.name, rid, event['sha256'], _job_id=event['job_id'], _evidence=evidence)
    entry = read_json(file)[rid]
    if entry.get('verification_only') != event['binding']: raise ValueError('交付核实绑定变化')
    from .accounts import owner
    job = {'id': event['job_id'], 'batch_id': child.name, 'project': read_json(child / 'confirmed.json')['drama_id'],
           'owner': owner(parent), 'actor': owner(parent), 'action': 'verify-delivery', 'request_id': rid,
           'phase': 'poll', 'lane': 'poll', 'created': time.time(), 'status': 'queued', 'instance': instance_marker(),
           'verification_only': event['binding'],
           'command': [sys.executable, '-u', '-m', 'exactcut.workflow', 'poll', child.name, rid, instance_marker()]}
    m.durable_write(target, job)


def local_pair_valid(child, rid):
    from . import workflow as w
    try:
        _, request = w.request_for(child, rid)
        record = w.result_records(child).get(rid, {}); meta = record.get('preview') or {}
        master = child / 'production/videos' / request.output_filename
        preview = child / 'production/previews' / request.output_filename
        return (record.get('status') in w.SAFE and meta.get('master_sha256') == record.get('output_sha256')
                and not master.is_symlink() and not preview.is_symlink()
                and file_sha256(master) == record.get('output_sha256') and file_sha256(preview) == meta.get('sha256'))
    except (OSError, ValueError, KeyError): return False


def recover_media_upload(parent, event):
    from . import workflow as w
    state = m.read(parent); version = state['versions'][event['version_id']]
    if version.get('upload_job_id') != event['job_id'] or version['status'] not in {'queued', 'processing'}: return
    child = w.BATCHES / event['child_id']
    job = _job(parent, {**event, 'kind': 'delivery'}, child)
    job.update(action='media-upload', phase='media-upload', lane='upload',
               command=[sys.executable, '-u', '-m', 'exactcut.workflow', 'media-upload', child.name, event['asset_id'], instance_marker()])
    target = w.ROOT / '.web-state/jobs' / (job['id'] + '.json')
    if not target.exists(): m.durable_write(target, job)


def retry_media_upload(batch_id, rid):
    from . import workflow as w, preview_oss, result_store
    child = w.folder(batch_id)
    if not local_pair_valid(child, rid): raise ValueError('候选本地文件变化，请重新制作')
    _, request = w.request_for(child, rid)
    record = w.result_records(child)[rid]
    preview_oss.upload_generated_preview(child / 'production', request, record)
    preview_oss.upload_generated_original(child / 'production', request, record)
    result_store.put(child / 'production', rid, record)
    return 0


def cancel_invalid_children(parent, snapshot=None):
    """Best-effort resource release follows the authoritative invalidation."""
    import signal
    from . import batch_control as control, workflow as w
    state = m.read(parent)
    jobs_by_child = None
    for event in state['outbox'].values():
        if event['kind'] != 'candidate': continue
        asset = state['assets'][event['asset_id']]; version = state['versions'][event['version_id']]
        invalid = (state['deleted_at'] or asset['deleted_at'] or version['status'] == 'superseded'
                   or version['child_id'] != event['child_id'])
        if not invalid: continue
        child = parent.parent / event['child_id']
        if (child / 'draft.json').exists():
            if jobs_by_child is None:
                jobs_by_child = {}
                entries = ((file, read_json(file)) for file in (w.ROOT / '.web-state/jobs').glob('*.json')) if snapshot is None else snapshot
                for file, job in entries:
                    jobs_by_child.setdefault(job.get('batch_id'), []).append(file)
            control.signal_children(child, signal.SIGKILL)
            cancel_queued_candidate(parent, event, child, job_files=jobs_by_child.get(child.name, []))


def cancel_queued_candidate(parent, event, child, *, job_files=None):
    """Retire only unlaunched/dead stages of an authoritatively invalid child."""
    from . import workflow as w, batch_control as control, storage
    if m.internal(child) != lineage(parent, event): return
    jobs = w.ROOT / '.web-state/jobs'
    job_files = list(jobs.glob('*.json')) if job_files is None else job_files
    message = '候选已取消或被替代；原成片已保留'
    cancelled = []
    for file in job_files:
        job = read_json(file)
        retired = job.get('status') == 'interrupted' and job.get('error') == message
        if (job.get('batch_id') != child.name or job.get('phase') not in {'fetch', 'render', 'media-upload'}
                or (job.get('status') not in {'queued', 'waiting_resource'} and not retired)): continue
        if retired:
            runtime = read_json(child / 'runtime.json')
            if runtime.get('status') not in w.ACTIVE and not runtime.get('executor_running'): continue
        # Resource waits and recovered launch windows may have had a process.
        # Unknown ownership or an undiscovered live token must fail closed.
        if not control.belongs(job): continue
        token = job.get('process_token')
        if token and (not control.valid_token(token) or control.PROCESSES.find(token)): continue
        if job.get('pid') and (not token or control.child_state(job) not in {None, 'Z'}): continue
        with m.locked(parent):
            state = m._read(parent)
            asset = state['assets'][event['asset_id']]; version = state['versions'][event['version_id']]
            invalid = (state['deleted_at'] or asset['deleted_at'] or version['status'] == 'superseded'
                       or version['child_id'] != child.name)
            if not invalid or read_json(file) != job: continue
            if not retired:
                job.update(status='interrupted', finished=time.time(), error=message)
                m.durable_write(file, job)
        storage.release(job['id'], jobs)
        cancelled.append(job)
    if not cancelled: return
    # No legacy runtime/result lock is acquired while holding the parent lock.
    # Running stages are still harvested by the existing scheduler exit path.
    if any(job.get('batch_id') == child.name and job.get('status') == 'running'
           for job in (read_json(file) for file in job_files)): return
    for job in cancelled:
        rid = job.get('request_id')
        if rid:
            record = w.result_records(child).get(rid, {})
            fields = {} if record.get('status') in w.SAFE else {'status': 'failed', 'error': message}
            w.end_execution(child, rid, **fields)
    w.end_execution(child, status='interrupted', message=message)
