"""Whole-video risk discovery and bounded region localization on mosaic snapshots."""
from contextvars import copy_context
import threading
import time
import uuid
from . import cutting, cutting_scan as scan, mosaic_locate as locate, mosaic_session as sessions, cutting_access, workflow, diagnostics
from .util import file_sha256, json_sha256, read_json, write_json_atomic, now_iso

NOTICE='整片抽样发现疑似时间段，再自动抽帧定位区域；可能遗漏短闪帧，候选框不代表整段覆盖，需人工复核。'
MAX_LOCALIZED_EVENTS=8
TIMEOUT=1200


def identity(session,cfg):
    return json_sha256({'source':session['source_sha256'],'reset_at':session.get('reset_at',''),'config':cfg,
                        'scan_prompt':scan.PROMPT,'region_prompt':locate.PROMPT,'region_schema':locate.SCHEMA,'engine':1})


def recover(path):
    if not path.is_file():return None
    job=read_json(path)
    if job['status']=='running':
        try:lease=cutting.lock_file(path.with_suffix('.worker.lock'))
        except cutting.CutError:return job
        lease.close();diagnostics.event('mosaic.scan_interrupted',level='warning',job_id=job['id'],session_id=path.parent.name,batch_id=path.parent.parent.parent.name);job.update(status='failed',error='上次整片识别已中断，请重新识别');write_json_atomic(path,job)
    return job


def state(bid,sid):
    session=sessions.read_session(bid,sid);path=sessions.session_path(bid,sid)/'scan.json'
    with cutting.lock_file(path.parent/'scan-state.lock',blocking=True):
        job=recover(path)
        if job:
            locate.current(bid,sid,job)
            if job['identity']!=identity(session,scan.settings()):job=None
        return {'enabled':scan.enabled(),'report':job,'notice':NOTICE}


def start(bid,sid,reset_at,*,force=False):
    if type(force) is not bool:raise cutting.CutError('重新识别参数必须为布尔值')
    session=sessions.read_session(bid,sid)
    with cutting_access.gate(workflow.folder(bid),session['request_id']):
        session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
        cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'])
        if not scan.enabled():raise cutting.CutError('整片识别未启用；请配置本机识别凭据',503)
        cfg=scan.settings();key=scan.credential();identifier=identity(session,cfg)
        path=sessions.session_path(bid,sid)/'scan.json'
        with cutting.lock_file(path.parent/'scan-state.lock',blocking=True):
            previous=recover(path)
            if previous and previous['identity']==identifier and (previous['status']=='running' or (previous['status']=='completed' and not force)):
                diagnostics.event('mosaic.scan_reused',batch_id=bid,session_id=sid,job_id=previous['id'],status=previous['status'])
                return previous
            leases=[]
            try:
                for target,shared,resource in [(cutting.lock_root()/'scan.lock',False,'scan'),(path.with_suffix('.worker.lock'),False,None),
                                      (cutting.lock_root()/f'{bid}.lock',True,None),(cutting.lock_root()/'compute.lock',False,'compute')]:
                    leases.append(cutting.lock_file(target,shared=shared,resource=resource))
                from .storage import require_space
                require_space(path.parent,512*1024**2)
                job={'schema_version':1,'id':uuid.uuid4().hex,'identity':identifier,'status':'running',
                     'source_sha256':session['source_sha256'],'reset_at':session.get('reset_at',''),'model':cfg['model'],
                     'events':[],'warnings':[],'progress':'正在准备整片识别','notice':NOTICE,'created_at':now_iso()}
                write_json_atomic(path,job)
            except Exception as exc:
                diagnostics.event('mosaic.scan_admission_failed',level='warning',batch_id=bid,session_id=sid,error=diagnostics.private_exception(exc))
                for lease in reversed(leases):lease.close()
                raise
    try:threading.Thread(target=copy_context().run,args=(_work,bid,sid,path,job,cfg,key,leases),daemon=True).start()
    except Exception as exc:
        diagnostics.event('mosaic.scan_spawn_failed',level='error',batch_id=bid,session_id=sid,job_id=job['id'],error=diagnostics.private_exception(exc))
        for lease in reversed(leases):lease.close()
        raise
    return job


def _work(bid,sid,path,job,cfg,key,leases):
    started=time.monotonic();phase='source_validate'
    diagnostics.bind(batch_id=bid,session_id=sid,job_id=job['id'],model=cfg['model'])
    diagnostics.event('mosaic.scan_started',duration_ms=0,timeout_ms=cfg['request_timeout_ms'])
    def update(**fields):
        with cutting.lock_file(path.parent/'scan-state.lock',blocking=True):
            saved=read_json(path)
            if saved['id']!=job['id']:return
            saved.update(fields);write_json_atomic(path,saved)
    try:
        deadline=time.monotonic()+TIMEOUT
        session=locate.current(bid,sid,job);source=path.parent/'source.mp4'
        diagnostics.bind(request_id=session['request_id'])
        if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片校验失败，未进行识别')
        phase='video_detect'
        result=scan.detect(source,session['metadata'],{**cfg,'max_refine_events':0},'standard',key,lambda message:update(progress=message),leases[-1],deadline=deadline)
        diagnostics.event('mosaic.scan_detected',event_count=len(result['risk_events']))
        phase='localization'
        locate.current(bid,sid,job)
        events=[];warnings=list(result['warnings'])
        for i,risk in enumerate(result['risk_events']):
            event={'id':str(i),'start':risk['start_frame'],'end':risk['end_frame'],'label':risk['category'],
                   'reason':risk['evidence_zh'],'status':'needs_review','candidates':[],
                   'review_note':risk.get('review_note','')}
            event['review_note']='等待自动定位；可先检查时间段。'
            events.append(event)
        update(events=events,warnings=warnings,progress=f'已发现 {len(events)} 个疑似时间段，正在补充区域')
        for i,event in enumerate(events):
            notes=[]
            if i>=MAX_LOCALIZED_EVENTS:
                diagnostics.event('mosaic.localization_skipped',event_id=event['id'],reason='event_limit')
                notes.append('超出本次自动定位上限，保留时间段供人工复核。')
            else:
                start,end=event['start'],event['end'];frames=list(dict.fromkeys([(start+end-1)//2,start,end-1]))
                for frame in frames:
                    if time.monotonic()+60+cfg['request_timeout_ms']/1000>deadline:
                        diagnostics.event('mosaic.localization_skipped',event_id=event['id'],reason='deadline')
                        notes.append('整体识别时间已达上限，区域定位未完成。');break
                    update(progress=f'正在定位第 {i+1}/{min(len(result["risk_events"]),MAX_LOCALIZED_EVENTS)} 个疑似区间')
                    attempt_started=time.monotonic();phase='frame_extract'
                    try:
                        locate.current(bid,sid,job)
                        with diagnostics.span('mosaic.frame_extract',private_error=True,event_id=event['id'],frame=frame):
                            jpeg=sessions.frame_file(bid,sid,frame).read_bytes()
                        phase='region_detect'
                        candidates=locate.detect(jpeg,session['metadata'],cfg,key,deadline=deadline)
                        diagnostics.event('mosaic.localization_completed',event_id=event['id'],frame=frame,candidate_count=len(candidates),duration_ms=round((time.monotonic()-attempt_started)*1000))
                        if candidates:
                            event.update(status='localized',seed_frame=frame,candidates=candidates)
                            notes.append('候选框仅对应自动抽样帧，不代表整个时间段覆盖。');break
                    except Exception as exc:
                        diagnostics.event('mosaic.localization_failed',level='warning',event_id=event['id'],frame=frame,phase=phase,
                                          duration_ms=round((time.monotonic()-attempt_started)*1000),error=diagnostics.private_exception(exc))
                        notes.append('代表帧定位失败，需人工复核。')
                if not event['candidates']:notes.append('未获得有效区域框；时间段仍保留，不能视作无风险。')
            if event['status']=='needs_review' or any('失败' in n for n in notes):warnings.append(f'第 {i+1} 个疑似区间区域定位未完整确认')
            event['review_note']=' '.join(notes)
            locate.current(bid,sid,job)
            update(events=events,warnings=warnings)
        phase='final_validate'
        with cutting_access.gate(workflow.folder(bid),session['request_id']):
            locate.current(bid,sid,job)
            if identity(session,scan.settings())!=job['identity']:raise cutting.CutError('识别配置已变化，请重新识别',409)
            if file_sha256(source)!=job['source_sha256']:raise cutting.CutError('原片校验失败，结果已失效')
            update(status='completed',events=events,warnings=warnings,completed_at=now_iso(),progress='整片识别完成，请人工复核')
        diagnostics.event('mosaic.scan_completed',duration_ms=round((time.monotonic()-started)*1000),event_count=len(events),
                          localized_count=sum(bool(e['candidates']) for e in events),warning_count=len(warnings))
    except Exception as exc:
        # Provider exception text can contain response bodies; retain only metadata.
        error=diagnostics.private_exception(exc)
        diagnostics.event('mosaic.scan_failed',level='error',batch_id=bid,session_id=sid,
                          job_id=job['id'],model=cfg['model'],phase=phase,duration_ms=round((time.monotonic()-started)*1000),error=error)
        update(status='failed',error=str(exc) if isinstance(exc,cutting.CutError) else '整片识别失败，请重试；原成片未修改')
    finally:
        for lease in reversed(leases):lease.close()
