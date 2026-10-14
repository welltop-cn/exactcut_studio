"""Private preview copies. Never delete objects or modify bucket-wide settings."""
from pathlib import Path
import hashlib
import threading
import shutil
import tempfile
import re
from urllib.parse import quote

from .runtime import environment
from .util import file_sha256, load_config, env
from . import diagnostics as diag

# Restricted namespaces: configuration cannot redirect writes into delivery keys.
PREFIX = 'exactcut-previews/production'
_LOCKS = [threading.Lock() for _ in range(64)]
_UPLOADS = threading.BoundedSemaphore(2)


def object_prefix():
    namespace = load_config().get("preview", {}).get("namespace", "production")
    if namespace not in ("production", "test"):
        raise ValueError("preview.namespace must be production or test")
    return "exactcut-previews/" + namespace


def enabled():
    return environment() == 'production' and load_config().get('preview', {}).get('oss_enabled', True)


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


def signed_preview(video: Path, batch_id: str, request_id: str, *, bucket=None, receipt=None, download_name=None, save_receipt=None):
    bucket = bucket if bucket is not None else _connect()
    verified = _ensure_uploaded(video, batch_id, request_id, bucket=bucket, receipt=receipt)
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
    if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
        raise ValueError('无效项目编号')
    request_key = quote(request_id, safe='')
    if not request_key or request_key in {'.', '..'}:
        raise ValueError('无效素材编号')
    prefix = f'{object_prefix()}/{batch_id}/{request_key}/'
    identity = _identity(video)
    if (isinstance(receipt, dict) and re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('sha256', ''))) and
            receipt.get('key') == prefix + receipt['sha256'] + '.mp4' and
            receipt.get('identity') == identity and receipt.get('bytes') == identity[2] and
            receipt.get('bucket') == bucket.bucket_name and receipt.get('endpoint') == bucket.endpoint):
        return receipt
    lock = _LOCKS[int(hashlib.sha256(prefix.encode()).hexdigest(), 16) % len(_LOCKS)]
    # Duplicate waiters never hold an upload slot needed by another project.
    if not lock.acquire(timeout=10):
        raise ValueError('该预览正在准备，请稍后重试')
    try:
        if not _UPLOADS.acquire(timeout=10):
            raise ValueError('预览准备繁忙，请稍后重试')
        try:
            before = _identity(video)
            with tempfile.TemporaryDirectory(prefix='.oss-preview-', dir=video.parent) as directory:
                snapshot = Path(directory) / 'preview.mp4'
                shutil.copyfile(video, snapshot)
                if _identity(video) != before:
                    raise ValueError('成片正在更新，请稍后重试')
                digest = file_sha256(snapshot)
                key = prefix + digest + '.mp4'
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
                # Some Linux filesystems coalesce timestamps for rapid writes.
                # A same-size in-place change may therefore retain stat identity.
                # Cold uploads verify bytes too; immutable published files retain
                # the receipt-only hot path.
                if _identity(video) != before or file_sha256(video) != digest:
                    raise ValueError('成片正在更新，请稍后重试')
                meta = bucket.head_object(key)
                remote_hash = meta.headers.get('x-oss-meta-sha256') or meta.headers.get('X-Oss-Meta-Sha256')
                if (meta.content_length != before[2] or remote_hash != digest or
                        bucket.get_object_acl(key).acl != 'private'):
                    raise ValueError('预览对象校验失败，未修改已有文件')
                return {'key': key, 'sha256': digest, 'bytes': before[2], 'identity': before,
                        'bucket': bucket.bucket_name, 'endpoint': bucket.endpoint}
        finally:
            _UPLOADS.release()
    finally:
        lock.release()
