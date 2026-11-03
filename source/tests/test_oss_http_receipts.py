"""Exercise the complete signing/receipt callback through authenticated HTTP."""
import hashlib
import json
import threading
from contextlib import contextmanager

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


def _preview_fixture(app, tmp_path, monkeypatch, *, bucket=None, request_id='R1'):
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    directory = workflow.folder(batch_id) / 'production'
    video = tmp_path / 'media.mp4'
    video.write_bytes(b'private media fixture for async preparation')
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    record = {'status': 'qc_passed', 'output_sha256': digest,
              'preview': {'sha256': digest, 'master_sha256': digest}}
    result_store.put(directory, request_id, record)
    monkeypatch.setattr(workflow, 'preview_file',
                        lambda *a, **kw: (video, result_store.read(directory)['requests'][request_id]))
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    bucket = bucket or SigningBucket()
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    return headers, batch_id, directory, video, digest, bucket


class ControlledBucket(SigningBucket):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.failures = 0

    def put_object_from_file(self, key, filename, headers):
        self.entered.set()
        assert self.release.wait(10), 'test upload was not released'
        if self.failures:
            self.failures -= 1
            raise RuntimeError('upload fixture failure')
        return super().put_object_from_file(key, filename, headers)


@contextmanager
def _dispatcher():
    from exactcut import interactive_queue
    instance = interactive_queue.Dispatcher(poll_seconds=.01)
    assert instance.start()
    try:
        yield instance
    finally:
        instance.stop(timeout=None)


def _held_transfers():
    from contextlib import ExitStack
    from exactcut import editor_resources
    stack = ExitStack()
    for _ in range(editor_resources.settings()['transfer_tasks']):
        stack.enter_context(editor_resources.acquire('transfer'))
    return stack


def _poll_signed(app, headers, batch_id, *, original=False, timeout=15):
    import time
    suffix = 'download' if original else 'preview-url'
    deadline = time.monotonic() + timeout
    while True:
        status, response_headers, body = request(app, 'GET', f'/home/api/batches/{batch_id}/{suffix}/R1', headers=headers)
        if status in (200, 302):
            return status, response_headers, body
        assert status == 202, (status, body)
        assert json.loads(body)['status'] == 'preparing'
        assert time.monotonic() < deadline, body
        time.sleep(.05)


def test_missing_receipt_returns_preparing_then_signs_after_background_upload(app, tmp_path, monkeypatch):
    import time
    bucket = ControlledBucket()
    headers, batch_id, _, video, digest, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher() as dispatcher:
        started = time.monotonic()
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
        assert status == 202 and time.monotonic() - started < 1.5
        preparing = json.loads(body)
        assert preparing['status'] == 'preparing' and preparing['retry_after_ms'] > 0
        assert bucket.signs == [] and 'url' not in preparing
        assert bucket.entered.wait(5)
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
        assert status == 202 and json.loads(body)['status'] == 'preparing'
        state = snapshot()
        assert len(state['pending']) + len(state['active']) == 1
        bucket.release.set()
        status, _, body = _poll_signed(app, headers, batch_id)
        result = json.loads(body)
        assert result['expires_in'] == 900 and result['url'].startswith('https://fixture.invalid/')
        assert len(bucket.puts) == 1 and bucket.signs
        assert '/preview/R1' not in result['url']


def test_full_transfer_slots_queue_background_work_then_complete(app, tmp_path, monkeypatch):
    import time
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, _, video, digest, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher() as dispatcher:
        with _held_transfers():
            started = time.monotonic()
            status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
            assert status == 202 and time.monotonic() - started < 1.5
            time.sleep(.2)
            assert dispatcher.active_workers == 0           # queued on the transfer pool, not an HTTP wait
            assert len(snapshot()['pending']) == 1
        status, _, body = _poll_signed(app, headers, batch_id)
    assert json.loads(body)['expires_in'] == 900 and len(bucket.puts) == 1


def test_same_resource_is_prepared_once_across_clients(app, tmp_path, monkeypatch):
    bucket = ControlledBucket()
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher():
        for _ in range(2):
            status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
            assert status == 202 and json.loads(body)['status'] == 'preparing'
        assert bucket.entered.wait(5)
        state = snapshot()
        assert len(state['pending']) + len(state['active']) == 1
        assert len(state['active']) <= 1
        bucket.release.set()
        status, _, body = _poll_signed(app, headers, batch_id)
        assert len(bucket.puts) == 1


def test_valid_receipt_signs_fast_without_queueing(app, tmp_path, monkeypatch):
    bucket = SigningBucket()
    headers, batch_id, directory, video, digest, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    _seed_receipt(batch_id, directory, video, bucket)
    signs = len(bucket.signs)
    with _held_transfers():
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 200 and json.loads(body)['expires_in'] == 900
    assert len(bucket.signs) == signs + 1 and len(bucket.puts) == 1
    assert json.loads(body)['url'].startswith('https://fixture.invalid/')
    assert not snapshot()['pending'] and not snapshot()['active']


def test_source_change_supersedes_old_preparation_and_uploads_new_copy(app, tmp_path, monkeypatch):
    import time
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, directory, video, digest, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher():
        with _held_transfers():
            status, _, _ = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
            assert status == 202
            video.write_bytes(b'new source after preparation was queued')
            new_digest = hashlib.sha256(video.read_bytes()).hexdigest()
        status, _, body = _poll_signed(app, headers, batch_id)
    assert json.loads(body)['expires_in'] == 900
    assert len(bucket.puts) == 1
    assert bucket.puts[0].endswith(new_digest + '.mp4')
    assert digest != new_digest


def test_failed_upload_is_retryable(app, tmp_path, monkeypatch):
    bucket = ControlledBucket(); bucket.release.set(); bucket.failures = 1
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher():
        status, _, _ = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
        assert status == 202
        status, _, body = _poll_signed(app, headers, batch_id)
    assert json.loads(body)['expires_in'] == 900 and len(bucket.puts) == 1


def test_download_returns_preparing_not_local_bytes(app, tmp_path, monkeypatch):
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, _, video, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher():
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1', headers=headers)
        assert status == 202 and json.loads(body)['status'] == 'preparing'
        assert body != video.read_bytes()
        status, response_headers, _ = _poll_signed(app, headers, batch_id, original=True)
    assert status == 302 and response_headers['Location'].startswith('https://fixture.invalid/')


def test_queued_preparation_survives_restart(app, tmp_path, monkeypatch):
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 202 and snapshot()['pending']
    with _dispatcher():
        status, _, body = _poll_signed(app, headers, batch_id)
    assert json.loads(body)['expires_in'] == 900 and len(bucket.puts) == 1


def test_unauthenticated_or_foreign_client_cannot_prepare(app, tmp_path, monkeypatch):
    from exactcut import accounts
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    assert request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers={})[0] == 401
    accounts.manage(app.auth_db, 'admin', {'action': 'create', 'username': 'bob', 'name': 'Bob', 'password': 'bob-password-123'})
    status, response_headers, _ = request(app, 'POST', '/home/api/login', {'username': 'bob', 'password': 'bob-password-123'}, {'X-Exactcut-Request': '1'})
    bob = {'Cookie': response_headers['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}
    assert request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=bob)[0] == 404
    assert not snapshot()['pending'] and not snapshot()['active']


def snapshot():
    from exactcut import interactive_queue
    return interactive_queue.snapshot()


def test_download_status_is_ready_for_a_valid_receipt(app, tmp_path, monkeypatch):
    bucket = SigningBucket(); bucket.release = threading.Event(); bucket.entered = threading.Event()
    headers, batch_id, directory, video, digest, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    _seed_receipt(batch_id, directory, video, bucket, original=True)
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1', headers=headers)
    assert status == 200 and json.loads(body) == {'status': 'ready'}
    status, response_headers, _ = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1', headers=headers)
    assert status == 302 and response_headers['Location'].startswith('https://fixture.invalid/')


def test_download_status_queues_then_becomes_ready(app, tmp_path, monkeypatch):
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher():
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1&retry=1', headers=headers)
        assert status == 202 and json.loads(body)['status'] == 'preparing'
        status, _, body = _poll_download_status(app, headers, batch_id)
        assert status == 200 and json.loads(body) == {'status': 'ready'}
    status, response_headers, _ = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1', headers=headers)
    assert status == 302 and response_headers['Location'].startswith('https://fixture.invalid/')


def test_download_status_failure_stops_polling_and_can_be_retried(app, tmp_path, monkeypatch):
    bucket = ControlledBucket(); bucket.release.set(); bucket.failures = 1
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    with _dispatcher():
        status, _, _ = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1&retry=1', headers=headers)
        assert status == 202
        deadline = __import__('time').monotonic() + 10
        while True:
            status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1', headers=headers)
            if status == 503: break
            assert status == 202 and __import__('time').monotonic() < deadline, body
            __import__('time').sleep(.05)
        failed = json.loads(body)
        assert failed['retryable'] is True and failed['code'] == 'preview_prepare_failed'
        status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1&retry=1', headers=headers)
        assert status == 202 and json.loads(body)['status'] == 'preparing'
        status, _, body = _poll_download_status(app, headers, batch_id)
        assert status == 200 and json.loads(body) == {'status': 'ready'}
    status, response_headers, _ = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1', headers=headers)
    assert status == 302 and response_headers['Location'].startswith('https://fixture.invalid/')


def test_download_status_keeps_local_dev_behaviour_when_oss_disabled(app, tmp_path, monkeypatch):
    headers, batch_id, _, video, _, bucket = _preview_fixture(app, tmp_path, monkeypatch)
    monkeypatch.setattr(preview_oss, 'enabled', lambda: False)
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1', headers=headers)
    assert status == 200 and json.loads(body) == {'status': 'ready'}
    status, _, data = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1', headers=headers)
    assert status == 200 and data == video.read_bytes()


def test_download_status_requires_batch_permission(app, tmp_path, monkeypatch):
    from exactcut import accounts
    bucket = ControlledBucket(); bucket.release.set()
    headers, batch_id, _, _, _, _ = _preview_fixture(app, tmp_path, monkeypatch, bucket=bucket)
    assert request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1', headers={})[0] == 401
    accounts.manage(app.auth_db, 'admin', {'action': 'create', 'username': 'bob', 'name': 'Bob', 'password': 'bob-password-123'})
    status, response_headers, _ = request(app, 'POST', '/home/api/login', {'username': 'bob', 'password': 'bob-password-123'}, {'X-Exactcut-Request': '1'})
    bob = {'Cookie': response_headers['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}
    assert request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1', headers=bob)[0] == 404


def _poll_download_status(app, headers, batch_id, timeout=15):
    import time
    deadline = time.monotonic() + timeout
    while True:
        status, response_headers, body = request(app, 'GET', f'/home/api/batches/{batch_id}/download/R1?status=1', headers=headers)
        if status in (200, 503):
            return status, response_headers, body
        assert status == 202 and json.loads(body)['status'] == 'preparing', (status, body)
        assert time.monotonic() < deadline, body
        time.sleep(.05)
