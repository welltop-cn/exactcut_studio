"""Shared raw-mask cache must serialize only an identical inference key."""
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from exactcut import mosaic_render as render,mosaic_tracking as tracking,mosaic_session as sessions,processing_cancel
from test_cutting_api import ready
from test_phase2_preview_api import fixture
from test_mosaic_interactive_admission import enable_models
from test_mosaic_render import make_track

pytestmark = pytest.mark.integration


def setup(ready,tmp_path,monkeypatch):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch)
    session=sessions.open_session(bid,rid,sha)
    return bid,session,{'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20},tracking.settings()


def test_same_key_produces_once_and_never_publishes_partial_result(ready,tmp_path,monkeypatch):
    bid,session,target,cfg=setup(ready,tmp_path,monkeypatch);root=sessions.session_path(bid,session['id'])
    started=threading.Event();second=threading.Event();finish=threading.Event();calls=[]
    def infer(src,directory,meta,target,cfg):
        calls.append(directory)
        if len(calls)>1:second.set()
        result=make_track(directory,target,meta)['result']
        started.set();assert finish.wait(5)
        return result
    monkeypatch.setattr(tracking,'run_tracker',infer)
    canonical=root/f'inference-{tracking.inference_identity(session,target,cfg)}'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(render.prepare_track,bid,session['id'],session,target,cfg,root/'target-a')
        try:
            assert started.wait(2)
            another=pool.submit(render.prepare_track,bid,session['id'],session,target,cfg,root/'target-b')
            second.wait(.3)
            assert not (canonical/'result.json').exists(),'uncommitted result became visible to another reader'
        finally:finish.set()
        a=first.result(3);b=another.result(3)
    assert len(calls)==1
    assert a['directory']==b['directory']
    assert (a['directory']/'result.json').is_file()


def test_cancelled_producer_keeps_no_partial_canonical_cache(ready,tmp_path,monkeypatch):
    bid,session,target,cfg=setup(ready,tmp_path,monkeypatch);root=sessions.session_path(bid,session['id'])
    def cancelled(src,directory,meta,target,cfg):
        make_track(directory,target,meta)
        raise processing_cancel.Cancelled()
    monkeypatch.setattr(tracking,'run_tracker',cancelled)
    with pytest.raises(processing_cancel.Cancelled):render.prepare_track(bid,session['id'],session,target,cfg,root/'target-a')
    assert not (root/f'inference-{tracking.inference_identity(session,target,cfg)}').exists()
    assert not list(root.glob('inference-*-work-*'))


def test_single_region_and_clip_preview_share_the_same_inference_key(ready,tmp_path,monkeypatch):
    from test_mosaic_interactive_admission import terminal
    bid,session,target,cfg=setup(ready,tmp_path,monkeypatch);root=sessions.session_path(bid,session['id'])
    started=threading.Event();both=threading.Event();finish=threading.Event();calls=[];paths=[]
    def infer(src,directory,meta,target,cfg):
        calls.append(directory)
        if len(calls)>1:both.set()
        result=make_track(directory,target,meta)['result']
        (directory/'preview.mp4').write_bytes(b'old-test-double-preview')
        started.set();assert finish.wait(5)
        return result
    monkeypatch.setattr(tracking,'run_tracker',infer)
    try:
        first=tracking.start(bid,session['id'],target,'',actor='alpha')
        paths.append(root/f"track-{first['id']}.json")
        assert started.wait(2)
        other=render.start(bid,session['id'],[target],'',preview=True,actor='beta')
        paths.append(root/f"render-{other['id']}.json")
        both.wait(.3);finish.set()
        for path in paths:terminal(path)
        assert len(calls)==1
        assert all(__import__('json').loads(path.read_text())['status']=='completed' for path in paths)
    finally:
        finish.set()
        for path in paths:terminal(path)


def test_different_keys_infer_at_the_same_time(ready,tmp_path,monkeypatch):
    bid,session,target,cfg=setup(ready,tmp_path,monkeypatch);root=sessions.session_path(bid,session['id'])
    barrier=threading.Barrier(2)
    def infer(src,directory,meta,target,cfg):
        barrier.wait(timeout=3)
        return make_track(directory,target,meta)['result']
    monkeypatch.setattr(tracking,'run_tracker',infer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(render.prepare_track,bid,session['id'],session,{**target,'x':x},cfg,root/f'target-{x}') for x in (10,40)]
        tracks=[f.result(5) for f in futures]
    assert tracks[0]['directory']!=tracks[1]['directory']

from interactive_test_support import interactive_dispatcher
