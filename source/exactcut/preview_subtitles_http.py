"""Authenticated preview subtitle adapter, outside the global web mutation lock."""
import json
from contextlib import nullcontext
from urllib.parse import unquote

from . import cutting, preview_subtitle_queue, preview_subtitles as subtitles
from .runtime import maintenance_file, mutation_lock


def handle(handler,batch_id,request_id,method):
    if not handler.authorized():
        return
    try:
        if not subtitles.enabled():
            return handler.respond(404,{'error':'预览中文字幕未启用'})
    except (ValueError,TypeError,OSError):
        subtitles.available()  # Records only the exception type, never private config values.
        return handler.respond(503,{'error':'字幕配置暂不可用，请检查本机开发配置'})
    if not handler.can_access(batch_id):
        return handler.respond(404,{'error':'项目不存在或无权访问'})
    if method == 'POST':
        if handler.headers.get('X-Exactcut-Request') != '1':
            return handler.respond(403,{'error':'无效请求来源'})
        if maintenance_file().exists():
            return handler.respond(503,{'error':'系统正在维护，请稍后重试'})
        try:
            size = int(handler.headers.get('Content-Length','0'))
            if not 0 < size <= 65536:
                raise ValueError()
            data = json.loads(handler.rfile.read(size))
            if not isinstance(data,dict):raise ValueError()
        except (ValueError,UnicodeError):
            return handler.respond(400,{'error':'字幕请求格式或大小无效'})
        sha,version = data.get('source_sha256'),data.get('version_id')
    else:
        sha = handler.query.get('source_sha256',[None])[0]
        version = handler.query.get('version_id',[''])[0] or None
    try:
        with mutation_lock() if method == 'POST' else nullcontext():
            if method == 'POST' and maintenance_file().exists():
                return handler.respond(503,{'error':'系统正在维护，请稍后重试'})
            with cutting.batch_lease(batch_id):
                if not handler.can_access(batch_id):
                    return handler.respond(404,{'error':'项目不存在或无权访问'})
                if method == 'POST':
                    # The queued actor is the authenticated session, never a request field.
                    result = subtitles.start(batch_id,unquote(request_id),sha,version,handler.user['username'])
                    return handler.respond(202 if result['report']['status'] in {'queued','running'} else 200,result)
                return handler.respond(200,subtitles.state(batch_id,unquote(request_id),sha,version))
    except subtitles.SubtitleError as exc:
        return handler.respond(exc.status,{'error':str(exc)})
    except preview_subtitle_queue.QueueError as exc:
        return handler.respond(exc.status,{'error':str(exc)})
    except cutting.CutError:
        return handler.respond(429,{'error':'素材正在处理其他操作，请稍后点击重试'})
    except (ValueError,OSError,RuntimeError):
        return handler.respond(503,{'error':'字幕处理资源暂不可用，请检查本机配置或稍后重试'})
