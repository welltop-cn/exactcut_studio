"""Exercise the complete signing/receipt callback through authenticated HTTP."""
import hashlib
import json

import pytest

from exactcut import preview_oss, result_store, workflow
from test_preview_oss import Bucket
from test_web_base_path import app, login, request


class SigningBucket(Bucket):
    def sign_url(self, method, key, expires, params=None):
        self.signs.append((method, key, expires, params))
        return 'https://fixture.invalid/' + key + '?signature=fixture'


@pytest.mark.parametrize('original', [False, True])
def test_generation_upload_persists_real_oss_receipt_before_signing(app, tmp_path, monkeypatch, original):
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    directory = workflow.folder(batch_id) / 'production'
    video = tmp_path / 'media.mp4'
    video.write_bytes(b'private media fixture')
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    record = {'status': 'qc_passed', 'output_sha256': digest,
              'preview': {'sha256': digest, 'master_sha256': digest},
              'preserve_me': 'existing metadata'}
    result_store.put(directory, 'R1', record)
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    bucket = SigningBucket()
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    receipt = preview_oss._ensure_uploaded(video, batch_id, 'R1', bucket=bucket)
    preview_oss.save_media_receipt(directory, 'R1', record, video, receipt, original=original)
    updated = result_store.read(directory)['requests']['R1']
    saved = updated['original_oss'] if original else updated['preview']['oss']
    assert saved['sha256'] == digest
    assert saved['identity'] == preview_oss._identity(video)
    assert updated['preserve_me'] == 'existing metadata'
    assert len(bucket.puts) == 1
    assert bucket.objects[receipt['key']][1]['x-oss-object-acl'] == 'private'
    if original:
        result = preview_oss.signed_preview(video, batch_id, 'R1', bucket=bucket, receipt=saved,
                                            download_name='download.mp4')
        assert result['expires_in'] == 900
        assert bucket.signs[0][3]['response-content-disposition'].startswith('attachment;')
    else:
        assert preview_oss.signed_preview(video, batch_id, 'R1', bucket=bucket, receipt=saved)['expires_in'] == 900


def test_changed_result_during_generation_upload_refuses_url_and_preserves_new_result(app, tmp_path, monkeypatch):
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    directory = workflow.folder(batch_id) / 'production'
    video = tmp_path / 'old-preview.mp4'
    video.write_bytes(b'old preview')
    old = {'status': 'qc_passed', 'output_sha256': 'old-master',
           'preview': {'sha256': hashlib.sha256(video.read_bytes()).hexdigest()}}
    result_store.put(directory, 'R1', old)
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    bucket = SigningBucket()
    put = bucket.put_object_from_file
    new = {'status': 'qc_passed', 'output_sha256': 'new-master', 'preserve_me': 'new state'}
    def upload_and_advance(key, filename, headers):
        result = put(key, filename, headers)
        result_store.put(directory, 'R1', new)
        return result
    bucket.put_object_from_file = upload_and_advance
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    import pytest
    with pytest.raises(ValueError):
        preview_oss.signed_preview(video, batch_id, 'R1', bucket=bucket,
                                   save_receipt=lambda receipt: preview_oss.save_media_receipt(
                                       directory, 'R1', old, video, receipt))
    assert result_store.read(directory)['requests']['R1'] == new
    assert len(bucket.objects) == 1  # The uploaded immutable object is retained.
    assert bucket.signs == []


def _seed_receipt(batch_id, directory, video, bucket, *, original=False):
    receipt = preview_oss._ensure_uploaded(video, batch_id, 'R1', bucket=bucket)
    record = result_store.read(directory)['requests']['R1']
    if original:
        record['original_oss'] = receipt
    else:
        record['preview']['oss'] = receipt
    result_store.put(directory, 'R1', record)
    return receipt


def _preview_fixture(app, tmp_path, monkeypatch, *, receipt=None):
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    directory = workflow.folder(batch_id) / 'production'
    video = tmp_path / 'media.mp4'
    video.write_bytes(b'private media fixture for fallback')
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    record = {'status': 'qc_passed', 'output_sha256': digest,
              'preview': {'sha256': digest, 'master_sha256': digest}}
    if receipt is not None:
        record['preview']['oss'] = receipt
    result_store.put(directory, 'R1', record)
    monkeypatch.setattr(workflow, 'preview_file', lambda *a, **kw: (video, result_store.read(directory)['requests']['R1']))
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    bucket = SigningBucket()
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    return headers, batch_id, directory, video, digest, bucket


def _held_transfers():
    from contextlib import ExitStack
    from exactcut import editor_resources
    stack = ExitStack()
    for _ in range(editor_resources.settings()['transfer_tasks']):
        stack.enter_context(editor_resources.acquire('transfer'))
    return stack


def test_busy_transfer_falls_back_to_authenticated_local_playback(app, tmp_path, monkeypatch):
    import time
    headers, batch_id, directory, video, digest, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    with _held_transfers():
        started = time.monotonic()
        status, response_headers, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
        elapsed = time.monotonic() - started
        assert status == 200 and elapsed < 3, (status, elapsed, body)
        result = json.loads(body)
        assert result == {'url': f'/api/batches/{batch_id}/preview/R1', 'expires_in': 0}
        assert bucket.puts == [] and bucket.signs == []
        assert 'fixture.invalid' not in body.decode()
    status, _, data = request(app, 'GET', result['url'], headers=headers)
    assert status == 200 and data == video.read_bytes()
    status, response_headers, data = request(app, 'GET', result['url'], headers={**headers, 'Range': 'bytes=0-9'})
    assert status == 206 and data == video.read_bytes()[:10]
    assert response_headers['Content-Range'].startswith('bytes 0-9/')
    assert result_store.read(directory)['requests']['R1'] == {'status': 'qc_passed', 'output_sha256': digest,
                                                              'preview': {'sha256': digest, 'master_sha256': digest}}


def test_busy_transfer_download_falls_back_to_local_stream(app, tmp_path, monkeypatch):
    headers, batch_id, _, video, _, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    with _held_transfers():
        status, _, data = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1', headers=headers)
    assert status == 200 and data == video.read_bytes()
    assert bucket.puts == [] and bucket.signs == []


def test_valid_receipt_still_signs_fast_while_transfer_is_busy(app, tmp_path, monkeypatch):
    headers, batch_id, directory, video, digest, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    receipt = _seed_receipt(batch_id, directory, video, bucket)
    signs = len(bucket.signs)
    with _held_transfers():
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 200 and json.loads(body)['expires_in'] == 900
    assert len(bucket.signs) == signs + 1 and len(bucket.puts) == 1
    assert json.loads(body)['url'].startswith('https://fixture.invalid/')


def test_changed_identity_with_busy_transfer_falls_back_instead_of_uploading(app, tmp_path, monkeypatch):
    headers, batch_id, directory, video, digest, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    _seed_receipt(batch_id, directory, video, bucket)
    video.write_bytes(b'replaced after the receipt')
    puts = len(bucket.puts)
    with _held_transfers():
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 200 and json.loads(body) == {'url': f'/api/batches/{batch_id}/preview/R1', 'expires_in': 0}
    assert len(bucket.puts) == puts
    assert result_store.read(directory)['requests']['R1']['preview']['oss']['sha256'] == digest


def test_missing_receipt_with_empty_transfer_never_starts_an_upload(app, tmp_path, monkeypatch):
    import threading, time
    headers, batch_id, _, video, digest, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    entered = threading.Event()
    def slow_upload(*a, **kw):
        entered.set()
        time.sleep(3)
        raise AssertionError('playback must not start an upload')
    bucket.put_object_from_file = slow_upload
    bucket.object_exists = slow_upload
    started = time.monotonic()
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    elapsed = time.monotonic() - started
    assert status == 200 and elapsed < 1.5, (status, elapsed, body)
    assert json.loads(body) == {'url': f'/api/batches/{batch_id}/preview/R1', 'expires_in': 0}
    assert not entered.is_set() and bucket.puts == [] and bucket.signs == []
    status, _, data = request(app, 'GET', json.loads(body)['url'], headers={**headers, 'Range': 'bytes=0-9'})
    assert status == 206 and data == video.read_bytes()[:10]


def test_stale_receipt_with_empty_transfer_never_starts_an_upload(app, tmp_path, monkeypatch):
    import threading, time
    headers, batch_id, directory, video, digest, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    stale = {'key': f'{preview_oss.object_prefix()}/{batch_id}/R1/{"0"*64}.mp4', 'sha256': '0'*64,
             'bytes': 0, 'identity': [], 'bucket': bucket.bucket_name, 'endpoint': bucket.endpoint}
    record = result_store.read(directory)['requests']['R1']
    record['preview']['oss'] = stale
    result_store.put(directory, 'R1', record)
    entered = threading.Event()
    def slow_upload(*a, **kw):
        entered.set()
        time.sleep(3)
        raise AssertionError('playback must not start an upload')
    bucket.put_object_from_file = slow_upload
    bucket.object_exists = slow_upload
    started = time.monotonic()
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 200 and time.monotonic() - started < 1.5, (status, body)
    assert json.loads(body) == {'url': f'/api/batches/{batch_id}/preview/R1', 'expires_in': 0}
    assert not entered.is_set() and bucket.puts == [] and bucket.signs == []


def test_missing_receipt_never_takes_object_lock_or_network(app, tmp_path, monkeypatch):
    import time
    headers, batch_id, _, video, digest, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    def forbidden(*a, **kw):
        raise AssertionError('wait=False must not take the object lock or upload')
    monkeypatch.setattr(preview_oss.resource_locks, 'hold', forbidden)
    monkeypatch.setattr(preview_oss.shutil, 'copyfile', forbidden)
    started = time.monotonic()
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 200 and time.monotonic() - started < 1.5
    assert json.loads(body) == {'url': f'/api/batches/{batch_id}/preview/R1', 'expires_in': 0}
