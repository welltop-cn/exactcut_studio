"""Local dev originals use the existing wire fields without enabling remote access."""
import base64
import json
import subprocess
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from exactcut import accounts, originals, webserver, workflow


@pytest.fixture(scope='module')
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp('local-original-video') / 'sample.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=32x32:rate=10:duration=0.5', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path.read_bytes()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    monkeypatch.delenv('EXACTCUT_SOURCE_ROOT', raising=False)
    monkeypatch.setattr(workflow, 'ROOT', tmp_path)
    monkeypatch.setattr(workflow, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(workflow, 'FEEDBACK', tmp_path / 'feedback')
    monkeypatch.setattr(webserver, 'ROOT', tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('local originals must not access external providers')
    for name in ('connect', 'metabase_query', '_get_stream'):
        monkeypatch.setattr(originals, name, forbidden)
    instance = ThreadingHTTPServer(('127.0.0.1', 0), webserver.Handler)
    instance.password = 'local-originals-test-password'
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    # A real saved batch keeps the same access gate as the remote originals API.
    batch_id = 'a' * 32
    batch = workflow.BATCHES / batch_id
    batch.mkdir(parents=True)
    (batch / 'draft.json').write_text(json.dumps({'draft': {'drama_id': '101682'}}))
    try:
        yield instance, batch_id
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def request(app, suffix='', *, auth=True, username='admin', password=None, byte_range=None):
    instance, batch = app
    headers = {}
    if auth:
        token = base64.b64encode(f'{username}:{password or instance.password}'.encode()).decode()
        headers['Authorization'] = 'Basic ' + token
    if byte_range:
        headers['Range'] = byte_range
    conn = HTTPConnection(*instance.server_address, timeout=5)
    try:
        conn.request('GET', f'/api/batches/{batch}/originals{suffix}', headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def put_video(tmp_path, video, episode='EP1', *, project=False):
    directory = tmp_path / ('projects/101682/source/videos' if project else 'sources/101682')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (episode + '.mp4')
    path.write_bytes(video)
    return path


def test_fetch_disabled_empty_local_list_is_normal_unready(app):
    status, _, body = request(app)
    assert status == 200
    result = json.loads(body)
    assert result['drama_id'] == '101682' and result['episodes'] == []
    assert not result.get('notice')  # A complete empty local catalog is not a provider error.
    status, _, body = request(app, '/EP1/file')
    assert status == 404
    assert 'EXACTCUT_ALLOW_MATERIAL_FETCH' not in json.loads(body)['error']


@pytest.mark.integration
def test_local_list_uses_real_cache_and_project_fallback_without_remote_calls(app, tmp_path, video):
    put_video(tmp_path, video)
    put_video(tmp_path, video, 'EP11', project=True)
    put_video(tmp_path, b'', 'EP2')
    put_video(tmp_path, video, 'EP501')
    status, _, body = request(app)
    assert status == 200
    result = json.loads(body)
    assert result['episodes'] == [
        {'episode': 'EP1', 'listed': None, 'bytes': len(video)},
        {'episode': 'EP11', 'listed': None, 'bytes': len(video)},
    ]
    assert 'notice' not in result
    assert 'url' not in str(result['episodes'])


@pytest.mark.integration
@pytest.mark.parametrize('project', [False, True])
@pytest.mark.parametrize('suffix', ['/EP1', '/EP1/file'])
def test_local_original_streams_real_video_and_range(app, tmp_path, video, project, suffix):
    path = put_video(tmp_path, video, project=project)
    before = path.read_bytes()
    status, headers, body = request(app, suffix)
    assert status == 200 and body == video
    assert headers['Content-Type'] == 'video/mp4'
    assert 'no-store' in headers['Cache-Control']
    assert headers['Content-Disposition'] == 'attachment; filename="EP1.mp4"'
    status, headers, body = request(app, suffix, byte_range='bytes=0-31')
    assert status == 206 and body == video[:32]
    assert headers['Content-Range'] == f'bytes 0-31/{len(video)}'
    assert path.read_bytes() == before


@pytest.mark.integration
def test_local_original_keeps_batch_authentication_and_owner_gate(app, tmp_path, video):
    put_video(tmp_path, video)
    instance, batch = app
    instance.auth_db = tmp_path / '.web-state/accounts.sqlite3'
    accounts.initialize(instance.auth_db, instance.password)
    for user in ('alice', 'bob'):
        accounts.manage(instance.auth_db, 'admin', {
            'action': 'create', 'username': user, 'name': user, 'password': user + '-test-password',
        })
    accounts.assign(workflow.BATCHES / batch, {'username': 'alice'})
    for suffix in ('', '/EP1', '/EP1/file'):
        assert request(app, suffix, auth=False)[0] == 401
        assert request(app, suffix, username='bob', password='bob-test-password')[0] == 404
        assert request(app, suffix, username='alice', password='alice-test-password')[0] == 200


@pytest.mark.integration
@pytest.mark.parametrize('component', ['episode', 'drama', 'project-source'])
def test_local_original_does_not_follow_symlinks(app, tmp_path, video, component):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'EP1.mp4').write_bytes(video)
    cache = tmp_path / 'sources'
    cache.mkdir()
    if component == 'episode':
        (cache / '101682').mkdir()
        (cache / '101682/EP1.mp4').symlink_to(outside / 'EP1.mp4')
    elif component == 'drama':
        (cache / '101682').symlink_to(outside, target_is_directory=True)
    else:
        project = tmp_path / 'projects/101682'
        project.mkdir(parents=True)
        (outside / 'videos').mkdir()
        (outside / 'videos/EP1.mp4').write_bytes(video)
        (project / 'source').symlink_to(outside, target_is_directory=True)
    status, _, body = request(app)
    assert status == 200 and json.loads(body)['episodes'] == []
    assert request(app, '/EP1/file')[0] == 404


@pytest.mark.parametrize('suffix', ['?drama_id=../../outside', '/..%2fEP1/file', '/EP501/file'])
def test_local_original_rejects_invalid_identifiers(app, suffix):
    status, _, _ = request(app, suffix)
    assert status == 400


@pytest.mark.integration
@pytest.mark.parametrize('environment', ['development', 'production'])
def test_fetch_enabled_keeps_remote_listing_even_with_local_copy(tmp_path, monkeypatch, video, environment):
    from test_originals import FakeStore
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', environment)
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    put_video(tmp_path, video)
    store = FakeStore(listings={
        ('content', 'assets/101682/video/'): [('assets/101682/video/EP8.mp4', 123)],
        ('source', 'tb/tbl5wK7ykMP9ZNKb/101682/'): [],
    })
    result = originals.load_originals('101682', store=store, listed={8})
    assert result == {'drama_id': '101682', 'episodes': [{'episode': 'EP8', 'listed': True, 'bytes': 123}]}
