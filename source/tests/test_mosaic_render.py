"""Full-length multi-target composition: real FFmpeg, synthetic masks."""
from pathlib import Path
import pytest
from PIL import Image
from exactcut import cutting, editor_resources, mosaic_render as render
from test_cutting import make_video, frame_numbers, checker_video, gray_frames, block_span
from test_cutting_api import ready, call
from test_phase2_preview_api import fixture, serve
from exactcut import mosaic_session as sessions, mosaic_tracking as tracking
from exactcut.util import file_sha256, write_json_atomic
import time


@pytest.fixture(autouse=True)
def final_queue(fixture):
    from exactcut import mosaic_queue, accounts, workflow
    accounts.initialize(workflow.BATCHES.parent/'accounts.sqlite3','synthetic-password-queue')
    dispatcher=mosaic_queue.Dispatcher(poll_seconds=.01)
    dispatcher.start()
    yield dispatcher
    dispatcher.stop(timeout=None)


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


@pytest.mark.integration
def test_fixed_mask_encoding_reuses_only_identical_active_regions(tmp_path,monkeypatch):
    meta={'width':64,'height':48,'frame_count':15,'fps_num':30000,'fps_den':1001}
    target={'start':1,'end':8,'motion':'fixed','x':0,'y':0,'w':20,'h':18}
    tracks=[{'target':target,'result':meta},{'target':{**target,'start':5,'end':11,'x':32},'result':meta},
            {'target':{**target,'start':0,'end':15},'failed':True}]
    appearance={'style':'pixel','strength':'medium','expansion':'large'}
    original=render.effects.prepare_mask;calls=[]
    def prepare(mask,effect):
        calls.append(1);return original(mask,effect)
    monkeypatch.setattr(render.effects,'prepare_mask',prepare)
    output=tmp_path/'mask.mkv'
    render.encode_mask(output,meta,tracks,(64,48),'30000/1001',appearance,lambda text:None)
    actual=cutting.run(['ffmpeg','-v','error','-i',str(output),'-fps_mode','passthrough','-f','rawvideo','-pix_fmt','gray','-'])
    expected=b''.join(original(render.union_mask(frame,tracks,(64,48)),appearance).tobytes() for frame in range(15))
    assert actual==expected
    assert len(calls)==3,'only A, A+B, B need preparing; blank and failed regions need none'


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


@pytest.mark.integration
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
        if job['status'] not in {'queued','running','cancelling'}:return job
        time.sleep(.025)
    pytest.fail('render did not finish')


@pytest.mark.integration
def test_batch_http_restore_download_and_immutable_source(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id'];calls=[]
    monkeypatch.setattr(tracking,'enabled',lambda:True)
    monkeypatch.setattr(tracking,'settings',lambda:{'enabled':True})
    def fake(source,directory,meta,target,cfg):
        calls.append(target)
        write_json_atomic(directory/'progress.json',{'frames_done':3,'frames_total':target['end']-target['start']})
        live=render.latest(bid,sid)['job']
        assert live['progress']==f"正在跟踪 3/{target['end']-target['start']} 帧（区域 {len(calls)}/2）"
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


@pytest.mark.integration
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
        assert call(app,'POST',base+'/discard',{'reset_at':''})[0]==409
        code,discarded=call(app,'POST',base+'/discard-result',{'job_id':job['id'],'reset_at':''});assert code==200
        assert discarded['discarded_at']
        assert call(app,'GET',base+'/render')[1]['job']['discarded_at']
        assert call(app,'GET',base+'/scan')[1]['report']['status']=='completed'
        assert file_sha256(source)==sha
        code,job2=call(app,'POST',base+'/render',{'targets':[],'ranges':[[5,10]],'reset_at':''});assert code==202
        done2=wait_job(bid,sid,job2['id']);assert done2['status']=='completed',done2
        assert done2['result']['metadata']['frame_count']==session['metadata']['frame_count']-5
    finally:app.shutdown();app.server_close()


@pytest.mark.integration
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


@pytest.mark.integration
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
        with pytest.raises(cutting.CutError):sessions.discard(bid,sid,'')
        cancelled=render.stop(bid,sid,job['id'],'')
        assert cancelled['status']=='cancelling'
        assert render.latest(bid,sid)['job']['id']==job['id']
    finally:finish.set()
    path=sessions.session_path(bid,sid)/f"render-{job['id']}.json"
    for _ in range(100):
        if read_json(path)['status'] not in {'running','cancelling'}:break
        time.sleep(.025)
    assert read_json(path)['status']=='cancelled'
    from exactcut import mosaic_replace
    with pytest.raises(cutting.CutError):mosaic_replace.start(bid,sid,job['id'],'')
    assert workflow.preview_file(bid,rid)[0]==source and file_sha256(source)==sha


@pytest.mark.integration
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
        with editor_resources.acquire('final_media'):pass
        monkeypatch.setattr(render,'process_video',original_process)
        restarted=render.start(bid,sid,[],'',[[1,3]])
        assert not restarted.get('cancel_requested')
        assert wait_job(bid,sid,restarted['id'])['status']=='completed'
    finally:app.shutdown();app.server_close()


@pytest.mark.integration
@pytest.mark.parametrize('managed',[False,True])
def test_confirmation_rechecks_discard_committed_before_asset_gate(ready,monkeypatch,managed):
    from contextlib import contextmanager
    from exactcut import mosaic_replace,cutting_access,workflow,material_versions
    from exactcut.util import read_json
    bid,rid,sha,source=ready
    if managed:material_versions.enroll(workflow.folder(bid))
    sid=sessions.open_session(bid,rid,sha)['id']
    job=render.start(bid,sid,[],'',[[1,3]])
    assert wait_job(bid,sid,job['id'])['status']=='completed'
    original=cutting_access.gate;switched=False;launched=[]
    @contextmanager
    def discard_before_gate(*args,**kwargs):
        nonlocal switched
        if not switched:
            switched=True
            discarded=render.discard_result(bid,sid,job['id'],'')
            assert discarded['discarded_at']
        with original(*args,**kwargs):yield
    monkeypatch.setattr(cutting_access,'gate',discard_before_gate)
    monkeypatch.setattr(mosaic_replace.threading.Thread,'start',lambda thread:launched.append(thread))
    with pytest.raises(cutting.CutError,match='放弃'):
        mosaic_replace.start(bid,sid,job['id'],'')
    assert launched==[]
    saved=read_json(sessions.session_path(bid,sid)/f"render-{job['id']}.json")
    assert saved['discarded_at'] and saved.get('replacement',{}).get('status')!='running'
    assert file_sha256(workflow.preview_file(bid,rid)[0])==sha


@pytest.mark.integration
@pytest.mark.parametrize('change',['status','job_source','replacement_running','replacement_completed','current_source'])
def test_confirmation_revalidates_report_and_version_after_gate(ready,monkeypatch,change):
    from contextlib import contextmanager
    from exactcut import mosaic_replace,cutting_access,workflow
    from exactcut.util import read_json
    bid,rid,sha,source=ready;sid=sessions.open_session(bid,rid,sha)['id']
    job=render.start(bid,sid,[],'',[[1,3]])
    assert wait_job(bid,sid,job['id'])['status']=='completed'
    path=sessions.session_path(bid,sid)/f"render-{job['id']}.json"
    original=cutting_access.gate;switched=False;launched=[]
    @contextmanager
    def change_before_gate(*args,**kwargs):
        nonlocal switched
        if not switched:
            switched=True
            with original(*args,**kwargs):
                saved=read_json(path)
                if change=='status':saved['status']='cancelled'
                elif change=='job_source':saved['source_sha256']='f'*64
                elif change.startswith('replacement_'):saved['replacement']={'status':change.removeprefix('replacement_')}
                else:
                    records=read_json(source.parent.parent/'results.json')
                    records['requests'][rid]['output_sha256']='f'*64
                    write_json_atomic(source.parent.parent/'results.json',records)
                write_json_atomic(path,saved)
        with original(*args,**kwargs):yield
    monkeypatch.setattr(cutting_access,'gate',change_before_gate)
    monkeypatch.setattr(mosaic_replace.threading.Thread,'start',lambda thread:launched.append(thread))
    with pytest.raises(cutting.CutError):mosaic_replace.start(bid,sid,job['id'],'')
    assert launched==[] and file_sha256(source)==sha
    with original(workflow.folder(bid),rid,allow_job=job['id']):pass


def test_target_effect_contract_is_validated_and_preserved():
    meta={'width':100,'height':100,'frame_count':30,'fps_num':30,'fps_den':1}
    target={'start':0,'end':10,'seed_frame':0,'x':0,'y':0,'w':30,'h':30,'effect':{'style':'soft_blur'}}
    assert render.validate_targets(meta,[target])[0]['effect']['style']=='soft_blur'
    with pytest.raises(cutting.CutError):render.validate_targets(meta,[{**target,'effect':{'style':'wrong'}}])


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_clip_preview_combines_regions_and_does_not_replace_full_result(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id']
    full=render.start(bid,sid,[],'',[[0,2]]);assert wait_job(bid,sid,full['id'])['status']=='completed'
    render.discard_result(bid,sid,full['id'],'')
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


@pytest.mark.integration
def test_clip_preview_of_entire_short_source(tmp_path):
    source=make_video(tmp_path/'source.mp4',frames=6);meta=cutting.probe_source(source)
    target={'start':0,'end':6,'seed_frame':2,'x':0,'y':0,'w':40,'h':40}
    track=make_track(tmp_path/'track',target,meta)
    result=render.process_preview(source,tmp_path/'preview.mp4',meta,[track],lambda text:None,{'style':'blur'})
    assert result['metadata']==meta


@pytest.mark.integration
def test_inference_reused_across_effects_but_not_geometry(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);calls=[]
    def infer(source,directory,meta,target,cfg):
        calls.append(target);return make_track(directory,target,meta)['result']
    monkeypatch.setattr(tracking,'run_tracker',infer)
    root=sessions.session_path(bid,session['id'])
    target={'start':0,'end':3,'seed_frame':0,'x':0,'y':0,'w':20,'h':20}
    for i,style in enumerate(('pixel','soft_blur','solid')):
        render.prepare_track(bid,session['id'],session,{**target,'effect':{'style':style}},{},root/f'job-{i}')
    assert len(calls)==1
    render.prepare_track(bid,session['id'],session,{**target,'x':1},{},root/'changed')
    assert len(calls)==2


@pytest.mark.integration
def test_preview_downscales_without_changing_duration(tmp_path):
    source=tmp_path/'source.mp4'
    cutting.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=white:s=1080x1920:r=30','-t','0.4','-c:v','libx264','-pix_fmt','yuv420p',str(source)])
    meta=cutting.probe_source(source)
    target={'start':2,'end':8,'seed_frame':2,'x':0,'y':0,'w':100,'h':100}
    track=make_track(tmp_path/'track',target,meta)
    result=render.process_preview(source,tmp_path/'out.mp4',meta,[track],lambda text:None,{'style':'solid'})
    assert result['metadata']['width']==360
    assert result['metadata']['height']==640
    assert result['metadata']['frame_count']==6


@pytest.mark.integration
def test_legacy_render_masks_require_matching_source_reset_and_config(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha)
    root=sessions.session_path(bid,session['id']);directory=root/'render-old'/'target-0'
    target={'start':0,'end':3,'seed_frame':0,'x':0,'y':0,'w':20,'h':20}
    make_track(directory,target,session['metadata'])
    write_json_atomic(root/'render-old.json',{'source_sha256':sha,'reset_at':''})
    write_json_atomic(directory/'request.json',{'source':str(root/'source.mp4'),'metadata':session['metadata'],'target':target,'config':{'device':'cpu'}})
    calls=[]
    def infer(source,directory,meta,target,cfg):
        calls.append(cfg);assert cfg['masks_only'];return make_track(directory,target,meta)['result']
    monkeypatch.setattr(tracking,'run_tracker',infer)
    track=render.prepare_track(bid,session['id'],session,{**target,'effect':{'style':'solid'}},{'device':'cpu'},root/'new')
    assert track['directory']==directory and not calls
    render.prepare_track(bid,session['id'],session,target,{'device':'mps'},root/'new')
    assert len(calls)==1
    render.prepare_track(bid,session['id'],{**session,'reset_at':'changed'},target,{'device':'cpu'},root/'new')
    assert len(calls)==2


def test_fixed_region_validation_and_mask_without_tracker(tmp_path,monkeypatch):
    meta={'width':160,'height':96,'frame_count':900,'fps_num':30,'fps_den':1}
    target={'start':10,'end':800,'seed_frame':10,'x':20,'y':30,'w':40,'h':10,'motion':'fixed'}
    assert render.validate_targets(meta,[target])[0]['motion']=='fixed'
    with pytest.raises(cutting.CutError):render.validate_targets(meta,[{**target,'motion':'unknown'}])
    monkeypatch.setattr(tracking,'run_tracker',lambda *a:pytest.fail('fixed mask must not track'))
    track=render.prepare_track('unused','unused',{'metadata':meta},target,{},tmp_path)
    assert render.union_mask(9,[track],(80,48)).getbbox() is None
    assert render.union_mask(10,[track],(80,48)).getbbox()==(10,15,30,20)
    assert render.union_mask(799,[track],(80,48)).getbbox()==(10,15,30,20)
    assert render.union_mask(800,[track],(80,48)).getbbox() is None


@pytest.mark.integration
def test_fixed_render_and_preview_without_sam(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id']
    monkeypatch.setattr(tracking,'enabled',lambda:False)
    monkeypatch.setattr(tracking,'run_tracker',lambda *a:pytest.fail('fixed region called SAM'))
    target={'start':10,'end':20,'seed_frame':10,'x':0,'y':0,'w':40,'h':40,'motion':'fixed'}
    for preview in (False,True):
        job=render.start(bid,sid,[target],'',effect={'style':'solid'},preview=preview)
        finished=wait_job(bid,sid,job['id']);assert finished['status']=='completed',finished
        assert finished['result']['metadata']['frame_count']==(10 if preview else session['metadata']['frame_count'])
        assert finished['result']['skipped_frames']==0
        if not preview:render.discard_result(bid,sid,job['id'],'')
    assert file_sha256(source)==sha


@pytest.mark.integration
def test_fixed_mask_pixels_and_time_boundaries_in_real_video(tmp_path):
    source=make_video(tmp_path/'source.mp4',frames=12)
    meta=cutting.probe_source(source)
    target={'motion':'fixed','start':3,'end':8,'seed_frame':3,'x':0,'y':0,'w':meta['width'],'h':meta['height']}
    track=render.prepare_track('unused','unused',{'metadata':meta},target,{},tmp_path)
    output=tmp_path/'out.mp4'
    render.compose(source,output,meta,[track],effect={'style':'solid','expansion':'none'})
    pixels=cutting.run(['ffmpeg','-v','error','-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','-'])
    stride=meta['width']*meta['height']*3
    assert max(pixels[3*stride:8*stride])<5
    assert max(pixels[2*stride:3*stride])>20
    assert max(pixels[8*stride:9*stride])>20
    preview=tmp_path/'preview.mp4'
    render.process_preview(source,preview,meta,[track],lambda text:None,{'style':'solid','expansion':'none'})
    pixels=cutting.run(['ffmpeg','-v','error','-i',str(preview),'-f','rawvideo','-pix_fmt','rgb24','-'])
    assert len(pixels)==5*stride and max(pixels)<5


@pytest.mark.integration
def test_discard_interrupts_composing_process_and_releases_compute(ready,monkeypatch):
    import sys,threading
    from exactcut.util import read_json
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha);sid=session['id'];entered=threading.Event()
    def slow(*args,**kwargs):
        entered.set();cutting.run([sys.executable,'-c','import time;time.sleep(60)'])
        raise AssertionError('discard must interrupt composition')
    monkeypatch.setattr(render,'process_video',slow)
    job=render.start(bid,sid,[],'',[[1,3]])
    assert entered.wait(3)
    with pytest.raises(cutting.CutError):sessions.discard(bid,sid,'')
    render.stop(bid,sid,job['id'],'')
    path=sessions.session_path(bid,sid)/f"render-{job['id']}.json"
    deadline=time.monotonic()+5
    while read_json(path)['status'] in {'running','cancelling'} and time.monotonic()<deadline:time.sleep(.025)
    saved=read_json(path)
    assert saved['status']=='cancelled',saved
    assert 'result' not in saved
    assert render.latest(bid,sid)['job']['status']=='cancelled'
    with editor_resources.acquire('final_media'):pass


@pytest.mark.integration
def test_mixed_processing_encodes_video_once_and_preserves_source_frame_mapping(tmp_path,monkeypatch):
    source=make_video(tmp_path/'source.mp4',frames=30,fps='30000/1001')
    meta=cutting.probe_source(source);original=cutting.run;encodes=[]
    def run(args,*a,**kw):
        # The segment path's one-frame encoder trial (this ultrafast source cannot be continued) is not a video encode.
        trial='-frames:v' in args and args[args.index('-frames:v')+1]=='1'
        if 'libx264' in args and not trial:encodes.append(args)
        return original(args,*a,**kw)
    monkeypatch.setattr(cutting,'run',run)
    target={'start':15,'end':25,'seed_frame':20,'x':0,'y':0,'w':60,'h':40}
    track=make_track(tmp_path/'track',target,meta)
    output=tmp_path/'out.mp4'
    result=render.process_video(source,output,meta,[track],[[3,9]])
    assert result['metadata']['frame_count']==24
    assert len(encodes)==1
    assert frame_numbers(output)[:9]==[0,1,2,9,10,11,12,13,14]
    assert frame_numbers(output)[-5:]==[25,26,27,28,29]
    assert result['metadata']['has_audio']


@pytest.mark.integration
@pytest.mark.parametrize('audio,fps',[(True,'30'),(False,'30000/1001')])
def test_one_pass_mask_boundaries_survive_head_and_middle_deletions(tmp_path,audio,fps):
    source=make_video(tmp_path/'source.mp4',frames=20,fps=fps,audio=audio)
    meta=cutting.probe_source(source)
    t={'motion':'fixed','start':8,'end':15,'seed_frame':8,'x':0,'y':0,'w':112,'h':64}
    track=render.prepare_track('unused','unused',{'metadata':meta},t,{},tmp_path)
    out=tmp_path/'out.mp4'
    result=render.process_video(source,out,meta,[track],[[0,3],[10,12]],effect={'style':'solid','expansion':'none'})
    assert result['metadata']=={**meta,'frame_count':15}
    assert frame_numbers(out)==[3,4,5,6,7,0,0,0,0,0,15,16,17,18,19]

@pytest.mark.integration
@pytest.mark.parametrize('engine',[5,6])
def test_raw_inference_survives_render_identity_upgrade(ready,monkeypatch,engine):
    from exactcut.util import json_sha256
    from exactcut import mosaic_effects
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha)
    root=sessions.session_path(bid,session['id'])
    target={'start':0,'end':3,'seed_frame':0,'x':0,'y':0,'w':20,'h':20};cfg={'device':'cpu'}
    key=json_sha256({'source':sha,'reset':session.get('reset_at',''),'target':target,'config':cfg,'engine':engine,'effect':mosaic_effects.validate(None)})[:32]
    old=root/f'inference-{key}';make_track(old,target,session['metadata'])
    monkeypatch.setattr(tracking,'identity',lambda *a,**kw:'future-render-version')
    monkeypatch.setattr(tracking,'run_tracker',lambda *a:pytest.fail('unchanged geometry must reuse raw masks'))
    result=render.prepare_track(bid,session['id'],session,{**target,'effect':{'style':'solid'}},cfg,root/'new')
    assert result['directory']==old


def test_inference_identity_is_independent_and_invalidates_inputs():
    session={'source_sha256':'a','reset_at':''};target={'start':0,'end':3,'seed_frame':0,'x':0,'y':0,'w':20,'h':20};cfg={'device':'cpu'}
    key=tracking.inference_identity(session,target,cfg)
    assert tracking.inference_identity(session,{**target,'effect':{'style':'solid'}},cfg)==key
    for other,t,c in [({**session,'source_sha256':'b'},target,cfg),({**session,'reset_at':'new'},target,cfg),(session,{**target,'x':1},cfg),(session,target,{**cfg,'inference_size':768})]:
        assert tracking.inference_identity(other,t,c)!=key

@pytest.mark.integration
@pytest.mark.parametrize('change',['missing_mask','reset','source','config'])
def test_legacy_raw_cache_rejects_incomplete_or_different_inputs(ready,monkeypatch,change):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha)
    root=sessions.session_path(bid,session['id']);target={'start':0,'end':3,'seed_frame':0,'x':0,'y':0,'w':20,'h':20};cfg={'device':'cpu'}
    old=root/f'inference-{tracking.legacy_inference_identities(session,target,cfg)[0]}'
    make_track(old,target,session['metadata'])
    if change=='missing_mask':(old/'masks/000001.png').unlink()
    if change=='reset':session={**session,'reset_at':'new'}
    if change=='source':session={**session,'source_sha256':'new'}
    if change=='config':cfg={**cfg,'inference_size':768}
    calls=[]
    def infer(source,directory,meta,target,cfg):
        calls.append(directory);return make_track(directory,target,meta)['result']
    monkeypatch.setattr(tracking,'run_tracker',infer)
    render.prepare_track(bid,session['id'],session,target,cfg,root/'new')
    assert len(calls)==1 and calls[0]!=old


def test_live_tracking_progress_is_scoped_and_tolerates_missing_file(tmp_path):
    job={'status':'running','stage':'tracking','tracking_inference':'inference-'+'a'*32,
         'completed_regions':1,'total_regions':3,'targets':[{}, {'start':10,'end':145}],
         'progress':'正在准备第 2 / 3 个打码区域'}
    assert render.tracking_progress(tmp_path,job)==job
    file=tmp_path/job['tracking_inference']/'progress.json'
    write_json_atomic(file,{'frames_done':60,'frames_total':135})
    assert render.tracking_progress(tmp_path,job)['progress']=='正在跟踪 60/135 帧（区域 2/3）'
    assert job['progress']=='正在准备第 2 / 3 个打码区域'
    for fields in ({'stage':'composing'},{'status':'completed'},{'cancel_requested':True},
                   {'tracking_inference':'../outside'},{'completed_regions':0}):
        other={**job,**fields}
        assert render.tracking_progress(tmp_path,other)==other
    for data in ({'frames_done':136,'frames_total':135},{'frames_done':60,'frames_total':140}):
        write_json_atomic(file,data)
        assert render.tracking_progress(tmp_path,job)==job
    file.write_text('{')
    assert render.tracking_progress(tmp_path,job)==job

from interactive_test_support import interactive_dispatcher


def test_active_intervals_union_exclusive_bounds_and_failed_tracks():
    def track(start,end,failed=False):return {'target':{'start':start,'end':end},'failed':failed}
    assert render.active_intervals([])==[]
    assert render.active_intervals([track(0,2),track(2,5)])==[[0,5]]
    assert render.active_intervals([track(2,6),track(4,8)])==[[2,8]]
    assert render.active_intervals([track(2,4),track(6,8)])==[[2,4],[6,8]]
    assert render.active_intervals([track(0,9,True),track(3,4)])==[[3,4]]
    assert render.active_intervals([track(0,9,True)])==[]


def blurred_mask_at(box,meta,**effect):
    target={'motion':'fixed','start':box['start'],'end':box['end'],'seed_frame':box['start'],
            'x':box.get('x',0),'y':box.get('y',0),'w':box.get('w',meta['width']),'h':box.get('h',meta['height']),
            'effect':{'style':'blur','strength':'heavy','expansion':'none',**effect}}
    return target


@pytest.mark.integration
def test_blur_uses_source_frame_indexes_before_deletion_and_keeps_audio(tmp_path):
    source=checker_video(tmp_path/'source.mp4',frames=30,audio=True,fps='30000/1001')
    meta=cutting.probe_source(source)
    target=blurred_mask_at({'start':8,'end':16},meta)
    track=render.prepare_track('unused','unused',{'metadata':meta},target,{},tmp_path)
    output=tmp_path/'out.mp4'
    render.compose(source,output,meta,[track],ranges=[[0,8]])
    result=cutting.probe_source(output)
    assert result['frame_count']==22 and result['has_audio']
    assert (result['fps_num'],result['fps_den'])==(meta['fps_num'],meta['fps_den'])
    assert (result['width'],result['height'])==(meta['width'],meta['height'])
    frames=gray_frames(output)
    assert len(frames)==22
    for index,frame in enumerate(frames):
        if 8<=8+index<16:assert block_span(frame,24,24)<90
        else:assert block_span(frame,24,24)>150
    reference=tmp_path/'reference.mp4';cutting.render_video(source,reference,[[0,8]],meta)
    pcm=lambda path:cutting.run(['ffmpeg','-v','error','-i',str(path),'-f','s16le','-ac','2','-ar','44100','-'])
    assert pcm(output)==pcm(reference)


@pytest.mark.integration
@pytest.mark.parametrize('start,end',[(0,2),(28,30)])
def test_blur_interval_covers_first_frame_and_excludes_end(tmp_path,start,end):
    source=checker_video(tmp_path/'source.mp4',frames=30)
    meta=cutting.probe_source(source)
    track=render.prepare_track('unused','unused',{'metadata':meta},
                               blurred_mask_at({'start':start,'end':end},meta),{},tmp_path)
    output=tmp_path/'out.mp4'
    render.compose(source,output,meta,[track])
    frames=gray_frames(output)
    assert len(frames)==meta['frame_count']
    for index,frame in enumerate(frames):
        if start<=index<end:assert block_span(frame,24,24)<90
        else:assert block_span(frame,24,24)>150


@pytest.mark.integration
def test_overlapping_groups_use_their_own_intervals_and_effects(tmp_path):
    source=checker_video(tmp_path/'source.mp4',frames=30)
    meta=cutting.probe_source(source)
    solid={**blurred_mask_at({'start':4,'end':8},meta),'effect':{'style':'solid','expansion':'none'}}
    blur=blurred_mask_at({'start':6,'end':10},meta)
    tracks=[render.prepare_track('unused','unused',{'metadata':meta},target,{},tmp_path)
            for target in (solid,blur)]
    output=tmp_path/'out.mp4'
    render.compose(source,output,meta,tracks)
    frames=gray_frames(output)
    for index,frame in enumerate(frames):
        if 4<=index<6:assert max(frame)<5
        elif 6<=index<10:assert block_span(frame,24,24)<90
        else:assert block_span(frame,24,24)>150


@pytest.mark.integration
def test_compose_passes_source_frame_intervals_with_deletion(tmp_path,monkeypatch):
    source=make_video(tmp_path/'source.mp4',frames=12);meta=cutting.probe_source(source)
    track=render.prepare_track('unused','unused',{'metadata':meta},
                               blurred_mask_at({'start':4,'end':8},meta),{},tmp_path)
    captured={};original=render.effects.layered_graph
    def capture(meta,appearances,intervals=None):
        captured['intervals']=intervals
        return original(meta,appearances,intervals)
    monkeypatch.setattr(render.effects,'layered_graph',capture)
    output=tmp_path/'out.mp4'
    render.compose(source,output,meta,[track],ranges=[[0,4]])
    assert cutting.probe_source(output)['frame_count']==8
    # The graph sees pre-deletion source frames, never the 8-frame output index.
    assert captured['intervals']==[[[4,8]]]
