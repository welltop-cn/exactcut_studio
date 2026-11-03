"""A final admission cannot revoke an already admitted read-only model task."""
from contextlib import ExitStack
import threading
import pytest
from exactcut import editor_resources, cutting,cutting_scan,mosaic_scan,mosaic_locate,mosaic_tracking as tracking,mosaic_render as render,mosaic_session as sessions
from exactcut.util import read_json,file_sha256
from test_cutting_api import ready
from test_phase2_preview_api import fixture
from test_mosaic_interactive_admission import enable_models,terminal
from test_mosaic_render import make_track


@pytest.mark.integration
@pytest.mark.parametrize('operation',['scan','locate','preview'])
def test_pre_admitted_read_task_finishes_after_same_asset_final_is_queued(ready,tmp_path,monkeypatch,operation):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch);session=sessions.open_session(bid,rid,sha)
    entered=threading.Event();finish=threading.Event();path=None
    def wait():entered.set();assert finish.wait(8)
    def scan(src,meta,cfg,policy,key,progress,**kwargs):
        wait();return {'risk_events':[],'warnings':[]}
    def locate(*args,**kwargs):wait();return []
    def infer(src,directory,meta,target,cfg):wait();return make_track(directory,target,meta)['result']
    monkeypatch.setattr(cutting_scan,'detect',scan);monkeypatch.setattr(mosaic_locate,'detect',locate);monkeypatch.setattr(tracking,'run_tracker',infer)
    target={'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20}
    try:
        if operation=='scan':job=mosaic_scan.start(bid,session['id'],'');filename='scan.json'
        elif operation=='locate':
            job=mosaic_locate.start(bid,session['id'],0,6,0,'');filename=f"locate-{job['id']}.json"
        else:
            job=render.start(bid,session['id'],[target],'',preview=True);filename=f"render-{job['id']}.json"
        path=sessions.session_path(bid,session['id'])/filename
        assert entered.wait(3)
        final=render.start(bid,session['id'],[],'',ranges=[[20,25]])
        assert final['status']=='queued'
        # This permit is only for the existing worker, never for a new request.
        with pytest.raises(cutting.CutError) as denied:mosaic_scan.start(bid,session['id'],'',force=True)
        assert denied.value.status==409
        with pytest.raises(cutting.CutError):mosaic_locate.start(bid,session['id'],6,12,6,'')
        with pytest.raises(cutting.CutError):render.start(bid,session['id'],[target],'',preview=True)
        finish.set();terminal(path)
        assert read_json(path)['status']=='completed'
        assert file_sha256(source)==sha
    finally:
        finish.set()
        if path:terminal(path)


@pytest.mark.integration
def test_worker_infrastructure_failure_is_not_successful_unmasked_preview(ready,tmp_path,monkeypatch):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch);session=sessions.open_session(bid,rid,sha)
    def broken(*args,**kwargs):raise cutting.CutError('SAM worker infrastructure failure',503)
    monkeypatch.setattr(tracking,'run_tracker',broken)
    job=render.start(bid,session['id'],[{'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20}],'',preview=True)
    path=sessions.session_path(bid,session['id'])/f"render-{job['id']}.json";terminal(path)
    assert read_json(path)['status']=='failed'
    assert 'result' not in read_json(path)
    assert file_sha256(source)==sha


@pytest.mark.integration
def test_legacy_single_track_stop_releases_owned_preview_budget(ready,tmp_path,monkeypatch):
    from test_cutting_api import call
    from test_phase2_preview_api import serve
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch);session=sessions.open_session(bid,rid,sha)
    entered=threading.Event();finish=threading.Event();path=None;app=serve()
    def infer(*args,**kwargs):
        from exactcut import processing_cancel
        entered.set()
        while not finish.wait(.01):processing_cancel.check()
        raise AssertionError('test tracker must be cancelled')
    monkeypatch.setattr(tracking,'run_tracker',infer)
    try:
        target={'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20}
        job=tracking.start(bid,session['id'],target,'');path=sessions.session_path(bid,session['id'])/f"track-{job['id']}.json"
        assert entered.wait(2)
        code,stopped=call(app,'POST',f"/api/batches/{bid}/mosaics/{session['id']}/track/{job['id']}/stop",{'reset_at':''})
        assert code==202
        terminal(path)
        assert read_json(path)['status']=='cancelled'
        assert file_sha256(source)==sha
    finally:
        finish.set()
        if path:terminal(path)
        app.shutdown();app.server_close()


@pytest.mark.integration
def test_reset_cancels_pre_admitted_legacy_track(ready,tmp_path,monkeypatch):
    from exactcut import processing_cancel
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch);session=sessions.open_session(bid,rid,sha)
    entered=threading.Event();finish=threading.Event();path=None
    def infer(*args,**kwargs):
        entered.set()
        while not finish.wait(.01):processing_cancel.check()
        raise AssertionError('expected reset cancellation')
    monkeypatch.setattr(tracking,'run_tracker',infer)
    try:
        job=tracking.start(bid,session['id'],{'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20},'')
        path=sessions.session_path(bid,session['id'])/f"track-{job['id']}.json"
        assert entered.wait(2)
        reset=sessions.discard(bid,session['id'],'')
        assert reset['reset_at']
        assert read_json(path).get('cancel_requested') is True
        terminal(path);assert read_json(path)['status']=='cancelled'
    finally:
        finish.set()
        if path:terminal(path)


@pytest.mark.parametrize('module',[mosaic_scan,mosaic_locate,tracking,render,cutting_scan])
def test_cancel_intent_survives_interactive_worker_loss(tmp_path,module):
    from exactcut.util import write_json_atomic
    path=tmp_path/'job.json'
    write_json_atomic(path,{'id':'a'*32,'status':'running','cancel_requested':True})
    assert module.recover(path)['status']=='cancelled'


@pytest.mark.integration
def test_legacy_cut_preparation_is_independent_of_final_compute(ready):
    from exactcut import cutting
    bid,rid,sha,source=ready
    with ExitStack() as stack:
        for _ in range(editor_resources.settings()['final_media_tasks']):
            stack.enter_context(editor_resources.acquire('final_media'))
        session=cutting.open_session(bid,rid,sha)
        assert session['metadata']['frame_count']==90
        assert session['source_sha256']==sha


@pytest.mark.integration
def test_legacy_cut_scan_does_not_wait_for_final_compute(ready,tmp_path,monkeypatch):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch);session=cutting.open_session(bid,rid,sha)
    def scan(src,meta,cfg,policy,key,progress,**kwargs):
        return {'risk_events':[],'warnings':[]}
    monkeypatch.setattr(cutting_scan,'detect',scan)
    with ExitStack() as stack:
        for _ in range(editor_resources.settings()['final_media_tasks']):
            stack.enter_context(editor_resources.acquire('final_media'))
        report=cutting_scan.start(bid,session['id'],'standard')
        path=cutting.session_path(bid,session['id'])/f"scan-{report['id']}.json";terminal(path)
        assert read_json(path)['status']=='completed'

from interactive_test_support import interactive_dispatcher
