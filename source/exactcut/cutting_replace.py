"""Confirm a cut as the current master. Original bytes and delivery stay intact."""
import copy
import shutil
import threading
import uuid
from pathlib import Path

from . import cutting, cutting_access, material_versions as m, result_store, preview_oss
from .preview import make_preview
from .util import file_sha256, read_json, write_json_atomic, now_iso


def prepare(batch, session, job, *, source=None):
    """Create a complete immutable pair before changing any current pointer."""
    from . import storage
    source = Path(source) if source is not None else cutting.session_path(batch.name, session['id']) / (job['id'] + '.mp4')
    storage.require_space(batch, max(256 * 1024**2, source.stat().st_size * 3))
    directory = batch / 'production/edits' / session['request_id'] / job['id']
    master = directory / 'master' / session['source_filename']
    preview = directory / 'preview' / session['source_filename']
    manifest = directory / 'pair.json'
    if manifest.exists():
        pair = read_json(manifest)
        if file_sha256(master) != job['output_sha256'] or file_sha256(preview) != pair['preview']['sha256']:
            raise cutting.CutError('编辑后视频校验失败，原成片保留')
    else:
        master.parent.mkdir(parents=True, exist_ok=True)
        if file_sha256(source) != job['output_sha256']:
            raise cutting.CutError('剪切结果已变化，请重新生成')
        temporary = master.with_suffix('.partial.mp4')
        shutil.copyfile(source, temporary)
        if file_sha256(temporary) != job['output_sha256']:
            raise cutting.CutError('剪切结果校验失败')
        temporary.replace(master)
        pair = {'preview': make_preview(master, preview, job['output_sha256'])}
        write_json_atomic(manifest, pair)
    pair = copy.deepcopy(pair)
    if preview_oss.enabled():
        bucket = preview_oss._connect()
        pair['original_oss'] = preview_oss._ensure_uploaded(master, batch.name, session['request_id'], bucket=bucket)
        pair['preview']['oss'] = preview_oss._ensure_uploaded(preview, batch.name, session['request_id'], bucket=bucket)
    from .runtime import environment
    if environment() == 'production' and not preview_oss.enabled():
        raise cutting.CutError('项目 OSS 未启用，不能替换正式成片')
    return master, preview, pair


def replacement_record(previous, session, job, master, preview, pair):
    record = copy.deepcopy(previous)
    for key in ('preview', 'original_oss', 'original_oss_error', 'preview_error', 'preview_oss_error', 'review', 'qc', 'unit_checks', 'hook_emphasis'):
        record.pop(key, None)
    duration = round(job['expected_frames'] * 1000 * session['metadata']['fps_den'] / session['metadata']['fps_num'])
    record.update(status='qc_passed', output=str(master), output_sha256=job['output_sha256'],
                  duration_ms=duration, planned_duration_ms=duration, preview=pair['preview'],
                  edited_media={'master': str(master), 'preview': str(preview), 'session_id': session['id'],
                                'job_id': job['id'], 'source_sha256': session['source_sha256'], 'replaced_at': now_iso()},
                  qc={'allowed': True, 'expected_duration_ms': duration, 'actual_duration_ms': duration,
                      'errors': [], 'method': job.get('validation_method', 'frame-cut-validated')})
    if pair.get('original_oss'): record['original_oss'] = pair['original_oss']
    return record


def current_is_job(batch, session, job):
    from . import workflow as w
    _, record = w.preview_file(batch.name, session['request_id'])
    edit = record.get('edited_media') or {}
    return record.get('output_sha256') == job['output_sha256'] and edit.get('session_id') == session['id'] and edit.get('job_id') == job['id']


def update_job(batch_id, sid, jid, result):
    path = cutting.session_path(batch_id, sid)
    with cutting.lock_file(path / 'state.lock', blocking=True):
        state = cutting.read_session(batch_id, sid)
        job = cutting.get_job(state, jid)
        job['replacement'] = result
        write_json_atomic(path / 'session.json', state)
        return copy.deepcopy(job)


def start(batch_id, sid, jid):
    from . import workflow as w
    batch = w.folder(batch_id)
    session = cutting.get_session(batch_id, sid)
    job = cutting.get_job(session, jid)
    if job['status'] not in {'ready', 'saved'} or job.get('mosaics') or job.get('discarded'):
        raise cutting.CutError('请先生成删除片段后的预览')
    if current_is_job(batch, session, job):
        return update_job(batch_id, sid, jid, {'status': 'completed', 'storage': 'oss' if preview_oss.enabled() else 'local'})
    guard = cutting_access.gate(batch, session['request_id'])
    guard.__enter__()
    lease = None
    try:
        cutting_access.require_editable(batch, session['request_id'], source_sha=session['source_sha256'])
        if cutting.get_job(cutting.read_session(batch_id, sid), jid).get('discarded'):
            raise cutting.CutError('本次剪切已放弃，请重新生成', 409)
        lease = cutting.lock_file(cutting.lock_root() / (batch_id + '.lock'), shared=True)
        result = update_job(batch_id, sid, jid, {'status': 'running', 'started_at': now_iso()})
        threading.Thread(target=run, args=(batch, session, job, guard, lease), daemon=True).start()
        return result
    except Exception:
        try:
            if lease: lease.close()
        finally:
            guard.__exit__(None, None, None)
        raise


def run(batch, session, job, guard, lease):
    from . import workflow as w
    try:
        master, preview, pair = prepare(batch, session, job)
        commit(batch, session, job, master, preview, pair)
        result = {'status': 'completed', 'completed_at': now_iso(), 'storage': 'oss' if preview_oss.enabled() else 'local'}
    except Exception:
        # Reconcile a lost acknowledgement after the atomic current commit.
        try:
            committed = current_is_job(batch, session, job)
        except (ValueError, OSError, KeyError):
            committed = False
        if committed:
            result = {'status': 'completed', 'storage': 'oss' if preview_oss.enabled() else 'local'}
        else:
            result = {'status': 'failed', 'error': '替换失败，当前成片未改变。请稍后重试。'}
    try:
        update_job(batch.name, session['id'], job['id'], result)
    finally:
        try:
            lease.close()
        finally:
            guard.__exit__(None, None, None)


def recover(batch_id, sid):
    """An interrupted attempt never starts external work implicitly."""
    from . import workflow as w
    batch = w.folder(batch_id)
    state = cutting.read_session(batch_id, sid)
    for job in state['jobs']:
        if job.get('replacement', {}).get('status') != 'running': continue
        try:
            with cutting_access.gate(batch, state['request_id']):
                result = {'status': 'completed', 'storage': 'oss' if preview_oss.enabled() else 'local'} if current_is_job(batch, state, job) else {'status': 'failed', 'error': '替换处理已中断，原成片保留，可重试。'}
                update_job(batch_id, sid, job['id'], result)
        except cutting.CutError:
            pass


def commit(batch, session, job, master, preview, pair):
    from . import workflow as w
    rid = session['request_id']
    # Guard excludes delivery admission and legacy execution. Managed CAS also
    # fences edits/archival/current promotion without holding a lock on I/O.
    if m.managed(batch):
        with m.locked(batch):
            cutting_access.require_editable(batch, rid, source_sha=session['source_sha256'],allow_job=job['id'] if job.get('queued_task') else None)
            state = m._read(batch); asset = state['assets'][rid]
            old_id = asset['current_version']; old = state['versions'][old_id]
            vid = uuid.uuid4().hex
            version = copy.deepcopy(old)
            for key in ('child_id', 'execution', 'upload_job_id', 'legacy'): version.pop(key, None)
            version.update(version_id=vid, status='current', master_path=str(master), preview_path=str(preview),
                           record=replacement_record(old['record'], session, job, master, preview, pair),
                           source_version_id=old_id, committed_at=now_iso())
            old['status'] = 'history'; asset['history'].append(old_id)
            state['versions'][vid] = version; asset['current_version'] = vid
            state['revision'] += 1; m._write(batch, state)
    else:
        cutting_access.require_editable(batch, rid, source_sha=session['source_sha256'],allow_job=job['id'] if job.get('queued_task') else None)
        with result_store.locked(batch / 'production', rid) as file:
            previous = read_json(file) if file.exists() else result_store.read(batch / 'production')['requests'][rid]
            if previous.get('output_sha256') != session['source_sha256']:
                raise cutting.CutError('当前成片已变化，未替换', 409)
            # A private snapshot preserves the old record/preview references.
            backup = master.parent.parent / 'previous.json'
            if not backup.exists(): write_json_atomic(backup, previous)
            write_json_atomic(file, replacement_record(previous, session, job, master, preview, pair))
