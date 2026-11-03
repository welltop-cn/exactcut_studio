"""Source-frame index and existing renderer boundaries, without visual picker APIs."""
import subprocess
import time
import pytest
from exactcut import source_frames, workflow as w, webserver as web
from test_web_base_path import app, login, request
from test_source_prefetch import draft_with

pytestmark = pytest.mark.integration


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


def test_prepare_cached_without_material_fetch_and_no_global_lock(source, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    def forbidden():
        raise AssertionError('source indexing must not hold global mutation lock')
    monkeypatch.setattr(web, 'mutation_lock', forbidden)
    _, item, _, _ = source
    result = source_frames.prepare(w.folder(item['id']), draft_with('EP1'), 'EP1')
    assert result['episode'] == 'EP1'
    assert result['times_ms'][:4] == [0, 33, 66, 100]
    assert result['duration_ms'] > result['times_ms'][-1]
    assert len(result['key']) == 64
    monkeypatch.setattr(source_frames.media, 'run_checked', lambda *a, **kw: pytest.fail('cached index must not probe again'))
    assert source_frames.prepare(w.folder(item['id']), draft_with('EP1'), 'EP1') == result


@pytest.mark.parametrize('vfr,offset', [(False, False), (True, False), (True, True)])
def test_millisecond_boundaries_match_sequential_decode(source, vfr, offset):
    from exactcut import source_frames
    _, item, _, video = source
    make_video(video, vfr=vfr, offset=offset)
    path = w.folder(item['id'])
    meta = source_frames.prepare(path, draft_with('EP1'), 'EP1')
    for index in [0, 1, 29, 31, len(meta['times_ms']) - 1]:
        expected = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-vf',
            f'select=eq(n\\,{index})', '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
            check=True, capture_output=True).stdout
        actual = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f"{meta['times_ms'][index]/1000:.3f}",
            '-accurate_seek', '-i', str(video), '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
            check=True, capture_output=True).stdout
        assert actual == expected

def test_existing_renderer_includes_selected_frame_with_next_boundary(source):
    from exactcut import source_frames, renderer
    _, item, _, video = source
    meta = source_frames.prepare(w.folder(item['id']), draft_with('EP1'), 'EP1')
    output = video.parent / 'cut.mp4'
    config = {'width': 96, 'height': 64, 'fps': '30000/1001', 'video_codec': 'libx264',
              'preset': 'ultrafast', 'crf': 0, 'audio_codec': 'aac', 'audio_rate': 48000, 'audio_channels': 2}
    renderer._render_episode_unit(video, meta['times_ms'][31], meta['times_ms'][34], output, config)
    def raw(path, vf):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', vf,
             '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'], check=True, capture_output=True).stdout
    assert raw(output, 'null') == raw(video, 'select=between(n\\,31\\,33)')


def test_index_requires_current_source_and_draft_episode(source):
    _, item, _, video = source
    path = w.folder(item['id'])
    with pytest.raises(ValueError, match='有效片段'):
        source_frames.prepare(path, draft_with('EP1'), 'EP2')
    meta = source_frames.prepare(path, draft_with('EP1'), 'EP1')
    make_video(video, vfr=True)
    with pytest.raises(ValueError, match='正片已变化'):
        source_frames._load(path, meta['key'])
    assert source_frames.prepare(path, draft_with('EP1'), 'EP1')['key'] != meta['key']
    video.unlink()
    with pytest.raises(ValueError, match='准备正片'):
        source_frames.prepare(path, draft_with('EP1'), 'EP1')

def test_index_defers_without_starting_probe_or_holding_identity_lock_when_media_is_full(source, monkeypatch):
    from contextlib import ExitStack
    from exactcut import editor_resources
    from exactcut.stage_results import ResourceDeferred
    _, item, _, _ = source
    path=w.folder(item['id'])
    calls=[]
    original=source_frames.media.run_checked
    monkeypatch.setattr(source_frames.media,'run_checked',lambda *a,**kw:(calls.append(a),original(*a,**kw))[1])
    def short_wait(kind,actor='admin',*,timeout=1800):
        try:return editor_resources.acquire(kind,actor)
        except editor_resources.cutting.ResourceBusy:
            raise editor_resources.cutting.CutError('等待处理资源超时，请重试',503)
    monkeypatch.setattr(editor_resources,'wait',short_wait)
    with ExitStack() as held:
        for _ in range(editor_resources.settings()['media_tasks']):held.enter_context(editor_resources.acquire('media'))
        with pytest.raises(ResourceDeferred) as error:
            source_frames.prepare(path,draft_with('EP1'),'EP1')
        assert 0 < error.value.not_before <= time.time()+2
        assert calls==[] and not list((path/'source-picker').glob('*/index.json'))
        video=source_frames._video('101573','EP1')
        key=source_frames.json_sha256({'version':1,'drama':'101573','episode':'EP1',
                                       'signature':source_frames._fingerprint(video)})
        with source_frames.resource_locks.hold('source-index',key,timeout=0):
            pass  # the waiter must not leave the identity lock held while media is busy
    result=source_frames.prepare(path,draft_with('EP1'),'EP1')
    assert result['times_ms'] and result['duration_ms']>0
    assert len(calls)==1
    w.update(path, 'queued', '等待制作')
    with pytest.raises(ValueError):
        source_frames.prepare(path, draft_with('EP1'), 'EP1')


def test_index_identity_lock_timeout_defers_instead_of_source_error(source, monkeypatch):
    import threading, time
    from exactcut import resource_locks
    from exactcut.stage_results import ResourceDeferred
    _, item, _, video = source
    path=w.folder(item['id'])
    key=source_frames.json_sha256({'version':1,'drama':'101573','episode':'EP1',
                                   'signature':source_frames._fingerprint(video)})
    original=resource_locks.hold
    monkeypatch.setattr(resource_locks,'hold',lambda ns,ident,*,timeout=180:original(ns,ident,timeout=.05))
    outcome=[]
    def run():
        try:source_frames.prepare(path,draft_with('EP1'),'EP1')
        except BaseException as exc:outcome.append(exc)
    with original('source-index',key):
        thread=threading.Thread(target=run);thread.start();thread.join(5)
    assert len(outcome)==1 and isinstance(outcome[0],ResourceDeferred),outcome
    assert not list((path/'source-picker').glob('*/index.json'))
    assert source_frames.prepare(path,draft_with('EP1'),'EP1')['times_ms']


def test_same_source_index_is_probed_once_across_projects(source, monkeypatch):
    import threading
    _, item, _, video = source
    first=w.folder(item['id']);second=w.folder(w.create_blank()['id'])
    entered,release=threading.Event(),threading.Event();calls=[]
    original=source_frames.media.run_checked
    def probe(*a,**kw):
        calls.append(a);entered.set();assert release.wait(10);return original(*a,**kw)
    monkeypatch.setattr(source_frames.media,'run_checked',probe)
    result=[]
    thread=threading.Thread(target=lambda:result.append(source_frames.prepare(first,draft_with('EP1'),'EP1')))
    thread.start();assert entered.wait(5)
    waiter=threading.Thread(target=lambda:result.append(source_frames.prepare(second,draft_with('EP1'),'EP1')))
    waiter.start()
    try:time.sleep(.2);assert waiter.is_alive(),'same-key waiter must wait for the holder'
    finally:release.set()
    thread.join(10);waiter.join(10)
    assert len(calls)==1 and len(result)==2
    assert result[0]==result[1] and result[0]['key']
    assert list((second/'source-picker'/result[0]['key']).glob('index.json'))


def test_bad_source_still_fails_without_publishing_index(source):
    _, item, _, video = source
    video.write_bytes(b'not a video')
    path=w.folder(item['id'])
    with pytest.raises(ValueError,match='帧索引暂不可用'):
        source_frames.prepare(path,draft_with('EP1'),'EP1')
    assert not list((path/'source-picker').glob('*/index.json'))
    with pytest.raises(ValueError,match='帧索引暂不可用'):
        source_frames.prepare(path,draft_with('EP1'),'EP1')

def test_visual_picker_script_and_media_endpoints_are_unavailable(source):
    app, item, headers, _ = source
    assert request(app, 'GET', '/home/source-picker.js', headers=headers)[0] == 404
    endpoint = f"/home/api/batches/{item['id']}/source-picker"
    assert request(app, 'POST', endpoint,
                   {'draft': draft_with('EP1'), 'episode': 'EP1'}, headers)[0] == 404
    meta = source_frames.prepare(w.folder(item['id']), draft_with('EP1'), 'EP1')
    for suffix in ('video', 'frame/1'):
        assert request(app, 'GET', endpoint + f"/{meta['key']}/{suffix}", headers=headers)[0] == 404
