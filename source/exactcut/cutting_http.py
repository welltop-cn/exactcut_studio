"""Small authenticated HTTP adapter; never holds the web mutation lock."""
import json
import re
from urllib.parse import unquote
from . import cutting
from .runtime import maintenance_file


def handle(handler, batch_id, suffix, method):
    if not cutting.enabled():
        return handler.respond(404, {'error': '剪切功能未启用'})
    if not handler.authorized():
        return
    if not handler.can_access(batch_id):
        return handler.respond(404, {'error': '项目不存在或无权访问'})
    data = {}
    if method == 'POST':
        if handler.headers.get('X-Exactcut-Request') != '1':
            return handler.respond(403, {'error': '无效请求来源'})
        if maintenance_file().exists():
            return handler.respond(503, {'error': '系统正在维护，请稍后重试'})
        try:
            size = int(handler.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError()
            data = json.loads(handler.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return handler.respond(400, {'error': '剪切请求格式或大小无效'})
    try:
        with cutting.batch_lease(batch_id):
            # Revalidate after acquiring a lease against concurrent deletion.
            if not handler.can_access(batch_id):
                raise cutting.CutError('项目不存在或无权访问', 404)
            if method == 'POST' and suffix == 'open':
                return handler.respond(200, cutting.open_session(batch_id, data.get('request_id'), data.get('source_sha256')))
            if method == 'GET' and suffix.startswith('saved/'):
                return handler.respond(200, {'versions': cutting.saved_versions(batch_id, unquote(suffix[6:]))})
            scan_route = re.fullmatch(r'([0-9a-f]{32})/scan(?:/(standard|strict))?', suffix)
            if scan_route:
                from . import cutting_scan
                sid, policy = scan_route.groups()
                from . import cutting_access, workflow
                scan_session = cutting.read_session(batch_id, sid)
                cutting_access.require_editable(workflow.folder(batch_id), scan_session['request_id'], source_sha=scan_session['source_sha256'])
                if method == 'GET' and policy:
                    return handler.respond(200, cutting_scan.state(batch_id, sid, policy))
                if method == 'POST' and policy is None:
                    with cutting_access.gate(workflow.folder(batch_id), scan_session['request_id']):
                        cutting_access.require_editable(workflow.folder(batch_id), scan_session['request_id'], source_sha=scan_session['source_sha256'])
                        cutting.require_reset(cutting.read_session(batch_id, sid), data.get('reset_at', ''))
                        return handler.respond(202, cutting_scan.start(batch_id, sid, data.get('policy', 'standard'), force=data.get('force', False)))
                raise cutting.CutError('识别入口不存在', 404)
            match = re.fullmatch(r'([0-9a-f]{32})(?:/(source|source-url|frame/[0-9]+|output/[0-9a-f]{32}|render|save|replace|discard))?', suffix)
            if not match:
                raise cutting.CutError('剪切入口不存在', 404)
            sid, action = match.groups()
            session = cutting.read_session(batch_id, sid)
            from . import cutting_access, workflow
            # Polling a completed replacement is needed to acknowledge the atomic switch.
            if action is not None and action != 'replace':
                cutting_access.require_editable(workflow.folder(batch_id), session['request_id'], source_sha=session['source_sha256'])
            if method == 'GET':
                if not action:
                    return handler.respond(200, cutting.get_session(batch_id, sid))
                if action == 'source-url':
                    from . import editor_source
                    return handler.respond(200, editor_source.resolve(batch_id, session, f'/api/batches/{batch_id}/cuts/{sid}/source'))
                if action == 'source':
                    return handler.stream_video(cutting.session_path(batch_id, sid)/'source.mp4')
                if action.startswith('frame/'):
                    file = cutting.frame_file(batch_id, sid, int(action[6:]))
                    return handler.respond(200, file.read_bytes(), 'image/jpeg')
                if action.startswith('output/'):
                    return handler.stream_video(cutting.output_file(batch_id, sid, action[7:]))
            elif action == 'discard':
                return handler.respond(200, cutting.discard(batch_id, sid, reset_at=data.get('reset_at', '')))
            elif action == 'replace':
                from . import cutting_replace
                return handler.respond(202, cutting_replace.start(batch_id, sid, data.get('job_id')))
            elif action == 'render':
                if data.get('mosaics'):
                    raise cutting.CutError('打码功能暂未开放', 422)
                with cutting_access.gate(workflow.folder(batch_id), session['request_id']):
                    cutting.require_reset(cutting.read_session(batch_id, sid), data.get('reset_at', ''))
                    cutting_access.require_editable(workflow.folder(batch_id), session['request_id'], source_sha=session['source_sha256'])
                    return handler.respond(202, cutting.start_render(batch_id, sid, data.get('ranges'), data.get('mosaics')))
            elif action == 'save':
                return handler.respond(200, cutting.save_job(batch_id, sid, data.get('job_id')))
            raise cutting.CutError('剪切入口不存在', 404)
    except cutting.CutError as exc:
        return handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, OSError, RuntimeError):
        return handler.respond(400, {'error': '剪切文件或处理资源暂不可用，请刷新后重试'})
