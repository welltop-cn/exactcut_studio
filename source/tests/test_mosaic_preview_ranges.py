"""Long fixed and separated tracked intervals retain their existing preview range."""
from exactcut import cutting,workflow,mosaic_session as sessions,mosaic_render as render,mosaic_tracking as tracking
from exactcut.util import read_json,write_json_atomic,file_sha256
from test_cutting import make_video,frame_numbers
from test_cutting_api import ready
from test_phase2_preview_api import fixture
from test_mosaic_interactive_admission import enable_models,terminal
from test_mosaic_render import make_track


def long_session(ready,tmp_path,monkeypatch):
    bid,rid,_,source=ready;enable_models(tmp_path,monkeypatch)
    make_video(source,frames=600);sha=file_sha256(source)
    path=workflow.folder(bid)/'production/results.json';records=read_json(path)
    records['requests'][rid].update(output_sha256=sha,duration_ms=20000);write_json_atomic(path,records)
    return bid,sessions.open_session(bid,rid,sha),source,sha


def test_fixed_twenty_second_preview_is_complete_while_final_resource_is_busy(ready,tmp_path,monkeypatch):
    bid,session,source,sha=long_session(ready,tmp_path,monkeypatch)
    monkeypatch.setattr(tracking,'run_tracker',lambda *a:(_ for _ in ()).throw(AssertionError('fixed must not invoke SAM')))
    target={'start':0,'end':600,'seed_frame':0,'x':5,'y':4,'w':20,'h':10,'motion':'fixed'}
    with cutting.lock_file(cutting.lock_root()/'compute.lock'):
        job=render.start(bid,session['id'],[target],'',effect={'style':'solid'},preview=True)
        path=sessions.session_path(bid,session['id'])/f"render-{job['id']}.json";terminal(path)
        assert read_json(path)['status']=='completed'
        output=path.parent/path.stem/'mosaic.mp4';meta=cutting.probe_source(output)
        assert meta['frame_count']==600 and meta['has_audio']
        assert frame_numbers(output)==[n&127 for n in range(600)]
    assert file_sha256(source)==sha


def test_separated_tracked_intervals_preserve_full_late_envelope(ready,tmp_path,monkeypatch):
    bid,session,source,sha=long_session(ready,tmp_path,monkeypatch)
    monkeypatch.setattr(tracking,'run_tracker',lambda src,directory,meta,target,cfg:make_track(directory,target,meta)['result'])
    targets=[{'start':start,'end':start+10,'seed_frame':start,'x':5,'y':4,'w':20,'h':10} for start in (10,500)]
    job=render.start(bid,session['id'],targets,'',effect={'style':'solid'},preview=True)
    path=sessions.session_path(bid,session['id'])/f"render-{job['id']}.json";terminal(path)
    assert read_json(path)['status']=='completed'
    output=path.parent/path.stem/'mosaic.mp4';meta=cutting.probe_source(output)
    assert meta['frame_count']==500 and meta['has_audio']
    assert frame_numbers(output)==[n&127 for n in range(10,510)]
    assert file_sha256(source)==sha

from interactive_test_support import interactive_dispatcher
