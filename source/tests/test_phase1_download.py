import sys
import pytest
from exactcut import material_download as dl

def test_hung_download_terminates_group_and_retains_existing_video(tmp_path):
    import time
    source=tmp_path/'source'; source.mkdir(); media=source/'EP1.mp4'; media.write_bytes(b'existing')
    with (tmp_path/'log').open('wb') as log:
        started=time.monotonic()
        with pytest.raises(TimeoutError, match='无文件进展'):
            dl.run_download([sys.executable, '-c', 'import time; time.sleep(60)'], source, log, idle_timeout=.01)
        assert time.monotonic()-started < 10
    assert media.read_bytes() == b'existing'


def test_mirrored_video_has_independent_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, 'validate_downloaded_video', lambda video: None)
    source=tmp_path/'source'; source.mkdir(); video=source/'EP1.mp4';video.write_bytes(b'original')
    project=tmp_path/'project';dl.mirror_videos(project,source)
    copy=project/'source/videos/EP1.mp4'
    assert copy.stat().st_ino != video.stat().st_ino
    video.write_bytes(b'modified')
    assert copy.read_bytes()==b'original'


def test_truncated_fast_start_mp4_is_not_copied_and_can_be_retried(tmp_path):
    import shutil
    if not shutil.which('ffmpeg'):pytest.skip('requires ffmpeg')
    from exactcut.media import run_checked,ffprobe
    source=tmp_path/'source';source.mkdir();video=source/'EP1.mp4'
    run_checked(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=s=108x192:r=30',
                 '-t','3','-c:v','libx264','-preset','ultrafast','-movflags','+faststart',str(video)])
    complete=video.read_bytes();video.write_bytes(complete[:len(complete)//2])
    assert float(ffprobe(video)['format']['duration'])>2  # Metadata alone reports full duration.
    project=tmp_path/'project'
    with pytest.raises(RuntimeError,match='未复制'):dl.mirror_videos(project,source)
    assert not (project/'source/videos/EP1.mp4').exists()
    damaged = complete[:len(complete)//2]
    (project/'source/videos/EP1.mp4').write_bytes(damaged)
    video.write_bytes(complete)
    assert dl.mirror_videos(project,source)==['EP1.mp4']
    retained=list((project/'source/videos/quarantine').glob('*.mp4'))
    assert len(retained)==1 and retained[0].read_bytes()==damaged
    assert (project/'source/videos/EP1.mp4').read_bytes()==complete
