import pytest

from exactcut import preview_subtitles as subtitles, material_versions as media, workflow as w
from exactcut.util import file_sha256, write_json_atomic
from exactcut.media import ffprobe
from exactcut import preview_subtitle_model as model
from test_preview_subtitles import configured, dispatcher, wait, CUE, ready, fixture

pytestmark = pytest.mark.integration


def test_recognition_reads_verified_lightweight_preview_and_keeps_master_binding(configured, monkeypatch, dispatcher):
    bid, rid, sha, master = configured
    media.enroll(w.folder(bid))
    state = media.read(w.folder(bid))
    vid = state['assets'][rid]['current_version']
    preview = master.parent/'small-preview.mp4'
    preview.write_bytes(b'lightweight-preview')
    version = state['versions'][vid]
    version.update(preview_path=str(preview))
    version['record']['preview'] = {'sha256': file_sha256(preview), 'master_sha256': sha}
    write_json_atomic(w.folder(bid)/'media-state.json',state)
    seen = []
    def recognize(source, *args):
        seen.append(source.read_bytes())
        return [CUE]
    monkeypatch.setattr(subtitles.model, 'recognize', recognize)
    # ffprobe sees copied preview; fixture preview bytes are intentionally not a media file.
    monkeypatch.setattr(subtitles, 'ffprobe', lambda _: {'format': {'duration': '2'}})
    subtitles.start(bid,rid,sha,vid,'admin'); dispatcher()
    result = wait(bid,rid,sha,vid)
    assert result['status'] == 'completed'
    assert seen == [b'lightweight-preview']
    assert result['binding']['source_sha256'] == sha
    assert file_sha256(master) == sha


def test_real_proxy_never_upscales_and_matches_model_sampling(ready, tmp_path):
    _, _, _, source = ready
    cfg = subtitles.settings()
    output = tmp_path/'proxy.mp4'
    model.prepare_chunk(source,output,0,1000,cfg)
    a = next(s for s in ffprobe(source)['streams'] if s['codec_type']=='video')
    b = next(s for s in ffprobe(output)['streams'] if s['codec_type']=='video')
    assert b['width'] <= a['width'] and b['height'] <= a['height']
    assert b['avg_frame_rate'] == f"{cfg['sample_fps']}/1"


def test_corrupt_lightweight_preview_never_reaches_provider(configured, monkeypatch, dispatcher):
    bid, rid, sha, master = configured
    media.enroll(w.folder(bid))
    state = media.read(w.folder(bid)); vid = state['assets'][rid]['current_version']
    preview = master.parent/'small-preview.mp4'; preview.write_bytes(b'corrupt')
    state['versions'][vid]['preview_path'] = str(preview)
    state['versions'][vid]['record']['preview'] = {'sha256': 'a'*64, 'master_sha256':sha}
    write_json_atomic(w.folder(bid)/'media-state.json',state)
    monkeypatch.setattr(subtitles.model, 'recognize', lambda *a: pytest.fail('corrupt preview sent'))
    subtitles.start(bid,rid,sha,vid,'admin'); dispatcher()
    result = wait(bid,rid,sha,vid)
    assert result['status'] == 'failed' and '校验' in result['error']
