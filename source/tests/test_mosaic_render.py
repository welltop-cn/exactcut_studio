"""Full-length multi-target composition: real FFmpeg, synthetic masks."""
from pathlib import Path
import pytest
from PIL import Image
from exactcut import cutting, mosaic_render as render
from test_cutting import make_video, frame_numbers
from test_cutting_api import ready, call
from test_phase2_preview_api import fixture, serve
from exactcut import mosaic_session as sessions, mosaic_tracking as tracking
from exactcut.util import file_sha256, write_json_atomic
import time


def make_track(directory,target,meta,empty=()):
    (directory/'masks').mkdir(parents=True,exist_ok=True)
    frames=[]
    for frame in range(target['start'],target['end']):
        mask=Image.new('L',(meta['width'],meta['height']))
        box=[target['x'],target['y'],target['x']+target['w'],target['y']+target['h']]
        if frame not in empty:mask.paste(255,box)
        mask.save(directory/'masks'/f"{frame-target['start']:06d}.png")
        frames.append({'frame':frame,'box':None if frame in empty else box,'area':0 if frame in empty else target['w']*target['h']})
    result={'width':meta['width'],'height':meta['height'],'frames':frames,'warnings':[],'elapsed_seconds':0}
    write_json_atomic(directory/'result.json',result)
    return {'target':target,'directory':directory,'result':result}


def test_mask_union_and_failure_never_erase_another_target(tmp_path):
    meta={'width':8,'height':6}
    t={'start':1,'end':4,'seed_frame':2,'x':0,'y':0,'w':2,'h':2}
    a=make_track(tmp_path/'a',t,meta,empty=(3,))
    b=make_track(tmp_path/'b',{**t,'x':6},meta)
    assert render.union_mask(0,[a,b],(8,6)).getbbox() is None
    mask=render.union_mask(2,[a,b],(8,6))
    assert mask.getpixel((0,0))==mask.getpixel((7,0))==255
    assert mask.getpixel((4,0))==0
    assert render.union_mask(3,[a,b],(8,6)).getextrema()==(255,255)
    (tmp_path/'a/masks/000001.png').unlink()
    with pytest.raises(cutting.CutError):render.union_mask(2,[a,b],(8,6))


def test_full_video_keeps_frames_rate_size_audio_and_original(tmp_path):
    source=make_video(tmp_path/'source.mp4',frames=30,fps='30000/1001')
    before=file_sha256(source);meta=cutting.probe_source(source)
    target={'start':10,'end':20,'seed_frame':15,'x':0,'y':0,'w':60,'h':64}
    tracks=[make_track(tmp_path/'a',target,meta),make_track(tmp_path/'b',{**target,'start':15,'end':25,'seed_frame':20,'x':60,'w':52},meta)]
    output=tmp_path/'complete.mp4';render.compose(source,output,meta,tracks)
    assert cutting.probe_source(output)==meta
    assert file_sha256(source)==before
    actual=frame_numbers(output)
    assert actual[:10]==list(range(10)) and actual[25:]==list(range(25,30))
    audio=lambda path:cutting.run(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-c:a','copy','-f','adts','-'])
    assert audio(source)==audio(output)


def wait_job(bid,sid,jid):
    for _ in range(400):
        job=render.state(bid,sid,jid)
        if job['status']!='running':return job
        time.sleep(.025)
    pytest.fail('render did not finish')


def test_batch_http_restore_download_and_immutable_source(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id'];calls=[]
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    def fake(source,directory,meta,target,cfg):
        calls.append(target)
        return make_track(directory,target,meta,empty=(target['start'],))['result']
    monkeypatch.setattr(tracking,'run_tracker',fake)
    target={'start':10,'end':20,'seed_frame':15,'x':0,'y':0,'w':40,'h':40}
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}/render';body={'targets':[target,{**target,'start':15,'end':25,'seed_frame':20,'x':60}],'reset_at':''}
    try:
        assert call(app,'GET',base)[1]=={'job':None}
        assert call(app,'POST',base,body,auth=False)[0]==401
        assert call(app,'POST',base,body,csrf=False)[0]==403
        assert call(app,'POST',base,{**body,'targets':[]})[0]==400
        assert call(app,'POST',base,{**body,'targets':[target]*9})[0]==400
        code,job=call(app,'POST',base,body);assert code==202
        finished=wait_job(bid,sid,job['id']);assert finished['status']=='completed',finished
        assert finished['result']['fallback_frames']==2
        assert len(calls)==2
        assert call(app,'GET',base)[1]['job']['id']==job['id']
        assert call(app,'POST',base,body)[1]['id']==job['id']
        assert len(calls)==2
        assert call(app,'GET',base+'/'+job['id']+'/video',auth=False)[0]==401
        code,video=call(app,'GET',base+'/'+job['id']+'/video');assert code==200 and b'ftyp' in video[:32]
        assert file_sha256(source)==sha
        session['reset_at']='new';write_json_atomic(sessions.session_path(bid,sid)/'session.json',session)
        assert call(app,'GET',base+'/'+job['id']+'/video')[0]==409
    finally:app.shutdown();app.server_close()
