import json
import base64
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from exactcut import originals
from exactcut import webserver as server
from exactcut import workflow as w


class FakeStore:
    def __init__(self, heads=None, listings=None):
        self.heads = heads or {}
        self.listings = listings or {}
        self.calls = []

    def head(self, role, key):
        self.calls.append(('head', role, key))
        return self.heads.get((role, key), False)

    def keys(self, role, prefix):
        self.calls.append(('keys', role, prefix))
        if (role, prefix) not in self.listings and role == 'source':
            raise originals.OriginalsUnavailable(503, '原片列表暂时读不到')
        return self.listings.get((role, prefix), [])

    def sign(self, role, key, episode):
        self.calls.append(('sign', role, key, episode))
        return f'https://oss.invalid/{role}/{episode}'

    def copy_object(self, *args, **kwargs):
        raise AssertionError('original download must not copy')

    def get_object(self, *args, **kwargs):
        raise AssertionError('original download must not read object bytes')


def _enable(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')


def test_public_endpoint_drops_internal_host():
    assert originals.browser_endpoint('https://oss-us-east-1-internal.aliyuncs.com') == 'https://oss-us-east-1.aliyuncs.com'
    assert originals.browser_endpoint('oss-us-east-1.aliyuncs.com') == 'https://oss-us-east-1.aliyuncs.com'
    with pytest.raises(originals.OriginalsUnavailable, match='公网'):
        originals.browser_endpoint('https://oss-internal.example.local')


def test_shelf_query_is_not_capped_at_ten():
    seen = []

    def query(sql):
        seen.append(sql)
        return [{'trailer_ep': 'EP11'}, {'trailer_ep': '第2集'}, {'trailer_ep': 'EP0'}]

    assert originals.fetch_listed('101606', query=query) == {11}
    sql = seen[0].upper()
    assert 'LIMIT' not in sql
    assert 'HIDDEN = 0' in sql
    assert 'PUBLISH_AT <= NOW()' in sql
    assert '101606' in seen[0]


def test_assemble_prefers_content_and_marks_unlisted():
    result = originals.assemble(
        '101606',
        [('assets/101606/video/EP01.mp4', 10), ('assets/101606/video/EP1.mp4', 20), ('notes.txt', 1)],
        [('tb/tbl5wK7ykMP9ZNKb/101606/EP1.mp4', 99), ('tb/tbl5wK7ykMP9ZNKb/101606/EP11.mp4', 30),
         ('tb/tbl5wK7ykMP9ZNKb/101606/EP501.mp4', 5)],
        {1},
    )
    assert result['episodes'] == [
        {'episode': 'EP1', 'listed': True, 'bytes': 20},
        {'episode': 'EP11', 'listed': False, 'bytes': 30},
    ]
    assert 'notice' not in result


def test_sign_uses_content_object_without_copy_or_read(monkeypatch):
    _enable(monkeypatch)
    store = FakeStore(heads={('content', 'assets/101606/video/EP11.mp4'): True,
                             ('source', 'tb/tbl5wK7ykMP9ZNKb/101606/EP11.mp4'): True})
    signed = originals.sign_original('101606', 'EP11', store=store)
    assert signed == {'url': 'https://oss.invalid/content/EP11', 'expires_in': 900}
    assert [call[0] for call in store.calls] == ['head', 'sign']
    assert store.calls[0][1:] == ('content', 'assets/101606/video/EP11.mp4')


def test_sign_falls_back_to_source_then_reports_missing(monkeypatch):
    _enable(monkeypatch)
    store = FakeStore(heads={('source', 'tb/tbl5wK7ykMP9ZNKb/101606/EP2.mp4'): True})
    assert originals.sign_original('101606', 'EP2', store=store)['url'].endswith('/source/EP2')
    empty = FakeStore()
    with pytest.raises(originals.OriginalsUnavailable, match='源片不存在') as caught:
        originals.sign_original('101606', 'EP3', store=empty)
    assert caught.value.status == 404
    assert all(call[0] in {'head', 'keys'} for call in empty.calls)


def test_source_gap_keeps_content_episodes(monkeypatch):
    _enable(monkeypatch)
    store = FakeStore(listings={('content', 'assets/101606/video/'): [('assets/101606/video/EP4.mp4', 8)]})
    result = originals.load_originals('101606', store=store, listed=set())
    assert result['episodes'] == [{'episode': 'EP4', 'listed': False, 'bytes': 8}]
    assert '源桶' in result['notice']


def test_disabled_fetch_does_not_connect(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.delenv('EXACTCUT_ALLOW_MATERIAL_FETCH', raising=False)
    monkeypatch.setattr(originals, 'connect', lambda: (_ for _ in ()).throw(AssertionError('connected')))
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_MATERIAL_FETCH'):
        originals.load_originals('101606')


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    instance = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    instance.password = 'originals-local-test-password'
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()


def call(app, method, path, auth=True):
    headers = {}
    if auth:
        headers['Authorization'] = 'Basic ' + base64.b64encode(('admin:' + app.password).encode()).decode()
    connection = HTTPConnection(*app.server_address, timeout=5)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def _draft(tmp_path, batch_id, drama_id):
    path = tmp_path / '.web-state/batches' / batch_id / 'draft.json'
    document = json.loads(path.read_text())
    document['draft']['drama_id'] = drama_id
    path.write_text(json.dumps(document))


def test_list_and_redirect(app, tmp_path, monkeypatch):
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(('admin:' + app.password).encode()).decode(),
        'X-Exactcut-Request': '1',
    }
    connection = HTTPConnection(*app.server_address, timeout=5)
    connection.request('POST', '/api/batches/blank', '{}', headers)
    created = json.loads(connection.getresponse().read())
    connection.close()
    _draft(tmp_path, created['id'], '101606')
    monkeypatch.setattr(originals, 'load_originals', lambda drama: {
        'drama_id': drama, 'episodes': [{'episode': 'EP1', 'listed': True, 'bytes': 3}, {'episode': 'EP11', 'listed': False, 'bytes': 4}]})
    monkeypatch.setattr(originals, 'sign_original', lambda drama, episode, store=None: {
        'url': 'https://oss.invalid/private/' + episode, 'expires_in': 900})

    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals", auth=False)
    assert status == 401
    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals")
    payload = json.loads(body)
    assert payload['drama_id'] == '101606'
    assert payload['episodes'][1]['listed'] is False
    assert 'url' not in payload['episodes'][0]

    status, headers, body = call(app, 'GET', f"/api/batches/{created['id']}/originals/EP11")
    header_map = {name.lower(): value for name, value in headers}
    assert status == 302
    assert header_map['location'] == 'https://oss.invalid/private/EP11'
    assert header_map['cache-control'] == 'no-store'
    assert header_map['referrer-policy'] == 'no-referrer'
    assert body == b''


def test_missing_original_is_404_and_disabled_fetch_stays_local(app, tmp_path, monkeypatch):
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(('admin:' + app.password).encode()).decode(),
        'X-Exactcut-Request': '1',
    }
    connection = HTTPConnection(*app.server_address, timeout=5)
    connection.request('POST', '/api/batches/blank', '{}', headers)
    created = json.loads(connection.getresponse().read())
    connection.close()
    _draft(tmp_path, created['id'], '101606')

    def missing(drama, episode, store=None):
        raise originals.OriginalsUnavailable(404, '源片不存在')

    real_sign = originals.sign_original
    monkeypatch.setattr(originals, 'sign_original', missing)
    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals/EP9")
    assert status == 404
    assert '源片不存在' in json.loads(body)['error']

    monkeypatch.setattr(originals, 'connect', lambda: (_ for _ in ()).throw(AssertionError('connected')))
    monkeypatch.setattr(originals, 'sign_original', real_sign)
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.delenv('EXACTCUT_ALLOW_MATERIAL_FETCH', raising=False)
    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals/EP1")
    assert status == 400
    assert 'EXACTCUT_ALLOW_MATERIAL_FETCH' in json.loads(body)['error']


def test_query_drama_id_overrides_the_saved_draft(app, tmp_path, monkeypatch):
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(('admin:' + app.password).encode()).decode(),
        'X-Exactcut-Request': '1',
    }
    connection = HTTPConnection(*app.server_address, timeout=5)
    connection.request('POST', '/api/batches/blank', '{}', headers)
    created = json.loads(connection.getresponse().read())
    connection.close()
    _draft(tmp_path, created['id'], '101050')

    def listed(drama):
        from exactcut.source_cache import check_drama
        check_drama(drama)
        return {'drama_id': drama, 'episodes': [{'episode': 'EP1', 'listed': True, 'bytes': 3}]}

    monkeypatch.setattr(originals, 'load_originals', listed)
    monkeypatch.setattr(originals, 'sign_original', lambda drama, episode, store=None: {
        'url': 'https://oss.invalid/' + drama + '/' + episode, 'expires_in': 900})

    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals?drama_id=101607")
    assert status == 200
    assert json.loads(body)['drama_id'] == '101607'
    status, headers, body = call(app, 'GET', f"/api/batches/{created['id']}/originals/EP1?drama_id=101607")
    header_map = {name.lower(): value for name, value in headers}
    assert status == 302
    assert header_map['location'] == 'https://oss.invalid/101607/EP1'
    assert body == b''
    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals")
    assert json.loads(body)['drama_id'] == '101050'
    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals?drama_id=12")
    assert status == 400
    assert '无效剧号' in json.loads(body)['error']


def test_open_original_streams_and_rejects_failed_fetch(monkeypatch):
    opened = []

    class Remote:
        status = 200
        headers = {'Content-Length': '3'}

        def iter_bytes(self):
            yield b'xyz'

        def close(self):
            opened.append('close')

    monkeypatch.setattr(originals, 'sign_original', lambda drama, episode, store=None: {'url': 'https://oss.invalid/EP1', 'expires_in': 900})
    remote = originals.open_original('101606', 'EP1', getter=lambda url: opened.append(url) or Remote())
    assert b''.join(remote.iter_bytes()) == b'xyz'
    remote.close()
    assert opened == ['https://oss.invalid/EP1', 'close']

    class Failed:
        status = 403
        headers = {}

        def close(self):
            opened.append('failed-close')

    with pytest.raises(originals.OriginalsUnavailable, match='原片暂时下不了'):
        originals.open_original('101606', 'EP1', getter=lambda url: Failed())
    assert opened[-1] == 'failed-close'


def test_file_stream_returns_bytes_without_redirect(app, tmp_path, monkeypatch):
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(('admin:' + app.password).encode()).decode(),
        'X-Exactcut-Request': '1',
    }
    connection = HTTPConnection(*app.server_address, timeout=5)
    connection.request('POST', '/api/batches/blank', '{}', headers)
    created = json.loads(connection.getresponse().read())
    connection.close()
    _draft(tmp_path, created['id'], '101606')

    class Remote:
        headers = {'Content-Length': '4', 'Content-Type': 'video/mp4'}

        def iter_bytes(self):
            yield b'abcd'

        def close(self):
            pass

    monkeypatch.setattr(originals, 'open_original', lambda drama, episode, store=None: Remote())
    status, response_headers, body = call(app, 'GET', f"/api/batches/{created['id']}/originals/EP1/file")
    header_map = {name.lower(): value for name, value in response_headers}
    assert status == 200
    assert body == b'abcd'
    assert header_map['content-type'].startswith('video/mp4')
    assert header_map['cache-control'] == 'no-store'
    assert 'location' not in header_map

    status, _, body = call(app, 'GET', f"/api/batches/{created['id']}/originals/EP1/other")
    assert status == 400
    assert '无效集号' in json.loads(body)['error']
