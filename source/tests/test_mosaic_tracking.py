"""Tracking request boundaries and full frame coverage; no model downloads in tests."""
import pytest
from exactcut import cutting, mosaic_tracking as tracking

META={'width':160,'height':96,'frame_count':300,'fps_num':30,'fps_den':1}
TARGET={'start':10,'end':40,'seed_frame':25,'x':20,'y':20,'w':40,'h':30}


def test_target_contract():
    assert tracking.validate_target(META,TARGET)==TARGET
    for patch in ({'x':True},{'w':0},{'x':150},{'seed_frame':40},{'end':301},{'start':-1},{'h':1.5},{'end':10}):
        with pytest.raises(cutting.CutError):tracking.validate_target(META,{**TARGET,**patch})


def test_reject_long_clip():
    with pytest.raises(cutting.CutError):tracking.validate_target({**META,'frame_count':1000},{**TARGET,'start':0,'end':601})


def test_result_requires_every_frame_and_marks_missing_mask():
    records=[{'frame':i,'box':[2,3,20,25],'area':400} for i in range(10,40)]
    assert tracking.checked_frames(TARGET,records,160,96)==records
    with pytest.raises(cutting.CutError):tracking.checked_frames(TARGET,records[:-1],160,96)
    with pytest.raises(cutting.CutError):tracking.checked_frames(TARGET,records+[records[0]],160,96)
    records[5]={'frame':15,'box':None,'area':0}
    assert tracking.checked_frames(TARGET,records,160,96)[5]['box'] is None

from test_cutting_scan import configured
from test_cutting_api import call, ready
from test_phase2_preview_api import fixture, serve
from exactcut import mosaic_session as sessions
from test_mosaic_locate import poll


def test_tracking_http_reuses_and_preserves_source(configured,monkeypatch):
    bid,cut_sid,source=configured
    cut=cutting.read_session(bid,cut_sid)
    session=sessions.open_session(bid,cut['request_id'],cut['source_sha256']);sid=session['id']
    original=source.read_bytes();calls=[]
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    def fake(source,directory,meta,target,cfg):
        calls.append(target);directory.mkdir(exist_ok=True)
        (directory/'preview.mp4').write_bytes(b'fixture-preview')
        return {'width':160,'height':96,'frames':[],'warnings':[],'elapsed_seconds':1}
    monkeypatch.setattr(tracking,'run_tracker',fake)
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}'
    try:
        body={'target':TARGET,'reset_at':''}
        assert call(app,'POST',base+'/track',body,auth=False)[0]==401
        assert call(app,'POST',base+'/track',body,csrf=False)[0]==403
        assert call(app,'POST',base+'/track',{**body,'target':{**TARGET,'w':99999}})[0]==400
        code,job=call(app,'POST',base+'/track',body);assert code==202
        assert poll(tracking,bid,sid,job['id'])['status']=='completed'
        assert call(app,'POST',base+'/track',body)[1]['id']==job['id']
        assert len(calls)==1 and source.read_bytes()==original
        from exactcut.util import write_json_atomic
        cached=tracking.state(bid,sid,job['id']);cached['result']['frames']=[{'box':None}]
        write_json_atomic(sessions.session_path(bid,sid)/f"track-{job['id']}.json",cached)
        tracking.start(bid,sid,TARGET,'');assert poll(tracking,bid,sid,job['id'])['status']=='completed'
        assert len(calls)==2
        assert tracking.preview_file(bid,sid,job['id']).read_bytes()==b'fixture-preview'
        assert cutting.read_session(bid,cut_sid)['jobs']==[]
    finally:app.shutdown();app.server_close()


def test_failure_is_safe_and_reset_invalidates_job(configured,monkeypatch):
    from exactcut.util import write_json_atomic
    bid,cut_sid,_=configured;cut=cutting.read_session(bid,cut_sid)
    session=sessions.open_session(bid,cut['request_id'],cut['source_sha256']);sid=session['id']
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    monkeypatch.setattr(tracking,'run_tracker',lambda *args:(_ for _ in ()).throw(RuntimeError('private-secret')))
    job=tracking.start(bid,sid,TARGET,'');result=poll(tracking,bid,sid,job['id'])
    assert result['status']=='failed' and 'private-secret' not in result['error']
    path=sessions.session_path(bid,sid)/f"track-{job['id']}.json"
    result['status']='running';write_json_atomic(path,result)
    assert '跟踪已中断' in tracking.state(bid,sid,job['id'])['error']
    session['reset_at']='reset';write_json_atomic(path.parent/'session.json',session)
    with pytest.raises(cutting.CutError) as error:tracking.state(bid,sid,job['id'])
    assert error.value.status==409
    monkeypatch.setattr(tracking,'enabled',lambda:False)
    with pytest.raises(cutting.CutError) as error:tracking.start(bid,sid,TARGET,'reset')
    assert error.value.status==503


def test_nonfinite_model_output_is_not_reported_as_empty_target():
    import numpy as np
    from exactcut.mosaic_sam2 import binary_mask
    assert binary_mask(np.array([[-1,0,1]],dtype=float)).tolist()==[[0,0,255]]
    for invalid in (float('nan'),float('inf'),-float('inf')):
        with pytest.raises(ValueError,match='non-finite'):binary_mask(np.array([[invalid]]))


def test_inference_size_override_is_bounded_and_part_of_cache_identity():
    from exactcut import mosaic_sam2, mosaic_tracking
    assert mosaic_sam2.model_overrides({})==[]
    assert mosaic_sam2.model_overrides({'inference_size':512})==['++model.image_size=512']
    for value in (0,513,'512',True):
        with pytest.raises(ValueError):mosaic_sam2.model_overrides({'inference_size':value})
    session={'source_sha256':'a'*64,'reset_at':''}
    assert mosaic_tracking.identity(session,{}, {'inference_size':512})!=mosaic_tracking.identity(session,{}, {'inference_size':1024})
