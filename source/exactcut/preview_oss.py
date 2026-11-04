"""Private preview copies. Never delete objects or modify bucket-wide settings."""
from pathlib import Path
import shutil
import tempfile
import re
from urllib.parse import quote

from .runtime import data_root, environment
from .util import file_sha256, json_sha256, load_config, env, now_iso, read_json
from . import diagnostics as diag, resource_locks

# Restricted namespaces: configuration cannot redirect writes into delivery keys.
PREFIX = 'exactcut-previews/production'


class PreviewBusy(RuntimeError):
    """Playback of an existing local result must not prepare or upload an OSS copy."""


def _prefix(batch_id, request_id):
    if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
        raise ValueError('无效项目编号')
    request_key = quote(request_id, safe='')
    if not request_key or request_key in {'.', '..'}:
        raise ValueError('无效素材编号')
    return f'{object_prefix()}/{batch_id}/{request_key}/'


def _valid_receipt(video, receipt, prefix, *, bucket_name, endpoint):
    if not isinstance(receipt, dict):
        return False
    identity = _identity(video)
    return (re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('sha256', ''))) and
            receipt.get('key') == prefix + receipt['sha256'] + '.mp4' and
            receipt.get('identity') == identity and
            receipt.get('bytes') == identity[2] and
            receipt.get('bucket') == bucket_name and
            receipt.get('endpoint') == endpoint)


def object_prefix():
    namespace = load_config().get("preview", {}).get("namespace", "production")
    if namespace not in ("production", "test"):
        raise ValueError("preview.namespace must be production or test")
    return "exactcut-previews/" + namespace


def enabled():
    return environment() == 'production' and load_config().get('preview', {}).get('oss_enabled', True)


def _preparation_id(batch_id, request_id, video, record, original):
    return json_sha256({'batch_id': batch_id, 'request_id': request_id, 'original': bool(original),
                        'identity': _identity(video), 'output_sha256': record.get('output_sha256'),
                        'preview_sha256': (record.get('preview') or {}).get('sha256')})[:32]


def _preparation_path(jid):
    return data_root() / '.web-state/preview-oss' / (jid + '.json')


def preparation_status(batch_id, request_id, video, record, *, original):
    """Read-only readiness for an existing OSS copy; no copy, upload or wait."""
    receipt = record.get('original_oss') if original else (record.get('preview') or {}).get('oss')
    try:
        bucket = _connect()
        prefix = _prefix(batch_id, request_id)
        if _valid_receipt(video, receipt, prefix, bucket_name=bucket.bucket_name, endpoint=bucket.endpoint):
            return 'ready'
    except Exception:
        pass
    path = _preparation_path(_preparation_id(batch_id, request_id, video, record, original))
    job = read_json(path) if path.is_file() else None
    if job and job.get('status') in {'failed', 'cancelled'}:
        return 'failed'
    return 'preparing'


def prepare_async(batch_id, request_id, video, record, *, original, actor):
    """Queue one bounded, persistent OSS copy; never copy/upload on the request."""
    from . import cutting, interactive_queue
    identity = _identity(video)
    preview_sha = (record.get('preview') or {}).get('sha256')
    jid = _preparation_id(batch_id, request_id, video, record, original)
    path = _preparation_path(jid)
    path.parent.mkdir(parents=True, exist_ok=True)
    session = {'batch_id': batch_id, 'id': request_id, 'request_id': request_id,
               'source_sha256': record.get('output_sha256', '')}
    job = {'schema_version': 1, 'id': jid, 'status': 'queued', 'actor': actor,
           'batch_id': batch_id, 'request_id': request_id, 'original': bool(original),
           'identity': identity, 'output_sha256': record.get('output_sha256'),
           'preview_sha256': preview_sha, 'created_at': now_iso(), 'progress': '正在准备 OSS 预览'}
    with cutting.lock_file(path.parent / 'state.lock', blocking=True):
        interactive_queue.enqueue('preview_oss', session, path, job, {}, state_lock=path.parent / 'state.lock')
        return read_json(path)


def run_queued(entry, leases):
    from . import interactive_queue, workflow
    job = read_json(interactive_queue.report(entry))
    batch_id, request_id = job['batch_id'], job['request_id']; original = bool(job['original'])
    output, record = workflow.preview_file(batch_id, request_id, lightweight=not original)
    preview_sha = (record.get('preview') or {}).get('sha256')
    if (_identity(output) != job['identity'] or record.get('output_sha256') != job['output_sha256']
            or (not original and preview_sha != job['preview_sha256'])):
        interactive_queue._update(entry, status='superseded', completed_at=now_iso(),
                                  progress='成片已变化，将为新成片重新准备')
        return
    receipt = record.get('original_oss') if original else (record.get('preview') or {}).get('oss')
    verified = _ensure_uploaded(output, batch_id, request_id, bucket=_connect(), receipt=receipt)
    save_media_receipt(workflow.folder(batch_id) / 'production', request_id, record, output, verified, original=original)
    interactive_queue._update(entry, status='completed', completed_at=now_iso(), progress='OSS 预览已就绪')


def _connect():
    if not enabled():
        raise RuntimeError('OSS 预览仅在生产环境显式启用')
    import oss2
    token = env('ALIYUN_SECURITY_TOKEN', required=False)
    access_id, secret = env('ALIYUN_OSS_ACCESS_KEY_ID'), env('ALIYUN_OSS_ACCESS_KEY_SECRET')
    auth = oss2.StsAuth(access_id, secret, token) if token else oss2.Auth(access_id, secret)
    bucket = oss2.Bucket(auth, env('ALIYUN_OSS_ENDPOINT'), env('ALIYUN_OSS_BUCKET'), connect_timeout=30)
    bucket.session.session.trust_env = False
    return bucket


def signed_preview(video: Path, batch_id: str, request_id: str, *, bucket=None, receipt=None, download_name=None, save_receipt=None, wait=True):
    prefix = _prefix(batch_id, request_id)
    if wait:
        bucket = bucket if bucket is not None else _connect()
        verified = _ensure_uploaded(video, batch_id, request_id, bucket=bucket, receipt=receipt)
    else:
        # Playback only validates an existing receipt; it never copies, hashes,
        # uploads or waits for an object lock in the request.
        if not isinstance(receipt, dict):
            raise PreviewBusy('OSS 预览尚未准备')
        try:
            bucket = bucket if bucket is not None else _connect()
        except Exception:
            raise PreviewBusy('OSS 预览暂不可用') from None
        if not _valid_receipt(video, receipt, prefix, bucket_name=bucket.bucket_name, endpoint=bucket.endpoint):
            raise PreviewBusy('OSS 预览尚未准备')
        verified = receipt
    if save_receipt:
        save_receipt(verified)
    options = {}
    if download_name:
        options['params'] = {'response-content-disposition': "attachment; filename=download.mp4; filename*=UTF-8''" + quote(download_name, safe='')}
    return {'url': bucket.sign_url('GET', verified['key'], 900, **options), 'expires_in': 900}


def upload_generated_preview(batch_dir, request, record):
    # CLI outputs outside the web batch namespace must not enter production preview storage.
    if not enabled() or batch_dir.name != 'production':
        return
    from .preview import available_preview
    video = available_preview(batch_dir.parent, request, record)
    if video is None:
        return
    try:
        bucket = _connect()
        from .material_workers import parent_batch_id
        record['preview']['oss'] = _ensure_uploaded(video, parent_batch_id(batch_dir.parent.name), request.request_id, bucket=bucket,
                                                   receipt=record['preview'].get('oss'))
        record.pop('preview_oss_error', None)
        diag.event('preview.oss_ready', request_id=request.request_id)
    except Exception as exc:
        record['preview_oss_error'] = '预览上传暂时失败，进入成片预览可重试；本地成片已保留'
        diag.event('preview.oss_failed', level='warning', request_id=request.request_id, error_type=type(exc).__name__)


def upload_generated_original(batch_dir, request, record):
    if not enabled() or batch_dir.name != 'production':
        return
    from .preview import media_path
    video = media_path(batch_dir.parent, request, record)
    if not video.is_file() or not record.get('output_sha256'):
        return
    try:
        from .material_workers import parent_batch_id
        record['original_oss'] = _ensure_uploaded(video, parent_batch_id(batch_dir.parent.name), request.request_id,
                                                  bucket=_connect(), receipt=record.get('original_oss'))
        record.pop('original_oss_error', None)
    except Exception as exc:
        record['original_oss_error'] = '正式成片上传暂时失败，下载时可重试；本地成片已保留'
        diag.event('original.oss_failed', level='warning', request_id=request.request_id, error_type=type(exc).__name__)


def save_media_receipt(batch_dir, request_id, record, video, receipt, *, original=False):
    from . import result_store, material_versions
    if material_versions.managed(batch_dir.parent):
        return material_versions.save_receipt(batch_dir.parent, request_id, record, video, receipt, original=original)
    from .util import read_json, write_json_atomic
    with result_store.locked(batch_dir, request_id) as file:
        latest = read_json(file) if file.exists() else result_store.read(batch_dir)['requests'].get(request_id, {})
        if (latest.get('output_sha256') != record.get('output_sha256') or _identity(video) != receipt['identity']):
            raise ValueError('成片正在更新，请稍后重试')
        if original:
            latest['original_oss'] = receipt
            latest.pop('original_oss_error', None)
        else:
            if (latest.get('preview') or {}).get('sha256') != (record.get('preview') or {}).get('sha256'):
                raise ValueError('预览正在更新，请稍后重试')
            latest.setdefault('preview', {})['oss'] = receipt
            latest.pop('preview_oss_error', None)
        write_json_atomic(file, latest)


def _identity(video):
    stat = video.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _ensure_uploaded(video: Path, batch_id: str, request_id: str, *, bucket, receipt=None):
    prefix = _prefix(batch_id, request_id)
    if _valid_receipt(video, receipt, prefix, bucket_name=bucket.bucket_name, endpoint=bucket.endpoint):
        return receipt
    before = _identity(video)
    with tempfile.TemporaryDirectory(prefix='.oss-preview-', dir=video.parent) as directory:
        snapshot = Path(directory) / 'preview.mp4'
        shutil.copyfile(video, snapshot)
        if _identity(video) != before:
            raise ValueError('成片正在更新，请稍后重试')
        digest = file_sha256(snapshot)
        key = prefix + digest + '.mp4'
        # The complete remote identity avoids fixed shards and same-prefix
        # collisions. A second process rechecks existence after the first put.
        with resource_locks.hold('oss-object',[bucket.endpoint,bucket.bucket_name,key]):
            if not bucket.object_exists(key):
                try:
                    result = bucket.put_object_from_file(key, str(snapshot), headers={
                        'Content-Type': 'video/mp4', 'Content-Disposition': 'inline',
                        'Cache-Control': 'private, max-age=900',
                        'x-oss-object-acl': 'private', 'x-oss-meta-sha256': digest,
                        'x-oss-forbid-overwrite': 'true',
                    })
                    if result.status not in (200, 201):
                        raise ValueError('预览上传失败')
                except Exception as exc:
                    if getattr(exc, 'code', None) != 'FileAlreadyExists':
                        raise
            # Stat identity and content are both checked before publishing a receipt.
            if _identity(video) != before or file_sha256(video) != digest:
                raise ValueError('成片正在更新，请稍后重试')
            meta = bucket.head_object(key)
            remote_hash = meta.headers.get('x-oss-meta-sha256') or meta.headers.get('X-Oss-Meta-Sha256')
            if (meta.content_length != before[2] or remote_hash != digest or
                    bucket.get_object_acl(key).acl != 'private'):
                raise ValueError('预览对象校验失败，未修改已有文件')
            return {'key': key, 'sha256': digest, 'bytes': before[2], 'identity': before,
                    'bucket': bucket.bucket_name, 'endpoint': bucket.endpoint}
