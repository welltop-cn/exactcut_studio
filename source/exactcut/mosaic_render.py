"""Full-length mosaic artifacts from a frozen set of adopted regions."""
import math
import re
import subprocess
import threading
from pathlib import Path
from PIL import Image, ImageChops
from . import cutting, mosaic_session as sessions, mosaic_locate, mosaic_tracking as tracking
from . import cutting_access, workflow, storage
from .media import bounded_ffmpeg
from . import mosaic_effects as effects, processing_cancel
from .util import file_sha256, json_sha256, read_json, write_json_atomic, now_iso
from . import mosaic_queue as queue
import uuid
import time
from contextlib import nullcontext
from .runtime import mutation_lock, maintenance_file
from . import editor_resources,mosaic_mps


def validate_targets(meta,targets):
    if not isinstance(targets,list) or not 1<=len(targets)<=8:
        raise cutting.CutError('请采用 1～8 个区域后生成完整视频')
    checked=[tracking.validate_target(meta,t) for t in targets]
    unique={json_sha256(t):t for t in checked}
    return sorted(unique.values(),key=lambda t:(t['start'],t['end'],t['seed_frame'],t['x'],t['y'],t['w'],t['h']))


def union_mask(frame,tracks,size):
    mask=Image.new('L',size)
    for track in tracks:
        target=track['target']
        if not target['start']<=frame<target['end']:continue
        if track.get('failed'):continue
        if target.get('motion')=='fixed':
            sx=size[0]/track['result']['width'];sy=size[1]/track['result']['height']
            box=(math.floor(target['x']*sx),math.floor(target['y']*sy),math.ceil((target['x']+target['w'])*sx),math.ceil((target['y']+target['h'])*sy))
            mask.paste(255,box)
            continue
        index=frame-target['start'];record=track['result']['frames'][index]
        if record['box'] is None:continue
        path=Path(track['directory'])/'masks'/f'{index:06d}.png'
        try:
            with Image.open(path) as image:
                if image.size!=(track['result']['width'],track['result']['height']):raise ValueError('mask size')
                current=image.convert('L').resize(size,Image.Resampling.NEAREST)
                if not current.getbbox():raise ValueError('missing foreground')
                mask=ImageChops.lighter(mask,current)
        except (OSError,ValueError):raise cutting.CutError('跟踪遮罩文件不完整，请重新生成') from None
    return mask


def active_intervals(tracks):
    # Union of source-frame spans that can carry a mask; failed tracks never do.
    spans=sorted((t['target']['start'],t['target']['end']) for t in tracks if not t.get('failed'))
    merged=[]
    for start,end in spans:
        if merged and start<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],end)
        else:merged.append([start,end])
    return merged


def compose(source,output,meta,tracks,progress=lambda text:None,effect=None,ranges=None):
    effect=effects.validate(effect)
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    width=min(640,meta['width']);height=max(2,round(meta['height']*width/meta['width']/2)*2)
    size=(width,height);rate=f"{meta['fps_num']}/{meta['fps_den']}"
    partial=output.with_name(output.stem+'.partial.mp4')
    groups={}
    for track in tracks:
        appearance=effects.validate(track['target'].get('effect',effect))
        key=tuple(appearance.values())
        if key not in groups:groups[key]=(appearance,[])
        groups[key][1].append(track)
    if not groups:groups[tuple(effect.values())]=(effect,[])
    if cutting.settings().get('segment_reencode') is True:
        layers=list(groups.values())
        def span_masks(start,end,directory):
            directory.mkdir(parents=True,exist_ok=True);paths=[]
            for index,(appearance,group) in enumerate(layers):
                path=directory/f'mask-{index}.mkv'
                encode_mask(path,meta,group,size,rate,appearance,progress,frames=range(start,end));paths.append(path)
            return paths
        video_layers={'appearances':[a for a,_ in layers],'intervals':[active_intervals(g) for _,g in layers],'masks':span_masks}
        progress('正在合成有打码区域的片段，其余画面原样保留…')
        actual=cutting.render_video(source,partial,ranges or [],meta,video_graph='segments',video_layers=video_layers,
                                    copy_audio=not ranges,segments_only=True)
        if actual is not None:
            actual.pop('sha256');partial.replace(output)
            return {'metadata':actual,'sha256':file_sha256(output),'bytes':output.stat().st_size}
    masks=[];appearances=[];intervals=[]
    for index,(appearance,group) in enumerate(groups.values()):
        mask_video=output.parent/f'combined-mask-{index}.mkv'
        encode_mask(mask_video,meta,group,size,rate,appearance,progress)
        masks.append(mask_video);appearances.append(appearance);intervals.append(active_intervals(group))
    progress('正在合成完整视频并保留原音频…')
    w,h=meta['width'],meta['height']
    # RGB planar masks avoid limited-range luma turning white masks into partial blends.
    graph=effects.layered_graph(meta,appearances,intervals)
    if ranges:
        progress('正在一次合成打码与删除片段…')
        actual=cutting.render_video(source,partial,ranges,meta,video_graph=graph,video_inputs=masks,_segments=False)
        actual.pop('sha256')
    else:
        cutting.run(['ffmpeg','-y','-v','error','-i',str(source),*[arg for mask in masks for arg in ('-i',str(mask))],'-filter_complex',graph,
                     '-map','[out]','-map','0:a:0?','-c:v','libx264','-crf','18','-preset','fast','-c:a','copy','-movflags','+faststart',str(partial)],900)
        progress('正在校验完整视频的帧数、分辨率和音频…')
        actual=cutting.probe_source(partial)
        if actual!=meta:raise cutting.CutError('完整视频校验未通过，原片保留')
    partial.replace(output)
    for mask in masks:mask.unlink(missing_ok=True)
    return {'metadata':actual,'sha256':file_sha256(output),'bytes':output.stat().st_size}


@editor_resources.media_operation
def encode_mask(mask_video,meta,tracks,size,rate,effect,progress,frames=None):
    width,height=size
    progress('正在合并全部区域遮罩…')
    with (mask_video.with_suffix('.log')).open('wb') as log:
        process=processing_cancel.spawn(bounded_ffmpeg(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','gray','-s',f'{width}x{height}','-r',rate,'-i','pipe:0','-an','-c:v','ffv1',str(mask_video)]),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log)
        timer=threading.Timer(600,lambda:processing_cancel.terminate(process));timer.start()
        try:
            black=bytes(width*height);cached_active=None;cached_mask=None
            for frame in (range(meta['frame_count']) if frames is None else frames):
                processing_cancel.check()
                active=tuple(i for i,t in enumerate(tracks) if not t.get('failed') and t['target']['start']<=frame<t['target']['end'])
                if not active:raw=black
                elif all(tracks[i]['target'].get('motion')=='fixed' for i in active):
                    if active!=cached_active:
                        cached_mask=effects.prepare_mask(union_mask(frame,tracks,size),effect).tobytes();cached_active=active
                    raw=cached_mask
                else:raw=effects.prepare_mask(union_mask(frame,tracks,size),effect).tobytes()
                process.stdin.write(raw)
                if frame%120==0:progress(f"合并遮罩 {frame+1} / {meta['frame_count']} 帧")
            process.stdin.close()
            if process.wait(timeout=60):raise cutting.CutError('遮罩视频生成失败，未生成成片')
        except Exception:
            processing_cancel.terminate(process);process.wait()
            raise
        finally:timer.cancel()

def recover(path):
    if not path.is_file():return None
    job=read_json(path)
    if job['status']=='running' and not job.get('queued_task'):
        try:lease=cutting.lock_file(path.with_suffix('.worker.lock'))
        except cutting.CutError:return job
        lease.close();processing_cancel.recover_interrupted(job,'上次完整视频生成已中断，请重新生成')
        write_json_atomic(path,job)
    return job


def tracking_progress(root,job):
    if job.get('status')!='running' or job.get('stage')!='tracking' or job.get('cancel_requested'):return job
    name=job.get('tracking_inference','')
    if not isinstance(name,str) or not re.fullmatch(r'inference-[0-9a-f]{32}(?:-work-[0-9a-f]{32})?',name):return job
    try:
        index=job['completed_regions'];target=job['targets'][index]
        p=read_json(root/name/'progress.json');done=p['frames_done'];total=p['frames_total']
        if type(done) is not int or type(total) is not int or total!=target['end']-target['start'] or not 0<=done<=total:return job
    except (OSError,ValueError,KeyError,IndexError,TypeError):return job
    return {**job,'progress':f"正在跟踪 {done}/{total} 帧（区域 {index+1}/{job['total_regions']}）"}


def state(bid,sid,jid):
    if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise cutting.CutError('完整视频任务不存在',404)
    path=sessions.session_path(bid,sid)/f'render-{jid}.json'
    with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
        job=recover(path)
        if not job:raise cutting.CutError('完整视频任务不存在',404)
        from . import mosaic_replace
        job=mosaic_replace.recover(bid,sid,job)
        session=sessions.read_session(bid,sid);cutting.require_reset(session,job['reset_at'])
        # Recovery leaves running only while the replacement owns the edit gate.
        # Its commit can change the source between two version reads.
        if job.get('replacement',{}).get('status')=='running':return job
        if not job.get('queued_task') and not mosaic_replace.is_current(bid,session,job):mosaic_locate.current(bid,sid,job,readonly=True)
        return tracking_progress(path.parent,job)


def latest(bid,sid):
    path=sessions.session_path(bid,sid)/'render-latest.json'
    if not path.is_file():return {'job':None}
    jid=read_json(path)['id']
    job=read_json(path.parent/f'render-{jid}.json')
    if job.get('reset_at','')!=sessions.read_session(bid,sid).get('reset_at',''):return {'job':None}
    return {'job':state(bid,sid,jid)}


def video_file(bid,sid,jid):
    job=state(bid,sid,jid)
    if job['status']!='completed':raise cutting.CutError('完整打码视频尚未生成',409)
    output=sessions.session_path(bid,sid)/f'render-{jid}'/'mosaic.mp4'
    if not output.is_file():raise cutting.CutError('成片文件已失效，请重新生成',410)
    return output


def current(bid,sid,job,*,permit=None):
    if not job.get('queued_task'):return mosaic_locate.current(bid,sid,job,permit=permit)
    if permit is not None:raise cutting.CutError("最终任务不能使用交互只读许可",409)
    session=sessions.read_session(bid,sid);cutting.require_reset(session,job['reset_at'])
    if session['source_sha256']!=job['source_sha256']:raise cutting.CutError('原片版本已变化',409)
    cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=job['source_sha256'],allow_job=job['id'])
    return session


def submit(bid,sid,targets,reset_at,ranges,effect,actor,submission_id=None,editor_draft=None):
    if submission_id is not None and (not isinstance(submission_id,str) or not re.fullmatch('[0-9a-f]{32}',submission_id)):
        raise cutting.CutError('提交回执标识无效')
    if editor_draft is not None and (not isinstance(editor_draft,dict) or editor_draft.get('schema_version')!=1):
        raise cutting.CutError('编辑草稿格式无效')
    session=sessions.read_session(bid,sid);batch=workflow.folder(bid)
    # The asset gate serializes admission, including retries of a frozen task.
    from . import material_versions
    with mutation_lock(), cutting_access.gate(batch,session['request_id'],readonly=True), (material_versions.locked(batch) if material_versions.managed(batch) else nullcontext()):
        if maintenance_file().exists():raise cutting.CutError('系统正在维护，请稍后重试',503)
        session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
        effect=effects.validate(effect)
        if ranges is not None and not isinstance(ranges,list):raise cutting.CutError('删除范围格式无效')
        ranges=cutting.normalize_ranges(ranges,session['metadata']['frame_count']) if ranges else []
        targets=validate_targets(session['metadata'],targets) if targets!=[] else []
        if not targets and not ranges:raise cutting.CutError('请选择要删除或打码的片段')
        if any(t.get('motion')!='fixed' for t in targets) and not tracking.enabled():raise cutting.CutError('本机 SAM 2 跟踪尚未配置',503)
        fingerprint=json_sha256({'source':session['source_sha256'],'reset':reset_at,'targets':targets,'ranges':ranges,'effect':effect})
        existing=queue.blocked(bid,session['request_id'])
        if existing:
            job=queue.report(existing)
            if existing['session_id']==sid and job.get('fingerprint')==fingerprint:return job
            raise cutting.CutError('该素材已提交处理，请完成或取消后再编辑',409)
        cutting_access.require_editable(batch,session['request_id'],source_sha=session['source_sha256'])
        jid=uuid.uuid4().hex
        job={'schema_version':1,'id':jid,'queued_task':True,'status':'queued','stage':'queued',
             'source_sha256':session['source_sha256'],'reset_at':reset_at,'targets':targets,'ranges':ranges,
             'effect':effect,'config':tracking.settings(),'fingerprint':fingerprint,'actor':actor,'submission_id':submission_id,'editor_draft':editor_draft,
             'created_at':now_iso(),'progress':'等待后台处理','completed_regions':0,'total_regions':len(targets)}
        queue.enqueue(bid,sid,session['request_id'],actor,job)
        return job


QUEUED_PROGRESS='排队中，轮到后自动开始处理'


def execute(item,stop_event):
    from . import accounts
    bid,sid=item['batch_id'],item['session_id'];path=queue.job_path(item);leases=[];slot=None
    try:
        leases.append(cutting.lock_file(path.with_suffix('.worker.lock')))
        # Stay queued (no asset gate held) until a final media slot is free; the
        # slot is handed to the first final media stage. Cancel stays responsive.
        while True:
            job=read_json(path)
            if job.get('cancel_requested'):
                with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
                    job=read_json(path);job.update(status='cancelled',progress='已停止生成，参数已保留');write_json_atomic(path,job)
                return
            if job['status'] in {'completed','failed','cancelled'}:return
            if stop_event.is_set():return 'deferred'
            try:
                slot=editor_resources.acquire('final_media',item['actor']);break
            except cutting.ResourceBusy:
                if job.get('progress')!=QUEUED_PROGRESS:
                    with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
                        job=read_json(path)
                        if job['status']=='queued' and not job.get('cancel_requested'):
                            job['progress']=QUEUED_PROGRESS;write_json_atomic(path,job)
                time.sleep(.1)
        user=accounts.get(workflow.BATCHES.parent/'accounts.sqlite3',item['actor'])
        if not user or not user.get('enabled') or not accounts.allowed(workflow.folder(bid),user):raise cutting.CutError('发起账号已停用或无权访问项目',403)
        with cutting_access.gate(workflow.folder(bid),item['request_id'],allow_job=item['id']):
            current(bid,sid,job)
            leases.append(cutting.lock_file(cutting.lock_root()/f'{bid}.lock',shared=True))
            with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
                job=read_json(path)
                if not job.get('cancel_requested'):job.update(status='running',stage='tracking',progress='正在准备处理视频')
                write_json_atomic(path,job)
        with processing_cancel.scope(lambda:read_json(path).get('cancel_requested',False),leases=leases),editor_resources.final_group(),editor_resources.reservation(('final_media',slot)):
            slot=None
            _work(bid,sid,path,job,job['config'],[])
    finally:
        if slot:slot.close()
        for lease in reversed(leases):lease.close()


def discard_result(bid,sid,jid,reset_at):
    if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise cutting.CutError('生成任务不存在',404)
    session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
    with cutting_access.gate(workflow.folder(bid),session['request_id'],allow_job=jid):
        path=sessions.session_path(bid,sid)/f'render-{jid}.json'
        with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
            job=read_json(path)
            if job['status']!='completed' or job.get('replacement',{}).get('status')=='completed':raise cutting.CutError('当前任务不能放弃结果',409)
            job['discarded_at']=now_iso();write_json_atomic(path,job)
            return job


def start(bid,sid,targets,reset_at,ranges=None,effect=None,preview=False,actor='admin',submission_id=None,editor_draft=None):
    if not preview:return submit(bid,sid,targets,reset_at,ranges,effect,actor,submission_id,editor_draft)
    effect=effects.validate(effect)
    session=sessions.read_session(bid,sid)
    with cutting_access.gate(workflow.folder(bid),session['request_id']):
        session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
        cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'])
        if ranges is not None and not isinstance(ranges,list):raise cutting.CutError('删除范围格式无效')
        ranges=cutting.normalize_ranges(ranges,session['metadata']['frame_count']) if ranges else []
        if targets!=[]:targets=validate_targets(session['metadata'],targets)
        if preview and not targets:raise cutting.CutError('请先选择本片段的打码区域')
        if not targets and not ranges:raise cutting.CutError('请选择要删除或打码的片段')
        if any(t.get('motion')!='fixed' for t in targets) and not tracking.enabled():raise cutting.CutError('本机 SAM 2 跟踪尚未配置',503)
        cfg=tracking.settings()
        jid=json_sha256({'source':session['source_sha256'],'reset':reset_at,'targets':targets,'ranges':ranges,'config':cfg,'engine':7,'effect':effect,'fallback':'fail_job',**(mosaic_mps.fingerprint(cfg) if any(t.get('motion')!='fixed' for t in targets) else {}),**({'preview':True,'preview_engine':2} if preview else {})})[:32]
        path=sessions.session_path(bid,sid)/f'render-{jid}.json'
        with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
            old=recover(path)
            if old and (old['status'] in {'queued','running'} or old['status']=='completed' and (path.parent/f'render-{jid}'/'mosaic.mp4').is_file()):
                if not preview:write_json_atomic(path.parent/'render-latest.json',{'id':jid})
                return old
            job={'schema_version':1,'id':jid,'status':'queued','stage':'waiting','source_sha256':session['source_sha256'],'reset_at':reset_at,
                 'targets':targets,'ranges':ranges,'effect':effect,'preview':True,'actor':actor,'created_at':now_iso(),'progress':'效果预览已接收，等待处理','completed_regions':0,'total_regions':len(targets)}
            from . import interactive_queue
            interactive_queue.enqueue('clip',session,path,job,cfg,state_lock=path.parent/'render-state.lock')
    return job


def stop(bid,sid,jid,reset_at):
    session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
    if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise cutting.CutError('生成任务不存在',404)
    path=sessions.session_path(bid,sid)/f'render-{jid}.json'
    with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
        job=recover(path)
        if not job:raise cutting.CutError('生成任务不存在',404)
        if job.get('queued_task'):current(bid,sid,job)
        else:mosaic_locate.current(bid,sid,job,readonly=True)
        if job['status']=='queued':
            job.update(status='cancelled',cancel_requested=True,progress='已取消排队，参数已保留')
            write_json_atomic(path,job)
        elif job['status'] in {'running','cancelling'}:
            job.update(cancel_requested=True,**({'status':'cancelling'} if job.get('queued_task') else {}),progress='正在停止生成…')
            write_json_atomic(path,job)
        return job


def prepare_track(bid,sid,session,target,cfg,directory,*,on_tracking=None,validate=lambda:None):
    if target.get('motion')=='fixed':
        return {'target':target,'result':{'width':session['metadata']['width'],'height':session['metadata']['height'],'frames':[]}}
    from . import mosaic_inference
    return mosaic_inference.prepare(sessions.session_path(bid,sid),session,target,cfg,directory,on_tracking=on_tracking,validate=validate)

def _work(bid,sid,path,job,cfg,leases,permit=None):
    with processing_cancel.scope(lambda:read_json(path).get('cancel_requested',False),leases=leases or None), (editor_resources.encoding() if job.get('preview') else nullcontext()):
        _work_cancelable(bid,sid,path,job,cfg,leases,permit)


def _work_cancelable(bid,sid,path,job,cfg,leases,permit=None):
    cancelled=False
    def update(**fields):
        with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
            saved=read_json(path)
            if saved.get('cancel_requested'):raise processing_cancel.Cancelled()
            saved.update(fields);write_json_atomic(path,saved)
    try:
        session=current(bid,sid,job,permit=permit);source=path.parent/'source.mp4';directory=path.parent/f"render-{job['id']}"
        if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片校验失败')
        storage.require_space(source.parent,source.stat().st_size*3+2*1024**3)
        directory.mkdir(exist_ok=True);tracks=[];warnings=[];skipped=set()
        for index,target in enumerate(job['targets']):
            current(bid,sid,job,permit=permit)
            update(progress=f"正在准备第 {index+1} / {len(job['targets'])} 个打码区域",completed_regions=index,tracking_inference='')
            track=prepare_track(bid,sid,session,target,cfg,directory/f'target-{index}',on_tracking=lambda name:update(tracking_inference=name),validate=lambda:current(bid,sid,job,permit=permit))
            for frame in track['result']['frames']:
                if frame['box'] is None:skipped.add(frame['frame'])
            for warning in track['result'].get('warnings',[]):warnings.append({'region':index+1,**warning})
            empty=sum(f['box'] is None for f in track['result']['frames'])
            if empty:warnings.append({'region':index+1,'frame':target['start'],'reason':f'{empty} 帧失跟，已跳过该区域'})
            tracks.append(track)
        update(stage='composing',completed_regions=len(tracks))
        current(bid,sid,job,permit=permit)
        if job.get('preview'):
            result=process_preview(source,directory/'mosaic.mp4',session['metadata'],tracks,lambda text:update(progress=text),job.get('effect'))
        else:
            result=process_video(source,directory/'mosaic.mp4',session['metadata'],tracks,job.get('ranges',[]),lambda text:update(progress=text),effect=job.get('effect'))
        with cutting_access.gate(workflow.folder(bid),session['request_id'],allow_job=job['id'] if job.get('queued_task') else None,readonly=permit is not None):
            current(bid,sid,job,permit=permit)
            if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片已变化，本次成片失效')
            result.update(skipped_frames=len(skipped),warnings=warnings,region_count=len(tracks),notice='请预览检查遮挡效果；无告警不代表没有漏遮。')
            update(status='completed',stage='completed',completed_at=now_iso(),result=result,progress='处理后视频已生成，可预览和下载')
    except processing_cancel.Cancelled:
        cancelled=True
    except Exception as exc:
        try:update(status='failed',error=str(exc) if isinstance(exc,cutting.CutError) else '完整视频生成失败，请重试；原片保留')
        except processing_cancel.Cancelled:cancelled=True
    finally:
        try:
            if cancelled:
                with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
                    saved=read_json(path);saved.update(status='cancelled',progress='已停止生成，当前选择已保留');saved.pop('result',None);write_json_atomic(path,saved)
                    for lease in reversed(leases):lease.close()
        finally:
            for lease in reversed(leases):lease.close()


def process_video(source,output,meta,tracks,ranges,progress=lambda text:None,effect=None):
    output=Path(output)
    if tracks or not ranges:return compose(source,output,meta,tracks,progress,effect,ranges=ranges)
    progress('正在删除所选片段并合成视频…')
    actual=cutting.render_video(source,output,ranges,meta)
    digest=actual.pop('sha256')
    return {'metadata':actual,'sha256':digest,'bytes':output.stat().st_size}


def process_preview(source,output,meta,tracks,progress,effect):
    start=min(t['target']['start'] for t in tracks);end=max(t['target']['end'] for t in tracks)
    clip=Path(output).with_name('preview-source.mp4')
    progress('正在准备本片段预览…')
    scale=min(1,640/max(meta['width'],meta['height']))
    width=max(2,round(meta['width']*scale/2)*2);height=max(2,round(meta['height']*scale/2)*2)
    seconds=lambda frame:frame*meta['fps_den']/meta['fps_num']
    video=f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,scale={width}:{height}"
    audio=['-af',f'atrim=start={seconds(start):.9f}:end={seconds(end):.9f},asetpts=PTS-STARTPTS','-c:a','aac'] if meta['has_audio'] else []
    cutting.run(['ffmpeg','-y','-v','error','-i',str(source),'-vf',video,'-map','0:v:0','-map','0:a:0?',
                 '-c:v','libx264','-preset','veryfast','-crf','20','-r',f"{meta['fps_num']}/{meta['fps_den']}",'-fps_mode','cfr',*audio,str(clip)],900)
    clip_meta=cutting.probe_source(clip)
    expected={**meta,'width':width,'height':height,'frame_count':end-start}
    if clip_meta!=expected:raise cutting.CutError('片段预览校验失败，请重试')
    shifted=[]
    for track in tracks:
        target={**track['target']}
        for key in ('start','end','seed_frame'):target[key]-=start
        shifted.append({**track,'target':target})
    try:return compose(clip,output,clip_meta,shifted,progress,effect)
    finally:clip.unlink(missing_ok=True)
