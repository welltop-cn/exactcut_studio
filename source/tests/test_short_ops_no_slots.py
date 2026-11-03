"""Short media work (metadata probe, single frame, stream copy, PCM, mux) never waits on
execution slots; long CPU work (full encodes, -show_frames) still does."""
from contextlib import ExitStack
import subprocess,threading
import pytest
from exactcut import cutting,editor_resources,media
from exactcut.util import write_json_atomic


@pytest.fixture
def slots(tmp_path,monkeypatch):
    """Every execution slot (media, frame, final_media) is held by other work."""
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    cfg=tmp_path/'runtime.json';write_json_atomic(cfg,{'environment':'test'});monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(cfg))
    with ExitStack() as stack:
        for kind in ('media','frame','final_media'):
            for _ in range(editor_resources.settings()[editor_resources._ROLES[kind][0]]):
                stack.enter_context(editor_resources.acquire(kind))
        yield tmp_path,stack


@pytest.fixture
def held(slots):
    root,stack=slots;video=root/'source.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc=size=64x64:rate=10:duration=1','-f','lavfi','-i','sine=duration=1',
                    '-shortest','-c:v','libx264','-c:a','aac','-y',str(video)],check=True)
    return root,video,stack


def finishes(function,seconds=10):
    thread=threading.Thread(target=function,daemon=True);thread.start();thread.join(seconds)
    return not thread.is_alive()


def waits(function,stack,seconds=.6):
    """Blocked while every slot is held, then completes once they are released."""
    done=threading.Event()
    def body():
        function();done.set()
    threading.Thread(target=body,daemon=True).start()
    blocked=not done.wait(seconds)
    stack.close()
    return blocked and done.wait(10)


@pytest.mark.integration
def test_metadata_probe_and_light_ffmpeg_run_while_every_slot_is_held(held):
    root,video,_=held
    assert finishes(lambda:media.ffprobe(video))
    assert finishes(lambda:media.run_checked(['ffmpeg','-y','-v','error','-ss','0.1','-i',str(video),'-frames:v','1',str(root/'a.jpg')],light=True))
    assert finishes(lambda:cutting.run(['ffmpeg','-y','-v','error','-i',str(video),'-map','0:v:0','-c:v','copy','-an',str(root/'copy.mp4')],light=True))
    def final_mux():
        with editor_resources.final_group():
            cutting.run(['ffmpeg','-y','-v','error','-i',str(video),'-vn','-f','s16le',str(root/'a.pcm')],light=True)
    assert finishes(final_mux)
    def frame_window():
        with cutting.frame_lease(root/'window.lock'):
            cutting.run(['ffmpeg','-y','-v','error','-i',str(video),'-frames:v','4',str(root/'w%d.jpg')],60,light=True)
    assert finishes(frame_window) and (root/'w1.jpg').is_file()
    assert all((root/name).is_file() for name in ('a.jpg','copy.mp4','a.pcm'))


@pytest.mark.integration
def test_full_frame_probe_still_waits_for_media(held):
    _,video,stack=held
    assert waits(lambda:media.run_checked(['ffprobe','-v','error','-show_frames','-of','json',str(video)]),stack)


@pytest.mark.integration
def test_encode_still_waits_in_both_runners(held):
    root,video,stack=held
    encode=['ffmpeg','-y','-v','error','-i',str(video),'-c:v','libx264','-an']
    def both():
        media.run_checked(encode+[str(root/'a.mp4')]);cutting.run(encode+[str(root/'b.mp4')])
    assert waits(both,stack)


def test_frame_lease_keeps_cache_dedup_without_a_frame_slot(slots):
    root,_=slots
    lock=root/'frame-cache.lock'
    with cutting.frame_lease(lock):
        with pytest.raises(cutting.ResourceBusy):
            with cutting.frame_lease(lock,timeout=.2):pytest.fail('one cache window is generated once')
