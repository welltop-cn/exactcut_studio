"""Cold exact-frame requests join the short-media queue; cache hits are immediate."""
import os
from . import cutting,interactive_queue,cutting_access
from .util import json_sha256,read_json,now_iso


def source_module(family):
    if family=='cuts':return cutting
    from . import mosaic_session
    return mosaic_session


def handle(handler,family,bid,sid,index):
    module=source_module(family);session=module.read_session(bid,sid)
    if type(index) is not int or not 0<=index<session['metadata']['frame_count']:raise cutting.CutError('帧号超出视频范围')
    root=module.session_path(bid,sid);cached=root/'frames'/(str(index)+'.jpg')
    try:
        data=cached.read_bytes()
        if data:
            try:os.utime(cached,None)
            except FileNotFoundError:pass
            return handler.respond(200,data,'image/jpeg')
    except FileNotFoundError:pass
    jid=json_sha256([sid,index,'frame-window-v1'])[:32];path=root/'frame-tasks'/(jid+'.json')
    with cutting.lock_file(path.parent/'state.lock',blocking=True):
        previous=read_json(path) if path.is_file() else None
        if previous and previous['status']=='failed' and handler.headers.get('X-Exactcut-Frame-Retry')!='1':
            return handler.respond(400,{'error':previous.get('error','准确帧生成失败，请重试')})
        job={'schema_version':1,'id':jid,'status':'queued','actor':handler.user['username'],'source_sha256':session['source_sha256'],
             'reset_at':session.get('reset_at',''),'index':index,'created_at':now_iso(),'progress':'准确帧已排队，轮到后自动读取'}
        entry=interactive_queue.enqueue('cut_frame' if family=='cuts' else 'mosaic_frame',session,path,job,{},state_lock=path.parent/'state.lock')
        current=read_json(path)
    return handler.respond(202,current)


def run(entry,leases):
    module=source_module('cuts' if entry['kind']=='cut_frame' else 'mosaics')
    bid=entry['session']['batch_id'];sid=entry['session']['id'];job=read_json(interactive_queue.report(entry))
    permit=cutting_access.InteractiveReadPermit(**entry['permit'])
    permit.require(module.read_session(bid,sid),job)
    module.frame_file(bid,sid,job['index'])
    permit.require(module.read_session(bid,sid),job)
    interactive_queue._update(entry,status='completed',completed_at=now_iso(),progress='准确帧已就绪')
