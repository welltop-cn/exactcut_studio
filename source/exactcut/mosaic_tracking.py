"""Bounded local SAM 2 preview jobs, separate from source and cut jobs."""
import os
import re
import subprocess
import sys
import time
import threading
from pathlib import Path
from . import cutting, mosaic_session as sessions, mosaic_locate, cutting_access, workflow, storage
from . import mosaic_effects as effects, processing_cancel, editor_resources, diagnostics
from .util import load_config, read_json, write_json_atomic, json_sha256, file_sha256, now_iso


def settings():
    return load_config().get('cutting', {}).get('tracking', {})


def enabled():
    cfg=settings()
    return cfg.get('enabled') is True and Path(cfg.get('checkpoint','')).is_file()


def validate_target(meta,target):
    keys=('start','end','seed_frame','x','y','w','h')
    if not isinstance(target,dict) or any(type(target.get(k)) is not int for k in keys):
        raise cutting.CutError('跟踪区域必须包含整数帧和像素坐标')
    motion=target.get('motion','tracked')
    if motion not in ('fixed','tracked'):raise cutting.CutError('打码位置模式无效')
    t={k:target[k] for k in keys}
    if motion=='fixed':t['motion']='fixed'
    if not 0<=t['start']<=t['seed_frame']<t['end']<=meta['frame_count']:
        raise cutting.CutError('跟踪参考帧必须在所选片段内')
    if not (0<=t['x']<t['x']+t['w']<=meta['width'] and 0<=t['y']<t['y']+t['h']<=meta['height']):
        raise cutting.CutError('跟踪区域超出画面')
    if motion!='fixed' and ((t['end']-t['start'])*meta['fps_den']>10*meta['fps_num'] or t['end']-t['start']>300):
        raise cutting.CutError('首轮跟踪支持最多 10 秒、300 帧，请先缩短作用范围')
    if 'effect' in target:t['effect']=effects.validate(target['effect'])
    return t


def checked_frames(target,frames,width,height):
    if not isinstance(frames,list) or len(frames)!=target['end']-target['start']:
        raise cutting.CutError('跟踪帧不完整，不能作为有效预览')
    for expected,item in zip(range(target['start'],target['end']),frames):
        if not isinstance(item,dict) or type(item.get('frame')) is not int or item['frame']!=expected:
            raise cutting.CutError('跟踪帧映射无效')
        box=item.get('box')
        if type(item.get('area')) is not int or not 0<=item['area']<=width*height:
            raise cutting.CutError('跟踪遮罩面积无效')
        if box is None:
            if item['area']!=0:raise cutting.CutError('跟踪遮罩无效')
        elif not (isinstance(box,list) and len(box)==4 and all(type(v) is int for v in box) and 0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height and item['area']>0):
            raise cutting.CutError('跟踪遮罩边界无效')
    return frames


def recover(path):
    job=mosaic_locate.recover(path)
    if job and job.get('error')=='上次区域识别已中断，请重新识别':
        job['error']='上次跟踪已中断，请重新跟踪'
        write_json_atomic(path,job)
    return job


def state(bid,sid,jid):
    if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise cutting.CutError('跟踪任务不存在',404)
    path=sessions.session_path(bid,sid)/f'track-{jid}.json'
    with cutting.lock_file(path.parent/'track-state.lock',blocking=True):
        job=recover(path)
        if not job:raise cutting.CutError('跟踪任务不存在',404)
        mosaic_locate.current(bid,sid,job,readonly=True)
        name=job.get('tracking_inference','')
        progress=path.parent/f'track-{jid}'/'progress.json'
        if isinstance(name,str) and re.fullmatch(r'inference-[0-9a-f]{32}(?:-work-[0-9a-f]{32})?',name):progress=path.parent/name/'progress.json'
        if job['status']=='running' and not job.get('cancel_requested') and job.get('stage')!='composing' and progress.is_file():
            p=read_json(progress)
            job={**job,'progress':f"SAM 2 跟踪 {p['frames_done']} / {p['frames_total']} 帧"}
        return job


def stop(bid,sid,jid,reset_at):
    session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
    if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise cutting.CutError('跟踪任务不存在',404)
    path=sessions.session_path(bid,sid)/f'track-{jid}.json'
    with cutting.lock_file(path.parent/'track-state.lock',blocking=True):
        job=recover(path)
        if not job:raise cutting.CutError('跟踪任务不存在',404)
        mosaic_locate.current(bid,sid,job,readonly=True)
        if job['status']=='running':
            job.update(cancel_requested=True,progress='正在停止预览…');write_json_atomic(path,job)
        return job


def preview_file(bid,sid,jid):
    job=state(bid,sid,jid)
    if job['status']!='completed':raise cutting.CutError('跟踪预览尚未完成',409)
    path=sessions.session_path(bid,sid)/f'track-{jid}'/'preview.mp4'
    if not path.is_file():raise cutting.CutError('跟踪预览已失效，请重新跟踪',410)
    return path


def inference_identity(session,target,cfg):
    """Raw SAM masks: change this version only when inference semantics change."""
    geometry={k:target[k] for k in ('start','end','seed_frame','x','y','w','h')}
    return json_sha256({'source':session['source_sha256'],'reset':session.get('reset_at',''),'target':geometry,'config':cfg,'inference_engine':1})[:32]


def legacy_inference_identities(session,geometry,cfg):
    # Explicit historical contracts, independent of future rendered-preview versions.
    return [json_sha256({'source':session['source_sha256'],'reset':session.get('reset_at',''),'target':geometry,'config':cfg,'engine':version,'effect':effects.validate(None)})[:32] for version in (6,5)]


def identity(session,target,cfg,effect=None):
    return json_sha256({'source':session['source_sha256'],'reset':session.get('reset_at',''),'target':target,'config':cfg,'engine':6,'effect':effects.validate(effect)})[:32]


def start(bid,sid,target,reset_at,effect=None,*,actor='admin'):
    effect=effects.validate(effect)
    session=sessions.read_session(bid,sid)
    with cutting_access.gate(workflow.folder(bid),session['request_id']):
        cutting.require_reset(session,reset_at)
        cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'])
        target=validate_target(session['metadata'],target)
        if target.get('motion')=='fixed':raise cutting.CutError('固定位置请使用片段效果预览')
        if not enabled():raise cutting.CutError('本机 SAM 2 跟踪尚未配置',503)
        cfg=settings()
        jid=identity(session,target,cfg,effect)
        path=sessions.session_path(bid,sid)/f'track-{jid}.json'
        with cutting.lock_file(path.parent/'track-state.lock',blocking=True):
            previous=recover(path)
            if previous and (previous['status']=='running' or previous['status']=='completed' and not any(f.get('box') is None for f in previous.get('result',{}).get('frames',[])) and (path.parent/f'track-{jid}'/'preview.mp4').is_file()):return previous
            leases=[]
            try:
                leases.append(editor_resources.acquire('preview',actor))
                for lock,shared in [(path.with_suffix('.worker.lock'),False),(cutting.lock_root()/f'{bid}.lock',True)]:
                    leases.append(cutting.lock_file(lock,shared=shared))
                job={'schema_version':1,'id':jid,'status':'running','actor':actor,'source_sha256':session['source_sha256'],'reset_at':reset_at,'target':target,'effect':effect,'created_at':now_iso(),'progress':'SAM 2 正在跟踪并生成预览'}
                write_json_atomic(path,job)
                permit=cutting_access.InteractiveReadPermit.issue(session,job)
            except Exception:
                for lease in reversed(leases):lease.close()
                raise
    try:threading.Thread(target=_work,args=(bid,sid,path,job,cfg,leases,permit),daemon=True).start()
    except Exception:
        for lease in reversed(leases):lease.close()
        raise
    return job


def run_tracker(source,directory,meta,target,cfg):
    storage.require_space(source.parent,1024**3)
    directory.mkdir(exist_ok=True)
    (directory/'progress.json').unlink(missing_ok=True)
    from .media import encoder_limit
    request={'source':str(source),'output':str(directory),'metadata':meta,'target':target,'config':cfg,'encoder_threads':encoder_limit()}
    write_json_atomic(directory/'request.json',request)
    env=os.environ.copy()
    env['PYTHONPATH']=os.pathsep.join([str(Path(__file__).resolve().parents[1]),*cfg.get('dependency_paths',[])])
    with (directory/'worker.log').open('wb') as log:
        result=processing_cancel.run([sys.executable,'-m','exactcut.mosaic_sam2',str(directory/'request.json')],env=env,stdout=log,stderr=log,timeout=900)
    if result.returncode:
        try:
            with (directory/'worker.log').open('rb') as log:
                log.seek(max(0,log.seek(0,2)-8192));tail=log.read(8192).decode('utf-8',errors='replace')
        except OSError:tail='模型进程失败，运行日志不可读'
        diagnostics.event('mosaic.tracker_process_failed',level='error',return_code=result.returncode,
            source_width=meta['width'],source_height=meta['height'],frame_count=target['end']-target['start'],worker_tail=tail)
        raise cutting.CutError('SAM 2 跟踪失败，原片保留；请检查本机模型运行日志')
    result=read_json(directory/'result.json')
    checked_frames(target,result.get('frames'),result['width'],result['height'])
    if not cfg.get('masks_only') and not (directory/'preview.mp4').is_file():raise cutting.CutError('跟踪预览未生成')
    return result


def _work(bid,sid,path,job,cfg,leases,permit=None):
    with processing_cancel.scope(lambda:read_json(path).get('cancel_requested',False),leases=leases), editor_resources.encoding():
        _work_scoped(bid,sid,path,job,cfg,leases,permit)


def _work_scoped(bid,sid,path,job,cfg,leases,permit=None):
    started=time.monotonic()
    def update(**fields):
        with cutting.lock_file(path.parent/'track-state.lock',blocking=True):
            saved=read_json(path);saved.update(fields);write_json_atomic(path,saved)
    try:
        session=mosaic_locate.current(bid,sid,job,permit=permit);source=path.parent/'source.mp4'
        if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片校验失败')
        from . import mosaic_inference
        from .mosaic_sam2 import render_preview
        directory=path.parent/f"track-{job['id']}"
        track=mosaic_inference.prepare(path.parent,session,job['target'],cfg,directory,
            on_tracking=lambda name:update(tracking_inference=name),
            validate=lambda:mosaic_locate.current(bid,sid,job,permit=permit))
        update(stage='composing',progress='正在合成单区域预览…')
        render_preview(source,directory,session['metadata'],job['target'],job.get('effect'),track['directory']/'masks')
        result={**track['result'],'elapsed_seconds':round(time.monotonic()-started,2)}
        with cutting_access.gate(workflow.folder(bid),session['request_id'],readonly=permit is not None):
            mosaic_locate.current(bid,sid,job,permit=permit)
            if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片已变化，跟踪结果失效')
            update(status='completed',completed_at=now_iso(),result=result,progress='预览已生成，请逐段检查遮挡效果')
    except processing_cancel.Cancelled:
        update(status='cancelled',progress='已停止预览，当前选择已保留')
    except Exception as exc:
        update(status='failed',error=str(exc) if isinstance(exc,cutting.CutError) else '跟踪未完成，请重试；原片保留')
    finally:
        for lease in reversed(leases):lease.close()
