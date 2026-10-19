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
    assert render.union_mask(3,[a,b],(8,6)).getbbox()==(6,0,8,2)
    assert render.union_mask(3,[a],(8,6)).getbbox() is None
    failed={'target':t,'failed':True}
    assert render.union_mask(2,[failed,b],(8,6)).getbbox()==(6,0,8,2)
    assert render.union_mask(2,[failed],(8,6)).getbbox() is None
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
        assert call(app,'POST',base,{**body,'ranges':False})[0]==400
        assert call(app,'POST',base,{**body,'targets':[target]*9})[0]==400
        code,job=call(app,'POST',base,body);assert code==202
        finished=wait_job(bid,sid,job['id']);assert finished['status']=='completed',finished
        assert finished['result']['skipped_frames']==2
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


def test_mixed_deletion_and_mosaic_and_discard_retains_discovery(ready,monkeypatch):
    from exactcut import mosaic_scan, cutting_scan, workflow
    from exactcut.util import read_json
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id']
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    monkeypatch.setattr(tracking,'run_tracker',lambda src,directory,meta,target,cfg:make_track(directory,target,meta)['result'])
    target={'start':10,'end':20,'seed_frame':15,'x':80,'y':0,'w':20,'h':20}
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}'
    try:
        code,job=call(app,'POST',base+'/render',{'targets':[target],'ranges':[[0,5],[12,16]],'reset_at':''});assert code==202
        done=wait_job(bid,sid,job['id']);assert done['status']=='completed',done
        assert done['result']['metadata']['frame_count']==session['metadata']['frame_count']-9
        output=render.video_file(bid,sid,job['id'])
        numbers=frame_numbers(output)
        assert numbers[:7]==list(range(5,12)) and numbers[7]==16
        scan={'status':'completed','identity':mosaic_scan.identity(session,cutting_scan.settings()),'source_sha256':sha,'reset_at':'','events':[]}
        write_json_atomic(sessions.session_path(bid,sid)/'scan.json',scan)
        code,reset=call(app,'POST',base+'/discard',{'reset_at':''});assert code==200
        assert reset['reset_at']
        assert call(app,'GET',base+'/render')[1]=={'job':None}
        assert call(app,'GET',base+'/render/'+job['id']+'/video')[0]==409
        assert call(app,'GET',base+'/scan')[1]['report']['status']=='completed'
        assert call(app,'POST',base+'/render',{'targets':[target],'reset_at':''})[0]==409
        assert call(app,'POST',base+'/discard',{'reset_at':''})[0]==409
        assert file_sha256(source)==sha
        code,job2=call(app,'POST',base+'/render',{'targets':[],'ranges':[[5,10]],'reset_at':reset['reset_at']});assert code==202
        done2=wait_job(bid,sid,job2['id']);assert done2['status']=='completed',done2
        assert done2['result']['metadata']['frame_count']==session['metadata']['frame_count']-5
    finally:app.shutdown();app.server_close()


@pytest.mark.parametrize('managed',[False,True])
def test_combined_result_replaces_current_preserves_source_and_reopens_new_round(ready,monkeypatch,managed):
    from exactcut import workflow, material_versions, preview_oss
    bid,rid,sha,source=ready
    if managed:material_versions.enroll(workflow.folder(bid))
    session=sessions.open_session(bid,rid,sha);sid=session['id']
    job=render.start(bid,sid,[],'',[[10,20]]);done=wait_job(bid,sid,job['id'])
    assert done['status']=='completed'
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}'
    try:
        assert call(app,'POST',base+'/replace',{'job_id':job['id'],'reset_at':''},csrf=False)[0]==403
        code,replacement=call(app,'POST',base+'/replace',{'job_id':job['id'],'reset_at':''});assert code==202
        for _ in range(400):
            code,replacement=call(app,'GET',base+'/render/'+job['id'])
            assert code==200,replacement
            if replacement.get('replacement',{}).get('status')!='running':break
            time.sleep(.025)
        assert replacement['replacement']['status']=='completed',replacement
        current,record=workflow.preview_file(bid,rid)
        assert record['output_sha256']==done['result']['sha256']==file_sha256(current)
        assert file_sha256(source)==sha
        assert call(app,'POST',base+'/replace',{'job_id':job['id'],'reset_at':''})[0]==202
        assert call(app,'POST',base+'/discard',{'reset_at':''})[0]==409
        fresh=sessions.open_session(bid,rid,record['output_sha256'])
        assert fresh['id']!=sid and fresh['metadata']['frame_count']==80
        assert render.latest(bid,fresh['id'])=={'job':None}
    finally:app.shutdown();app.server_close()


def test_discard_running_job_fences_result_and_does_not_change_project(ready,monkeypatch):
    import threading
    from exactcut import workflow
    from exactcut.util import read_json
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id']
    entered,finish=threading.Event(),threading.Event()
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    def delayed(src,directory,meta,target,cfg):
        entered.set();assert finish.wait(5)
        return make_track(directory,target,meta)['result']
    monkeypatch.setattr(tracking,'run_tracker',delayed)
    target={'start':10,'end':20,'seed_frame':15,'x':80,'y':0,'w':20,'h':20}
    job=render.start(bid,sid,[target],'')
    try:
        assert entered.wait(2)
        reset=sessions.discard(bid,sid,'')
        assert reset['reset_at']
        assert render.latest(bid,sid)=={'job':None}
    finally:finish.set()
    path=sessions.session_path(bid,sid)/f"render-{job['id']}.json"
    for _ in range(100):
        if read_json(path)['status']!='running':break
        time.sleep(.025)
    assert read_json(path)['status']=='failed'
    from exactcut import mosaic_replace
    with pytest.raises(cutting.CutError):mosaic_replace.start(bid,sid,job['id'],'')
    assert workflow.preview_file(bid,rid)[0]==source and file_sha256(source)==sha


def test_stop_interrupts_owned_process_and_retains_session(ready,monkeypatch):
    import sys,threading
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id'];entered=threading.Event()
    def slow(*args,**kwargs):
        entered.set();cutting.run([sys.executable,'-c','import time;time.sleep(60)'])
        raise AssertionError('must stop before publishing')
    original_process=render.process_video
    monkeypatch.setattr(render,'process_video',slow)
    job=render.start(bid,sid,[],'',[[1,3]])
    assert entered.wait(3)
    app=serve();url=f'/api/batches/{bid}/mosaics/{sid}/stop'
    try:
        assert call(app,'POST',url,{'job_id':job['id'],'reset_at':''},csrf=False)[0]==403
        assert call(app,'POST',url,{'job_id':job['id'],'reset_at':''})[0]==202
        stopped=wait_job(bid,sid,job['id']);assert stopped['status']=='cancelled',stopped
        assert 'result' not in stopped
        assert sessions.read_session(bid,sid).get('reset_at','')==''
        assert file_sha256(source)==sha
        assert render.stop(bid,sid,job['id'],'')['status']=='cancelled'
        with cutting.lock_file(cutting.lock_root()/'compute.lock'):pass
        monkeypatch.setattr(render,'process_video',original_process)
        restarted=render.start(bid,sid,[],'',[[1,3]])
        assert not restarted.get('cancel_requested')
        assert wait_job(bid,sid,restarted['id'])['status']=='completed'
    finally:app.shutdown();app.server_close()


def test_target_effect_contract_is_validated_and_preserved():
    meta={'width':100,'height':100,'frame_count':30,'fps_num':30,'fps_den':1}
    target={'start':0,'end':10,'seed_frame':0,'x':0,'y':0,'w':30,'h':30,'effect':{'style':'soft_blur'}}
    assert render.validate_targets(meta,[target])[0]['effect']['style']=='soft_blur'
    with pytest.raises(cutting.CutError):render.validate_targets(meta,[{**target,'effect':{'style':'wrong'}}])


def test_distinct_clip_effects_are_rendered_in_one_video(tmp_path):
    source=make_video(tmp_path/'source.mp4',frames=12);meta=cutting.probe_source(source)
    target={'start':0,'end':4,'seed_frame':0,'x':0,'y':0,'w':meta['width'],'h':meta['height'],'effect':{'style':'solid','expansion':'none'}}
    other={**target,'start':4,'end':8,'seed_frame':4,'effect':{'style':'blur','expansion':'none'}}
    tracks=[make_track(tmp_path/'a',target,meta),make_track(tmp_path/'b',other,meta)]
    output=tmp_path/'mixed.mp4';render.compose(source,output,meta,tracks)
    assert cutting.probe_source(output)==meta
    raw=cutting.run(['ffmpeg','-v','error','-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','-'])
    size=meta['width']*meta['height']*3
    assert max(raw[:size])<5
    assert max(raw[4*size:5*size])>30
    assert frame_numbers(output)[8:]==list(range(8,12))


def test_polling_own_running_replacement_does_not_revalidate_old_source(ready,monkeypatch):
    from exactcut import mosaic_replace, mosaic_locate
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id']
    job=render.start(bid,sid,[],'',[[10,20]]);assert wait_job(bid,sid,job['id'])['status']=='completed'
    # Simulate a replacement worker holding the gate across its source-version switch.
    monkeypatch.setattr(mosaic_replace,'recover',lambda bid,sid,job:{**job,'replacement':{'status':'running'}})
    monkeypatch.setattr(mosaic_replace,'is_current',lambda *args:False)
    def changed(*args):raise cutting.CutError('当前成片已变化',409)
    monkeypatch.setattr(mosaic_locate,'current',changed)
    assert render.state(bid,sid,job['id'])['replacement']['status']=='running'


def test_clip_preview_combines_regions_and_does_not_replace_full_result(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id']
    full=render.start(bid,sid,[],'',[[0,2]]);assert wait_job(bid,sid,full['id'])['status']=='completed'
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    monkeypatch.setattr(tracking,'run_tracker',lambda source,directory,meta,target,cfg:make_track(directory,target,meta)['result'])
    target={'start':10,'end':20,'seed_frame':12,'x':0,'y':0,'w':40,'h':40}
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}'
    try:
        body={'targets':[target,{**target,'x':60}],'effect':{'style':'solid','expansion':'none'},'reset_at':''}
        assert call(app,'POST',base+'/clip-preview',body,csrf=False)[0]==403
        code,job=call(app,'POST',base+'/clip-preview',body);assert code==202,job
        done=wait_job(bid,sid,job['id']);assert done['status']=='completed',done
        assert done['result']['metadata']['frame_count']==10
        assert render.latest(bid,sid)['job']['id']==full['id']
        assert call(app,'POST',base+'/replace',{'job_id':job['id'],'reset_at':''})[0]==400
        output=render.video_file(bid,sid,job['id'])
        raw=cutting.run(['ffmpeg','-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
        width=session['metadata']['width']
        for x in (10,70):assert max(raw[(10*width+x)*3:(10*width+x)*3+3])<5
        assert file_sha256(source)==sha
    finally:app.shutdown()


def test_clip_preview_of_entire_short_source(tmp_path):
    source=make_video(tmp_path/'source.mp4',frames=6);meta=cutting.probe_source(source)
    target={'start':0,'end':6,'seed_frame':2,'x':0,'y':0,'w':40,'h':40}
    track=make_track(tmp_path/'track',target,meta)
    result=render.process_preview(source,tmp_path/'preview.mp4',meta,[track],lambda text:None,{'style':'blur'})
    assert result['metadata']==meta
