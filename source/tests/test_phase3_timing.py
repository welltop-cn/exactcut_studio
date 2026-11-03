import subprocess
import time
import pytest
from exactcut import timing_review as review
from exactcut.intake import timing_explanation
from test_phase2_timing import manifest

pytestmark = pytest.mark.integration


def video(tmp_path,seconds=4,color='navy'):
    path=tmp_path/'source/videos/EP1.mp4';path.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'color={color}:s=90x160:r=25:d={seconds}',
                    '-c:v','libx264','-preset','ultrafast',str(path)],check=True)
    return path


def test_whole_and_to_end_update_from_local_video_without_changing_draft(tmp_path):
    video(tmp_path)
    item=manifest([dict(type='episode',episode='EP1',start_ms=0,full_episode=True),
                   dict(type='episode',episode='EP1',start_ms=1000,to_end=True)])
    result=timing_explanation(item,tmp_path)[0]
    assert result['planned_duration_ms']==7000 and result['pending_endings']==0
    assert [s['end_ms'] for s in result['segments']]==[4000,4000]
    assert all(s['source_status']=='ready' for s in result['segments'])
    assert item.requests[0].timeline[0].end_ms is None
    assert item.requests[0].timeline[1].to_end


def test_replaced_source_invalidates_duration_cache_and_out_of_range_is_visible(tmp_path):
    video(tmp_path)
    item=manifest([dict(type='episode',episode='EP1',start_ms=3000,to_end=True)])
    assert timing_explanation(item,tmp_path)[0]['planned_duration_ms']==1000
    video(tmp_path,seconds=2,color='maroon')
    result=timing_explanation(item,tmp_path)[0]
    assert result['planned_duration_ms'] is None
    assert result['segments'][0]['error'] and result['segments'][0]['source_status']=='invalid'
    explicit=manifest([dict(type='episode',episode='EP1',start_ms=0,end_ms=3500)])
    assert timing_explanation(explicit,tmp_path)[0]['segments'][0]['error']


def test_missing_source_stays_pending_and_probe_does_not_block_polling(tmp_path,monkeypatch):
    item=manifest([dict(type='episode',episode='EP1',start_ms=0,full_episode=True)])
    assert timing_explanation(item,tmp_path)[0]['segments'][0]['source_status']=='missing'
    path=tmp_path/'source/videos/EP1.mp4';path.parent.mkdir(parents=True);path.write_bytes(b'fixture')
    def slow(*args):time.sleep(.2);return 5000
    monkeypatch.setattr(review,'_duration',slow)
    started=time.monotonic()
    result=timing_explanation(item,tmp_path,wait=False)[0]
    assert time.monotonic()-started<.1 and result['segments'][0]['source_status']=='checking'
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        result=timing_explanation(item,tmp_path,wait=False)[0]
        if not result['pending_endings']:break
        time.sleep(.02)
    assert result['planned_duration_ms']==5000


def test_corrupt_video_is_reported_without_inventing_endpoint(tmp_path):
    path=tmp_path/'source/videos/EP1.mp4';path.parent.mkdir(parents=True);path.write_bytes(b'corrupt')
    item=manifest([dict(type='episode',episode='EP1',start_ms=0,full_episode=True)])
    result=timing_explanation(item,tmp_path)[0]
    assert result['planned_duration_ms'] is None
    assert result['segments'][0]['source_status']=='invalid'
