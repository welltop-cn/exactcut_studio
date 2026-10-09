import base64
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from exactcut import webserver as server
from exactcut import workflow as w


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    instance = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    instance.password = 'blank-batch-local-test-password'
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()


def call(app, method, path, data=None, auth=True, csrf=True):
    headers = {}
    if auth:
        headers['Authorization'] = 'Basic ' + base64.b64encode(('admin:' + app.password).encode()).decode()
    if csrf:
        headers['X-Exactcut-Request'] = '1'
    connection = HTTPConnection(*app.server_address, timeout=5)
    try:
        connection.request(method, path, None if data is None else json.dumps(data), headers)
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type'), response.read()
    finally:
        connection.close()


def test_blank_batch_requires_auth_and_csrf_then_is_owned_and_readable(app):
    assert call(app, 'POST', '/api/batches/blank', {}, auth=False)[0] == 401
    assert call(app, 'POST', '/api/batches/blank', {}, csrf=False)[0] == 403

    status, content_type, body = call(app, 'POST', '/api/batches/blank', {})
    assert status == 201
    assert content_type.startswith('application/json')
    created = json.loads(body)
    assert created['filename'] == '网页填写'
    assert created['runtime']['status'] == 'draft'
    row = created['draft']['rows'][0]
    assert row['source'] == '网页填写'
    assert row['clips'][0]['episode'] == ''
    assert row['hook_options'] == {'mode': 'ai', 'style': 'auto', 'position': 'auto', 'duration_ms': None, 'manual_ranges': []}

    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}")
    assert status == 200
    assert json.loads(body)['id'] == created['id']


def test_ending_image_round_trip_rejects_bad_content_and_replaces_old_file(app):
    created = json.loads(call(app, 'POST', '/api/batches/blank', {})[2])
    batch_id = created['id']
    request_id = created['draft']['rows'][0]['request_id']
    endpoint = f"/api/batches/{batch_id}/ending-image"

    png = b'\x89PNG\r\n\x1a\n' + b'first-image'
    status, _, body = call(app, 'POST', endpoint, {'request_id': request_id, 'content': base64.b64encode(png).decode()})
    assert status == 200
    assert json.loads(body)['image'] == request_id + '.png'

    status, content_type, body = call(app, 'GET', f"/api/batches/{batch_id}/ending-image/{request_id}")
    assert status == 200
    assert content_type.startswith('image/png')
    assert body == png

    status, _, body = call(app, 'POST', endpoint, {'request_id': request_id, 'content': base64.b64encode(b'not-an-image').decode()})
    assert status == 400
    assert '结尾图片' in json.loads(body)['error']

    jpeg = b'\xff\xd8\xff' + b'second-image'
    status, _, body = call(app, 'POST', endpoint, {'request_id': request_id, 'content': base64.b64encode(jpeg).decode()})
    assert status == 200
    assert json.loads(body)['image'] == request_id + '.jpg'
    endings = w.folder(batch_id) / 'endings'
    assert sorted(path.name for path in endings.iterdir()) == [request_id + '.jpg']
    status, content_type, body = call(app, 'GET', f"/api/batches/{batch_id}/ending-image/{request_id}")
    assert status == 200
    assert content_type.startswith('image/jpeg')
    assert body == jpeg


def test_ending_image_rejects_path_like_request_id_and_missing_file(app):
    created = json.loads(call(app, 'POST', '/api/batches/blank', {})[2])
    batch_id = created['id']
    endpoint = f"/api/batches/{batch_id}/ending-image"
    png = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'payload').decode()

    assert call(app, 'POST', endpoint, {'request_id': '../escape', 'content': png})[0] == 400
    assert call(app, 'GET', f"/api/batches/{batch_id}/ending-image/R-missing")[0] == 400
