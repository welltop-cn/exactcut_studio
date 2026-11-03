"""Completed Hook receipts must track source arrival, replacement and shot indexes."""
import time
from pathlib import Path
import pytest
from exactcut import hook_preview,interactive_queue,workflow as w
from exactcut.util import write_json_atomic
from test_phase2_preview_api import fixture
from test_cutting import make_video

pytestmark = pytest.mark.integration

@pytest.fixture
def hook_input(fixture):
    batch=w.create_blank();draft=batch['draft'];draft.update(drama_id='101573',author='source-bound-hook')
    draft['rows'][0].update(hook_text='Source-bound preview',hook_options={'mode':'none','position':'top'},clips=[{'episode':'1','start_m':'00','start_s':'00','start_ms':'000','end_m':'00','end_s':'01','end_ms':'000'}])
    saved=w.save_draft(batch['id'],{'revision':batch['revision'],'draft':draft});assert not saved['errors']
    latest,manifest,request=hook_preview.current_request(batch['id'],saved['draft']['rows'][0]['request_id'])
    project=w.ROOT/'projects'/manifest.drama_id
    return latest,request,project


def finish(batch,request,dispatcher):
    job=hook_preview.submit(batch['id'],batch,0,request,'admin');deadline=time.monotonic()+12
    while job['status'] in {'queued','running'}:
        assert time.monotonic()<deadline;time.sleep(.02);job=hook_preview.state(batch['id'],job['id'])
    assert job['status']=='completed',job
    assert job['input_current']
    assert hook_preview.submit(batch['id'],batch,0,request,'admin')['id']==job['id']
    return job


def test_completed_placeholder_is_not_reused_after_source_arrives(hook_input):
    batch,request,project=hook_input;dispatcher=interactive_queue.Dispatcher(poll_seconds=.01);assert dispatcher.start()
    try:
        first=finish(batch,request,dispatcher)
        assert not first['result']['has_frame'] and not first['result']['sources_complete']
        video=project/'source/videos/EP1.mp4';video.parent.mkdir(parents=True,exist_ok=True);make_video(video)
        receipt=hook_preview.submit(batch['id'],batch,0,request,'admin');assert receipt['id']!=first['id']
        assert hook_preview.state(batch['id'],first['id'])['input_current'] is False
        second=finish(batch,request,dispatcher)
        assert second['id']!=first['id'] and second['result']['has_frame'] and second['result']['sources_complete']
        assert second['result']['preview_key']!=first['result']['preview_key']
        assert w.detail(batch['id'])['revision']==batch['revision']
    finally:dispatcher.stop(timeout=None)


@pytest.mark.parametrize('changed',['source','shot_index'])
def test_completed_receipt_invalidates_when_source_or_index_identity_changes(hook_input,changed):
    batch,request,project=hook_input;video=project/'source/videos/EP1.mp4';video.parent.mkdir(parents=True,exist_ok=True);make_video(video)
    if changed=='shot_index':write_json_atomic(project/'data/episodes/EP1.video.json',{'shots':[],'revision':'initial'})
    dispatcher=interactive_queue.Dispatcher(poll_seconds=.01);assert dispatcher.start()
    try:
        first=finish(batch,request,dispatcher);assert first['result']['has_frame']
        if changed=='source':
            # A real new encoded container, keeping the valid video stream, changes source identity.
            import subprocess
            replacement=video.with_name('replacement.mp4')
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(video),'-c','copy','-metadata','comment=replacement',str(replacement)],check=True)
            replacement.replace(video)
        else:
            index=project/'data/episodes/EP1.video.json';write_json_atomic(index,{'shots':[],'revision':'updated'})
        receipt=hook_preview.submit(batch['id'],batch,0,request,'admin');assert receipt['id']!=first['id']
        assert hook_preview.state(batch['id'],first['id'])['input_current'] is False
        second=finish(batch,request,dispatcher)
        assert second['id']!=first['id'] and second['result']['preview_key']!=first['result']['preview_key']
        assert w.detail(batch['id'])['revision']==batch['revision']
    finally:dispatcher.stop(timeout=None)


def test_source_arrival_during_compute_cannot_publish_placeholder(hook_input,monkeypatch):
    import threading
    batch,request,project=hook_input;computed=threading.Event();release=threading.Event();keys=[]
    real_compute=hook_preview.compute
    def paused(*args):
        result=real_compute(*args);keys.append(result[0]['preview_key']);computed.set();assert release.wait(5);return result
    monkeypatch.setattr(hook_preview,'compute',paused)
    dispatcher=interactive_queue.Dispatcher(poll_seconds=.01);assert dispatcher.start()
    try:
        job=hook_preview.submit(batch['id'],batch,0,request,'admin');assert computed.wait(5)
        video=project/'source/videos/EP1.mp4';video.parent.mkdir(parents=True,exist_ok=True);make_video(video);release.set()
        deadline=time.monotonic()+5
        while job['status'] in {'queued','running'}:
            assert time.monotonic()<deadline;time.sleep(.02);job=hook_preview.state(batch['id'],job['id'])
        assert job['status']=='superseded' and not job.get('result')
        assert not (project/'cache/hook-previews'/(keys[0]+'.json')).exists()
    finally:release.set();dispatcher.stop(timeout=None)
