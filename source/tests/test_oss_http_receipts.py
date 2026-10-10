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
def test_authenticated_http_persists_real_oss_receipt_before_returning_url(app, tmp_path, monkeypatch, original):
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
    monkeypatch.setattr(workflow, 'preview_file', lambda *a, **kw: (video, dict(record)))
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    bucket = SigningBucket()
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    suffix = 'download' if original else 'preview-url'
    endpoint = f'/home/api/batches/{batch_id}/{suffix}/R1'
    status, response_headers, body = request(app, 'GET', endpoint, headers=headers)
    assert status == (302 if original else 200), body
    assert response_headers['Cache-Control'] == 'no-store'
    updated = result_store.read(directory)['requests']['R1']
    receipt = updated['original_oss'] if original else updated['preview']['oss']
    assert receipt['sha256'] == digest
    assert receipt['identity'] == preview_oss._identity(video)
    assert updated['preserve_me'] == 'existing metadata'
    assert len(bucket.puts) == 1
    assert bucket.objects[receipt['key']][1]['x-oss-object-acl'] == 'private'
    if original:
        assert body == b''
        assert bucket.signs[0][3]['response-content-disposition'].startswith('attachment;')
    else:
        assert json.loads(body)['expires_in'] == 900


def test_changed_result_during_upload_refuses_url_and_preserves_new_result(app, tmp_path, monkeypatch):
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    directory = workflow.folder(batch_id) / 'production'
    video = tmp_path / 'old-preview.mp4'
    video.write_bytes(b'old preview')
    old = {'status': 'qc_passed', 'output_sha256': 'old-master',
           'preview': {'sha256': hashlib.sha256(video.read_bytes()).hexdigest()}}
    result_store.put(directory, 'R1', old)
    monkeypatch.setattr(workflow, 'preview_file', lambda *a, **kw: (video, old))
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
    status, _, body = request(app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
    assert status == 503
    assert result_store.read(directory)['requests']['R1'] == new
    assert len(bucket.objects) == 1  # The uploaded immutable object is retained.
    assert bucket.signs == []
    assert b'fixture.invalid' not in body
