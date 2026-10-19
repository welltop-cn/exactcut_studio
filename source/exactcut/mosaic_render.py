"""Full-length mosaic artifacts from a frozen set of adopted regions."""
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


def compose(source,output,meta,tracks,progress=lambda text:None,effect=None):
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
    masks=[];appearances=[]
    for index,(appearance,group) in enumerate(groups.values()):
        mask_video=output.parent/f'combined-mask-{index}.mkv'
        encode_mask(mask_video,meta,group,size,rate,appearance,progress)
        masks.append(mask_video);appearances.append(appearance)
    progress('正在合成完整视频并保留原音频…')
    w,h=meta['width'],meta['height']
    # RGB planar masks avoid limited-range luma turning white masks into partial blends.
    graph=effects.layered_graph(meta,appearances)
    cutting.run(['ffmpeg','-y','-v','error','-i',str(source),*[arg for mask in masks for arg in ('-i',str(mask))],'-filter_complex',graph,
                 '-map','[out]','-map','0:a:0?','-c:v','libx264','-crf','18','-preset','fast','-c:a','copy','-movflags','+faststart',str(partial)],900)
    progress('正在校验完整视频的帧数、分辨率和音频…')
    actual=cutting.probe_source(partial)
    if actual!=meta:raise cutting.CutError('完整视频校验未通过，原片保留')
    partial.replace(output)
    for mask in masks:mask.unlink(missing_ok=True)
    return {'metadata':actual,'sha256':file_sha256(output),'bytes':output.stat().st_size}


def encode_mask(mask_video,meta,tracks,size,rate,effect,progress):
    width,height=size
    progress('正在合并全部区域遮罩…')
    with (mask_video.with_suffix('.log')).open('wb') as log:
        process=subprocess.Popen(bounded_ffmpeg(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','gray','-s',f'{width}x{height}','-r',rate,'-i','pipe:0','-an','-c:v','ffv1',str(mask_video)]),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log)
        timer=threading.Timer(600,process.kill);timer.start()
        try:
            black=bytes(width*height)
            for frame in range(meta['frame_count']):
                processing_cancel.check()
                active=any(t['target']['start']<=frame<t['target']['end'] for t in tracks)
                process.stdin.write(effects.prepare_mask(union_mask(frame,tracks,size),effect).tobytes() if active else black)
                if frame%120==0:progress(f"合并遮罩 {frame+1} / {meta['frame_count']} 帧")
            process.stdin.close()
            if process.wait(timeout=60):raise cutting.CutError('遮罩视频生成失败，未生成成片')
        except Exception:
            process.kill();process.wait()
            raise
        finally:timer.cancel()

def recover(path):
    if not path.is_file():return None
    job=read_json(path)
    if job['status']=='running':
        try:lease=cutting.lock_file(path.with_suffix('.worker.lock'))
        except cutting.CutError:return job
        lease.close();job.update(status='failed',error='上次完整视频生成已中断，请重新生成')
        write_json_atomic(path,job)
    return job


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
        if not mosaic_replace.is_current(bid,session,job):mosaic_locate.current(bid,sid,job)
        return job


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


def start(bid,sid,targets,reset_at,ranges=None,effect=None,preview=False):
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
        if targets and not tracking.enabled():raise cutting.CutError('本机 SAM 2 跟踪尚未配置',503)
        cfg=tracking.settings()
        jid=json_sha256({'source':session['source_sha256'],'reset':reset_at,'targets':targets,'ranges':ranges,'config':cfg,'engine':5,'effect':effect,'fallback':'skip_region',**({'preview':True,'preview_engine':2} if preview else {})})[:32]
        path=sessions.session_path(bid,sid)/f'render-{jid}.json'
        with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
            old=recover(path)
            if old and (old['status']=='running' or old['status']=='completed' and (path.parent/f'render-{jid}'/'mosaic.mp4').is_file()):
                if not preview:write_json_atomic(path.parent/'render-latest.json',{'id':jid})
                return old
            leases=[]
            try:
                for lock,shared in [(cutting.lock_root()/'compute.lock',False),(path.with_suffix('.worker.lock'),False),(cutting.lock_root()/f'{bid}.lock',True)]:
                    leases.append(cutting.lock_file(lock,shared=shared))
                job={'schema_version':1,'id':jid,'status':'running','stage':'tracking','source_sha256':session['source_sha256'],'reset_at':reset_at,
                     'targets':targets,'ranges':ranges,'effect':effect,'preview':preview,'created_at':now_iso(),'progress':f'准备删除 {len(ranges)} 段、打码 {len(targets)} 个区域','completed_regions':0,'total_regions':len(targets)}
                write_json_atomic(path,job)
                if not preview:write_json_atomic(path.parent/'render-latest.json',{'id':jid})
            except Exception:
                for lease in reversed(leases):lease.close()
                raise
    try:threading.Thread(target=_work,args=(bid,sid,path,job,cfg,leases),daemon=True).start()
    except Exception:
        for lease in reversed(leases):lease.close()
        raise
    return job


def stop(bid,sid,jid,reset_at):
    session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
    if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise cutting.CutError('生成任务不存在',404)
    path=sessions.session_path(bid,sid)/f'render-{jid}.json'
    with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
        job=recover(path)
        if not job:raise cutting.CutError('生成任务不存在',404)
        mosaic_locate.current(bid,sid,job)
        if job['status']=='running':
            job.update(cancel_requested=True,progress='正在停止生成…')
            write_json_atomic(path,job)
        return job


def prepare_track(bid,sid,session,target,cfg,directory):
    # compute.lock is already held by this batch. Never recursively enqueue a single job.
    jid=tracking.identity(session,target,cfg);root=sessions.session_path(bid,sid)
    geometry={k:target[k] for k in ('start','end','seed_frame','x','y','w','h')}
    inference=root/f'inference-{tracking.identity(session,geometry,cfg)}'
    if (inference/'result.json').is_file():
        result=read_json(inference/'result.json')
        tracking.checked_frames(target,result.get('frames'),result['width'],result['height'])
        if all((inference/'masks'/f'{i:06d}.png').is_file() for i in range(target['end']-target['start'])):
            return {'target':target,'directory':inference,'result':result}
    cached=root/f'track-{jid}.json';cache_dir=root/f'track-{jid}'
    if cached.is_file():
        previous=read_json(cached)
        if previous['status']=='completed' and (cache_dir/'result.json').is_file():
            result=read_json(cache_dir/'result.json')
            tracking.checked_frames(target,result.get('frames'),result['width'],result['height'])
            if all((cache_dir/'masks'/f'{i:06d}.png').is_file() for i in range(target['end']-target['start'])):
                return {'target':target,'directory':cache_dir,'result':result}
    # A retry of this batch can reuse its own completed per-target inference.
    if (directory/'result.json').is_file():
        result=read_json(directory/'result.json')
        tracking.checked_frames(target,result.get('frames'),result['width'],result['height'])
        if all((directory/'masks'/f'{i:06d}.png').is_file() for i in range(target['end']-target['start'])):
            return {'target':target,'directory':directory,'result':result}
    # Reuse verified inference from earlier render/preview jobs made before the shared cache.
    for job_path in root.glob('render-*.json'):
        previous=read_json(job_path)
        if previous.get('source_sha256')!=session['source_sha256'] or previous.get('reset_at','')!=session.get('reset_at',''):continue
        for request_path in (root/job_path.stem).glob('target-*/request.json'):
            request=read_json(request_path)
            if request.get('config')!=cfg or request.get('metadata')!=session['metadata']:continue
            if request.get('source')!=str(root/'source.mp4'):continue
            if {k:request.get('target',{}).get(k) for k in geometry}!=geometry:continue
            legacy=request_path.parent
            if not (legacy/'result.json').is_file():continue
            result=read_json(legacy/'result.json')
            tracking.checked_frames(target,result.get('frames'),result['width'],result['height'])
            if all((legacy/'masks'/f'{i:06d}.png').is_file() for i in range(target['end']-target['start'])):
                return {'target':target,'directory':legacy,'result':result}
    result=tracking.run_tracker(root/'source.mp4',inference,session['metadata'],geometry,{**cfg,'masks_only':True})
    return {'target':target,'directory':inference,'result':result}


def _work(bid,sid,path,job,cfg,leases):
    with processing_cancel.scope(lambda:read_json(path).get('cancel_requested',False)):
        _work_cancelable(bid,sid,path,job,cfg,leases)


def _work_cancelable(bid,sid,path,job,cfg,leases):
    cancelled=False
    def update(**fields):
        with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
            saved=read_json(path)
            if saved.get('cancel_requested'):raise processing_cancel.Cancelled()
            saved.update(fields);write_json_atomic(path,saved)
    try:
        session=mosaic_locate.current(bid,sid,job);source=path.parent/'source.mp4';directory=path.parent/f"render-{job['id']}"
        if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片校验失败')
        storage.require_space(source.parent,source.stat().st_size*3+2*1024**3)
        directory.mkdir(exist_ok=True);tracks=[];warnings=[];skipped=set()
        for index,target in enumerate(job['targets']):
            mosaic_locate.current(bid,sid,job)
            update(progress=f"正在跟踪第 {index+1} / {len(job['targets'])} 个区域",completed_regions=index)
            try:
                track=prepare_track(bid,sid,session,target,cfg,directory/f'target-{index}')
                for frame in track['result']['frames']:
                    if frame['box'] is None:skipped.add(frame['frame'])
                for warning in track['result'].get('warnings',[]):warnings.append({'region':index+1,**warning})
                empty=sum(f['box'] is None for f in track['result']['frames'])
                if empty:warnings.append({'region':index+1,'frame':target['start'],'reason':f'{empty} 帧失跟，已跳过该区域'})
            except (cutting.CutError,subprocess.TimeoutExpired):
                track={'target':target,'failed':True}
                skipped.update(range(target['start'],target['end']))
                warnings.append({'region':index+1,'frame':target['start'],'reason':'跟踪未完成，已跳过该区域'})
            tracks.append(track)
        update(stage='composing',completed_regions=len(tracks))
        mosaic_locate.current(bid,sid,job)
        if job.get('preview'):
            result=process_preview(source,directory/'mosaic.mp4',session['metadata'],tracks,lambda text:update(progress=text),job.get('effect'))
        else:
            result=process_video(source,directory/'mosaic.mp4',session['metadata'],tracks,job.get('ranges',[]),lambda text:update(progress=text),effect=job.get('effect'))
        with cutting_access.gate(workflow.folder(bid),session['request_id']):
            mosaic_locate.current(bid,sid,job)
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
    if not ranges:return compose(source,output,meta,tracks,progress,effect)
    intermediate=output.with_name('masked-source.mp4')
    if tracks:compose(source,intermediate,meta,tracks,progress,effect)
    progress('正在删除所选片段并合成视频…')
    actual=cutting.render_video(intermediate if tracks else source,output,ranges,meta)
    intermediate.unlink(missing_ok=True)
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
