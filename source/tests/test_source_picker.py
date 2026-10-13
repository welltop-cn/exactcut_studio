import json
import subprocess
import pytest
from exactcut import workflow as w, webserver as web
from test_web_base_path import app, login, request
from test_source_prefetch import draft_with


def make_video(path, vfr=False, offset=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'testsrc2=size=96x64:rate=30000/1001:duration=4', '-vf',
        "select='not(eq(mod(n,5),2))'" if vfr else 'null', '-fps_mode', 'vfr',
        '-c:v', 'libx264', '-g', '30', '-bf', '3',
        *(['-output_ts_offset', '2'] if offset else []), str(path)], check=True)


@pytest.fixture
def source(app):
    item = w.create_blank()
    video = w.ROOT / 'projects/101573/source/videos/EP1.mp4'
    make_video(video)
    return app, item, login(app), video


def prepare(source):
    app, item, headers, _ = source
    return request(app, 'POST', f"/home/api/batches/{item['id']}/source-picker",
                   {'draft': draft_with('EP1'), 'episode': 'EP1'}, headers)


def test_prepare_cached_without_material_fetch_and_no_global_lock(source, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    def forbidden():
        raise AssertionError('source indexing must not hold global mutation lock')
    monkeypatch.setattr(web, 'mutation_lock', forbidden)
    status, _, body = prepare(source)
    assert status == 200, body
    result = json.loads(body)
    assert result['episode'] == 'EP1'
    assert result['times_ms'][:4] == [0, 33, 66, 100]
    assert result['duration_ms'] > result['times_ms'][-1]
    assert len(result['key']) == 64


def test_auth_csrf_missing_source_and_stale_key(source):
    app, item, headers, video = source
    endpoint = f"/home/api/batches/{item['id']}/source-picker"
    body = {'draft': draft_with('EP1'), 'episode': 'EP1'}
    assert request(app, 'POST', endpoint, body)[0] == 401
    assert request(app, 'POST', endpoint, body, {'Cookie': headers['Cookie']})[0] == 403
    status, _, payload = prepare(source)
    assert status == 200
    key = json.loads(payload)['key']
    video.unlink()
    status, _, payload = request(app, 'GET', endpoint + f'/{key}/video', headers=headers)
    assert status == 400
    assert str(w.ROOT).encode() not in payload
    status, _, payload = prepare(source)
    assert status == 400 and '准备正片' in json.loads(payload)['error']


@pytest.mark.parametrize('vfr,offset', [(False, False), (True, False), (True, True)])
def test_frame_pixels_and_millisecond_boundaries_match_sequential_decode(source, vfr, offset):
    from exactcut import source_picker
    _, item, _, video = source
    make_video(video, vfr=vfr, offset=offset)
    path = w.folder(item['id'])
    meta = source_picker.prepare(path, draft_with('EP1'), 'EP1')
    for index in [0, 1, 29, 31, len(meta['times_ms']) - 1]:
        expected = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-vf',
            f'select=eq(n\\,{index})', '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
            check=True, capture_output=True).stdout
        assert source_picker.frame(path, meta['key'], index) == expected
        actual = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f"{meta['times_ms'][index]/1000:.3f}",
            '-accurate_seek', '-i', str(video), '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
            check=True, capture_output=True).stdout
        assert actual == expected


def test_managed_batch_get_access_range_and_changed_source(source, monkeypatch):
    from exactcut import material_versions as m, accounts, source_picker
    app, item, headers, video = source
    monkeypatch.setenv('EXACTCUT_MATERIAL_VERSIONS', '1')
    m.enroll(w.folder(item['id']))
    status, _, payload = prepare(source)
    assert status == 200, payload
    meta = json.loads(payload)
    endpoint = f"/home/api/batches/{item['id']}/source-picker/{meta['key']}"
    assert request(app, 'GET', endpoint + '/video')[0] == 401
    assert request(app, 'GET', endpoint + '/video', headers={**headers, 'Range': 'bytes=0-99'})[0] == 206
    assert request(app, 'GET', endpoint + '/frame/1', headers=headers)[2].startswith(b'\x89PNG')
    accounts.manage(app.auth_db, 'admin', {'username': 'alice', 'name': 'Alice', 'password': 'fixture-password'})
    accounts.assign(w.folder(item['id']), {'username': 'admin'})
    _, h, _ = request(app, 'POST', '/home/api/login', {'username': 'alice', 'password': 'fixture-password'}, {'X-Exactcut-Request': '1'})
    other = {'Cookie': h['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}
    assert request(app, 'GET', endpoint + '/frame/1', headers=other)[0] == 404
    make_video(video, vfr=True)
    assert request(app, 'GET', endpoint + '/frame/1', headers=headers)[0] == 400
    assert json.loads(prepare(source)[2])['key'] != meta['key']


def test_bounded_work_and_cached_frames_do_not_decode_again(source, monkeypatch):
    from exactcut import source_picker as picker
    _, item, _, _ = source
    path = w.folder(item['id'])
    meta = picker.prepare(path, draft_with('EP1'), 'EP1')
    expected = picker.frame(path, meta['key'], 1)
    monkeypatch.setattr(picker.subprocess, 'run', lambda *a, **kw: pytest.fail('cache must avoid process'))
    assert picker.frame(path, meta['key'], 1) == expected
    picker._SLOTS.acquire(); picker._SLOTS.acquire()
    try:
        with pytest.raises(ValueError, match='繁忙'):
            picker.frame(path, meta['key'], 2)
    finally:
        picker._SLOTS.release(); picker._SLOTS.release()


def test_existing_renderer_includes_selected_frame_with_next_boundary(source):
    from exactcut import source_picker, renderer
    _, item, _, video = source
    meta = source_picker.prepare(w.folder(item['id']), draft_with('EP1'), 'EP1')
    output = video.parent / 'cut.mp4'
    config = {'width': 96, 'height': 64, 'fps': '30000/1001', 'video_codec': 'libx264',
              'preset': 'ultrafast', 'crf': 0, 'audio_codec': 'aac', 'audio_rate': 48000, 'audio_channels': 2}
    renderer._render_episode_unit(video, meta['times_ms'][31], meta['times_ms'][34], output, config)
    def raw(path, vf):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', vf,
             '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'], check=True, capture_output=True).stdout
    assert raw(output, 'null') == raw(video, 'select=between(n\\,31\\,33)')


def test_rejects_non_draft_invalid_episode_and_frame_index(source):
    from exactcut import source_picker
    _, item, _, _ = source
    path = w.folder(item['id'])
    with pytest.raises(ValueError, match='有效片段'):
        source_picker.prepare(path, draft_with('EP1'), 'EP2')
    meta = source_picker.prepare(path, draft_with('EP1'), 'EP1')
    with pytest.raises(ValueError, match='超出范围'):
        source_picker.frame(path, meta['key'], len(meta['times_ms']))
    w.update(path, 'queued', '等待制作')
    with pytest.raises(ValueError):
        source_picker.prepare(path, draft_with('EP1'), 'EP1')


def test_picker_script_is_served_and_source_io_failure_is_sanitized(source, monkeypatch):
    from exactcut import source_picker
    app, item, headers, _ = source
    assert request(app, 'GET', '/home/source-picker.js', headers=headers)[0] == 200
    status, _, payload = prepare(source)
    key = json.loads(payload)['key']
    def failed(*args):
        raise OSError('/private/secret/source.mp4')
    monkeypatch.setattr(source_picker, '_fingerprint', failed)
    status, _, payload = request(app, 'GET', f"/home/api/batches/{item['id']}/source-picker/{key}/video", headers=headers)
    assert status == 400
    assert b'/private/secret' not in payload


def test_frame_response_survives_cache_eviction(source):
    from exactcut import source_picker as picker
    _, item, _, _ = source
    path = w.folder(item['id'])
    meta = picker.prepare(path, draft_with('EP1'), 'EP1')
    first = picker.frame(path, meta['key'], 1)
    assert isinstance(first, bytes)
    directory = path / 'source-picker' / meta['key']
    for index in range(1000, 1256):
        (directory / f'{index}.png').write_bytes(b'cached')
    picker.frame(path, meta['key'], 2)
    assert not (directory / '1.png').exists()
    assert first.startswith(b'\x89PNG')


def test_concurrent_same_frame_misses_return_complete_immutable_images(source, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from exactcut import source_picker as picker
    _, item, _, _ = source
    path = w.folder(item['id'])
    meta = picker.prepare(path, draft_with('EP1'), 'EP1')
    run = picker.subprocess.run
    barrier = threading.Barrier(2)
    def together(*args, **kwargs):
        barrier.wait(timeout=5)
        return run(*args, **kwargs)
    monkeypatch.setattr(picker.subprocess, 'run', together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: picker.frame(path, meta['key'], 30), range(2)))
    assert all(isinstance(value, bytes) for value in results)
    assert results[0] == results[1]
    assert results[0].startswith(b'\x89PNG')
    target = path / 'source-picker' / meta['key'] / '30.png'
    assert target.read_bytes() == results[0]
    assert not list(target.parent.glob('*.tmp'))
