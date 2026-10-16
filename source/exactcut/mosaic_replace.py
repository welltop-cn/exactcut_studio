"""Promote validated unified processing results through the shared media commit."""
import threading
from . import cutting, cutting_access, cutting_replace as replace, mosaic_session as sessions, workflow, preview_oss
from .util import read_json, write_json_atomic, now_iso


def contract(job):
    return {**job,'output_sha256':job['result']['sha256'],'expected_frames':job['result']['metadata']['frame_count'],
            'validation_method':'video-processing-validated'}


def is_current(bid,session,job):
    return job.get('status')=='completed' and replace.current_is_job(workflow.folder(bid),session,contract(job))


def update(bid,sid,jid,result):
    root=sessions.session_path(bid,sid)
    with cutting.lock_file(root/'render-state.lock',blocking=True):
        path=root/f'render-{jid}.json';job=read_json(path);job['replacement']=result
        write_json_atomic(path,job);return job


def recover(bid,sid,job):
    if job.get('replacement',{}).get('status')!='running':return job
    session=sessions.read_session(bid,sid)
    try:
        with cutting_access.gate(workflow.folder(bid),session['request_id']):
            result={'status':'completed'} if is_current(bid,session,job) else {'status':'failed','error':'替换已中断，请重试；当前成片保留。'}
            job['replacement']=result
            write_json_atomic(sessions.session_path(bid,sid)/f"render-{job['id']}.json",job)
    except cutting.CutError:pass
    return job


def start(bid,sid,jid,reset_at):
    from . import mosaic_render
    session=sessions.read_session(bid,sid);cutting.require_reset(session,reset_at)
    job=mosaic_render.state(bid,sid,jid)
    if job['status']!='completed':raise cutting.CutError('请先生成处理后的视频')
    if is_current(bid,session,job):return update(bid,sid,jid,{'status':'completed'})
    batch=workflow.folder(bid);guard=cutting_access.gate(batch,session['request_id']);guard.__enter__();lease=None
    try:
        cutting.require_reset(sessions.read_session(bid,sid),reset_at)
        cutting_access.require_editable(batch,session['request_id'],source_sha=session['source_sha256'])
        lease=cutting.lock_file(cutting.lock_root()/f'{bid}.lock',shared=True)
        result=update(bid,sid,jid,{'status':'running','started_at':now_iso()})
        threading.Thread(target=run,args=(bid,sid,session,job,guard,lease),daemon=True).start()
        return result
    except Exception:
        if lease:lease.close()
        guard.__exit__(None,None,None);raise


def run(bid,sid,session,job,guard,lease):
    try:
        batch=workflow.folder(bid);source=sessions.session_path(bid,sid)/f"render-{job['id']}"/'mosaic.mp4'
        prepared=replace.prepare(batch,session,contract(job),source=source)
        cutting.require_reset(sessions.read_session(bid,sid),job['reset_at'])
        replace.commit(batch,session,contract(job),*prepared)
        result={'status':'completed','completed_at':now_iso(),'storage':'oss' if preview_oss.enabled() else 'local'}
    except Exception:
        try:committed=is_current(bid,session,job)
        except (ValueError,OSError,KeyError):committed=False
        result={'status':'completed'} if committed else {'status':'failed','error':'替换失败，当前成片未改变，请重试。'}
    try:update(bid,sid,job['id'],result)
    finally:
        lease.close();guard.__exit__(None,None,None)
