"""Independent authenticated region-recognition workspace routes."""
import json
import re
from . import cutting, mosaic_session as sessions, mosaic_locate, mosaic_scan, mosaic_tracking, mosaic_render, cutting_access, workflow
from .runtime import maintenance_file


def handle(handler,bid,suffix,method):
    if not cutting.enabled():return handler.respond(404,{'error':'打码功能未启用'})
    if not handler.authorized():return
    if not handler.can_access(bid):return handler.respond(404,{'error':'项目不存在或无权访问'})
    data={}
    if method=='POST':
        if handler.headers.get('X-Exactcut-Request')!='1':return handler.respond(403,{'error':'无效请求来源'})
        if maintenance_file().exists():return handler.respond(503,{'error':'系统正在维护，请稍后重试'})
        try:
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=65536:raise ValueError()
            data=json.loads(handler.rfile.read(size))
            if not isinstance(data,dict):raise ValueError()
        except (ValueError,UnicodeError):return handler.respond(400,{'error':'打码请求格式或大小无效'})
    try:
        with cutting.batch_lease(bid):
            if not handler.can_access(bid):raise cutting.CutError('项目不存在或无权访问',404)
            if method=='POST' and suffix=='open':
                from . import editor_preparation
                value=editor_preparation.submit('mosaics',bid,data.get('request_id'),data.get('source_sha256'),handler.user['username'])
                return handler.respond(202 if value.get('status') in {'queued','running'} else 200,value)
            preparation=re.fullmatch(r'preparation/([0-9a-f]{32})',suffix)
            if method=='GET' and preparation:
                from . import editor_preparation
                return handler.respond(200,editor_preparation.state('mosaics',bid,preparation[1]))
            match=re.fullmatch(r'([0-9a-f]{32})(?:/(source|source-url|scan|discard|discard-result|replace|stop|clip-preview|frame/[0-9]+|locate(?:/[0-9a-f]{32})?|track(?:/[0-9a-f]{32}(?:/(?:preview|stop))?)?|render(?:/[0-9a-f]{32}(?:/video)?)?))?',suffix)
            if not match:raise cutting.CutError('打码入口不存在',404)
            sid,action=match.groups();session=sessions.read_session(bid,sid)
            if action!='replace' and not (method=='GET' and action and re.fullmatch(r'render/[0-9a-f]{32}',action)):
                cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'],readonly=True)
            track_stop=bool(action and re.fullmatch(r'track/[0-9a-f]{32}/stop',action))
            if method=='POST' and action not in {'render','replace','stop','discard-result'} and not track_stop:
                cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'])
            if method=='GET':
                if action is None:return handler.respond(200,sessions.get_session(bid,sid))
                if action=='render':return handler.respond(200,mosaic_render.latest(bid,sid))
                if action and action.startswith('render/'):
                    parts=action.split('/')
                    if len(parts)==3:return handler.stream_video(mosaic_render.video_file(bid,sid,parts[1]))
                    return handler.respond(200,mosaic_render.state(bid,sid,parts[1]))
                if action and action.startswith('track/'):
                    parts=action.split('/')
                    if len(parts)==3:
                        if parts[2]!='preview':raise cutting.CutError('跟踪入口不存在',404)
                        return handler.stream_video(mosaic_tracking.preview_file(bid,sid,parts[1]))
                    return handler.respond(200,mosaic_tracking.state(bid,sid,parts[1]))
                if action=='scan':return handler.respond(200,mosaic_scan.state(bid,sid))
                if action=='source-url':
                    from . import editor_source
                    return handler.respond(200,editor_source.resolve(bid,session,f'/api/batches/{bid}/mosaics/{sid}/source'))
                if action=='source':return handler.stream_video(sessions.session_path(bid,sid)/'source.mp4')
                if action.startswith('frame/'):
                    from . import editor_frames
                    return editor_frames.handle(handler,'mosaics',bid,sid,int(action[6:]))
                if action.startswith('locate/'):return handler.respond(200,mosaic_locate.state(bid,sid,action[7:]))
            if method=='POST' and track_stop:
                return handler.respond(202,mosaic_tracking.stop(bid,sid,action.split('/')[1],data.get('reset_at','')))
            if method=='POST' and action=='discard-result':
                return handler.respond(200,mosaic_render.discard_result(bid,sid,data.get('job_id'),data.get('reset_at','')))
            if method=='POST' and action=='stop':
                return handler.respond(202,mosaic_render.stop(bid,sid,data.get('job_id'),data.get('reset_at','')))
            if method=='POST' and action=='clip-preview':
                return handler.respond(202,mosaic_render.start(bid,sid,data.get('targets'),data.get('reset_at',''),effect=data.get('effect'),preview=True,actor=handler.user['username']))
            if method=='POST' and action=='render':
                return handler.respond(202,mosaic_render.start(bid,sid,data.get('targets'),data.get('reset_at',''),data.get('ranges'),data.get('effect'),actor=handler.user['username'],submission_id=data.get('submission_id')))
            if method=='POST' and action=='replace':
                from . import mosaic_replace
                return handler.respond(202,mosaic_replace.start(bid,sid,data.get('job_id'),data.get('reset_at','')))
            if method=='POST' and action=='discard':return handler.respond(200,sessions.discard(bid,sid,data.get('reset_at','')))
            if method=='POST' and action=='track':
                return handler.respond(202,mosaic_tracking.start(bid,sid,data.get('target'),data.get('reset_at',''),data.get('effect'),actor=handler.user['username']))
            if method=='POST' and action=='scan':
                return handler.respond(202,mosaic_scan.start(bid,sid,data.get('reset_at',''),force=data.get('force',False),actor=handler.user['username']))
            if method=='POST' and action=='locate':
                return handler.respond(202,mosaic_locate.start(bid,sid,data.get('start'),data.get('end'),data.get('seed_frame'),data.get('reset_at',''),actor=handler.user['username']))
            raise cutting.CutError('打码入口不存在',404)
    except cutting.CutError as exc:return cutting.respond_error(handler, exc)
    except (ValueError,OSError,RuntimeError):return handler.respond(400,{'error':'打码文件或处理资源暂不可用，请刷新后重试'})
