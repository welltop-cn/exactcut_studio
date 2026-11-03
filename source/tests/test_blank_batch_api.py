import base64
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from exactcut import webserver as server
from exactcut import workflow as w
from browser_support import shared_playwright


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


def test_ending_thumbnail_is_small_and_original_is_unchanged(app):
    import io
    from PIL import Image
    created = json.loads(call(app, 'POST', '/api/batches/blank', {})[2])
    bid = created['id']; rid = created['draft']['rows'][0]['request_id']
    image = Image.effect_noise((1800, 2400), 60).convert('RGB')
    stream = io.BytesIO(); image.save(stream, 'JPEG', quality=85)
    original = stream.getvalue()
    assert call(app, 'POST', f'/api/batches/{bid}/ending-image',
                {'request_id': rid, 'content': base64.b64encode(original).decode()})[0] == 200
    endpoint = f'/api/batches/{bid}/ending-thumbnail/{rid}'
    assert call(app, 'GET', endpoint, auth=False)[0] == 401
    status, kind, body = call(app, 'GET', endpoint)
    assert status == 200, body
    assert kind.startswith('image/webp')
    thumbnail = Image.open(io.BytesIO(body))
    assert thumbnail.size == (360, 480)
    assert len(body) < 100_000 and len(body) < len(original) / 10
    assert call(app, 'GET', f'/api/batches/{bid}/ending-image/{rid}')[2] == original


@pytest.mark.ui
def test_ending_preview_uses_lazy_thumbnail_in_hidden_editor():
    import io
    from pathlib import Path
    from PIL import Image
    playwright = pytest.importorskip('playwright.sync_api')
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    markup = next(line for line in source.splitlines() if line.startswith('function endingSettings('))
    stream = io.BytesIO(); Image.new('RGB', (20, 30)).save(stream, 'WEBP')
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            images = []
            def respond(route):
                if '/ending-thumbnail/' in route.request.url:
                    images.append(route.request.url)
                    route.fulfill(content_type='image/webp', body=stream.getvalue())
                else:
                    route.fulfill(content_type='text/html', body='<div id="editor" hidden></div>')
            page.route('http://fixture/**', respond)
            page.goto('http://fixture/')
            page.add_script_tag(content="window.current={id:'fixture'};window.endingImages=new Map();window.endingOf=r=>r;window.esc=s=>String(s??'');window.appUrl=s=>s;"+markup)
            page.evaluate("document.getElementById('editor').innerHTML=endingSettings({request_id:'r1',image:'fixture.jpg',mode:'image',duration_ms:3000},0)")
            page.wait_for_timeout(200)
            assert not images, 'hidden editor must not start image downloads'
            image = page.locator('.ending-preview')
            assert image.get_attribute('fetchpriority') == 'low'
            page.evaluate("document.getElementById('editor').hidden=false")
            page.wait_for_function("document.querySelector('.ending-preview').naturalWidth>0")
            assert len(images) == 1 and '/ending-thumbnail/r1?' in images[0]
        finally:
            browser.close()
