"""Independent authenticated region-recognition workspace routes."""
import json
import re
from . import cutting, mosaic_session as sessions, mosaic_locate, mosaic_scan, cutting_access, workflow
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
            if method=='POST' and suffix=='open':return handler.respond(200,sessions.open_session(bid,data.get('request_id'),data.get('source_sha256')))
            match=re.fullmatch(r'([0-9a-f]{32})(?:/(source|scan|frame/[0-9]+|locate(?:/[0-9a-f]{32})?))?',suffix)
            if not match:raise cutting.CutError('打码入口不存在',404)
            sid,action=match.groups();session=sessions.read_session(bid,sid)
            cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'])
            if method=='GET':
                if action is None:return handler.respond(200,sessions.get_session(bid,sid))
                if action=='scan':return handler.respond(200,mosaic_scan.state(bid,sid))
                if action=='source':return handler.stream_video(sessions.session_path(bid,sid)/'source.mp4')
                if action.startswith('frame/'):return handler.respond(200,sessions.frame_file(bid,sid,int(action[6:])).read_bytes(),'image/jpeg')
                if action.startswith('locate/'):return handler.respond(200,mosaic_locate.state(bid,sid,action[7:]))
            if method=='POST' and action=='scan':
                return handler.respond(202,mosaic_scan.start(bid,sid,data.get('reset_at',''),force=data.get('force',False)))
            if method=='POST' and action=='locate':
                return handler.respond(202,mosaic_locate.start(bid,sid,data.get('start'),data.get('end'),data.get('seed_frame'),data.get('reset_at','')))
            raise cutting.CutError('打码入口不存在',404)
    except cutting.CutError as exc:return handler.respond(exc.status,{'error':str(exc)})
    except (ValueError,OSError,RuntimeError):return handler.respond(400,{'error':'打码文件或处理资源暂不可用，请刷新后重试'})
