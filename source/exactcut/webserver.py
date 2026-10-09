"""Authenticated job console with explicit environment and data boundaries."""
from __future__ import annotations
import base64
import hmac
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from urllib.parse import unquote
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .catalog import load_catalog
from . import workflow, batch_control, accounts, production_helper
from .util import json_sha256, write_json_atomic, read_json, load_env_file

from .runtime import CODE_ROOT, data_root, require_publish, feature_flags, instance_marker, environment, release_id, maintenance_file, mutation_lock
from .runtime import project_env_file, claim_data_root

ROOT = data_root()
STATE = ROOT / '.web-state'
JOBS = STATE / 'jobs'
WORK = queue.Queue()
LOCK = threading.Lock()
WORKER_HEARTBEAT = 0.0
WORKER_FAILURES = 0
HOOK_PREVIEWS = threading.BoundedSemaphore(2)


def web_base_path():
    from .util import load_config
    value = load_config().get('web', {}).get('base_path', '')
    if not isinstance(value, str) or (value and not re.fullmatch(r'(?:/[a-zA-Z0-9_-]+)+', value)):
        raise ValueError('web.base_path 必须为空或 /exactcut 形式的路径，末尾不带斜线')
    return value


def project_path(project):
    if not isinstance(project, str) or not re.fullmatch(r'[0-9]{6}', project):
        raise ValueError('请选择六位剧 ID')
    path = ROOT / 'projects' / project
    if path.is_symlink() or not (path / 'project.json').is_file():
        raise ValueError('项目不存在')
    return path


def job_command(data):
    project = project_path(data.get('project'))
    action = data.get('action')
    if action == 'download':
        return [sys.executable, '-u', '-m', 'exactcut.material_download', project.name]
    if action not in ('prepare', 'compile', 'validate', 'execute', 'publish'):
        raise ValueError('不支持的操作')
    cmd = [sys.executable, '-u', '-m', 'exactcut', '--env-file', str(project_env_file()),
           'execute' if action == 'publish' else action, '--project', str(project)]
    if action != 'prepare':
        name = data.get('input', '')
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.xlsx'):
            raise ValueError('请选择 Excel 填写表')
        source = project / 'requests' / name
        if not source.is_file() or source.is_symlink():
            raise ValueError('填写表不存在')
        cmd += ['--input', str(source)]
    if action == 'publish':
        cmd += ['--publish', '--wait-ready']
    return cmd


def save(job):
    path = JOBS / (job['id'] + '.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def stored_jobs():
    for file in JOBS.glob('*.json'):
        try:
            job = json.loads(file.read_text())
            if isinstance(job, dict) and isinstance(job.get('id'), str):
                yield job
        except (OSError, ValueError):
            logging.exception('Unreadable job record: %s', file.name)


def queue_size():
    count = 0
    for job in stored_jobs():
        if job.get('status') not in ('queued', 'running'):
            continue
        if job.get('batch_id'):
            path = workflow.BATCHES / job['batch_id']
            if not (path / 'draft.json').exists():
                continue
            control = batch_control.read(path)
            if control.get('paused') or control.get('archived'):
                continue
        count += 1
    return count


def batch_response(batch_id, job=None):
    result = workflow.detail(batch_id)
    jobs = [item for item in stored_jobs() if item.get('batch_id') == batch_id]
    if job is None:
        job = max(jobs, key=lambda j: j.get('created', 0), default=None)
    if result['runtime']['status'] == 'queued':
        from .source_prefetch import download_progress
        plan = result['runtime'].get('plan') or result.get('plan') or {}
        prefetch_jobs = [item for item in jobs if item.get('action') == 'prefetch'
                         and batch_control.belongs(item)]
        result['runtime'].update(download_progress(workflow.folder(batch_id), result['draft']['drama_id'],
                                                   plan.get('required', []), prefetch_jobs))
    if job:
        result['submission'] = {key: job.get(key) for key in ('id', 'action', 'status', 'created', 'request_id')}
    return result


def prefetch_sources(batch_id, data, actor):
    from . import source_prefetch
    from .runtime import require_material_fetch
    require_material_fetch()
    path = workflow.folder(batch_id)
    batch_control.ensure_editable(path)
    if workflow.runtime(path)['status'] != 'draft' or (path / 'confirmed.json').exists():
        raise ValueError('仅草稿可提前准备正片')
    if data.get('cancel') is True:
        write_json_atomic(path / 'source-prefetch.json', {'drama_id': '', 'episodes': []})
        return {'drama_id': '', 'episodes': [], 'timing_explanation': []}
    drama, episodes, manifest = source_prefetch.snapshot(data.get('draft'))
    write_json_atomic(path / 'source-prefetch.json', {'drama_id': drama, 'episodes': episodes})
    jobs = [job for job in stored_jobs() if job.get('batch_id') == batch_id
            and job.get('action') == 'prefetch' and (not job.get('instance') or batch_control.belongs(job))]
    result = source_prefetch.status(path, drama, episodes, jobs, manifest)
    missing = [e['episode'] for e in result['episodes'] if e['status'] == 'waiting'
               or (e['status'] == 'failed' and data.get('retry') is True)]
    if missing and queue_size() < 10:
        job = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'project': drama, 'episodes': missing,
               'actor': actor, 'action': 'prefetch', 'created': time.time(), 'status': 'queued',
               'instance': instance_marker()}
        cmd = [sys.executable, '-u', '-m', 'exactcut.source_prefetch', batch_id, job['id'], instance_marker()]
        job['command'] = cmd
        save(job)
        WORK.put((job, cmd))
        for state in result['episodes']:
            if state['episode'] in missing:
                state.update(status='queued')
                state.pop('error_info', None)
        for timing in result['timing_explanation']:
            for segment in timing['segments']:
                if segment['episode'] in missing and segment.get('source_status') in {'waiting', 'failed'}:
                    segment['source_status'] = 'queued'
    return result


def existing_submission(batch_id, action, data, actor):
    token = data.get('submission_token')
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', token)):
        raise ValueError('无效提交标识，请刷新页面重试')
    payload = json_sha256({key: value for key, value in data.items() if key != 'submission_token'})
    for job in stored_jobs():
        if job.get('batch_id') != batch_id or job.get('action') != action or job.get('actor') != actor:
            continue
        if token and job.get('submission_token') == token:
            if job.get('submission_payload') != payload:
                raise ValueError('提交标识已用于其他内容，请刷新页面重试')
            return job
        if job.get('status') in ('queued', 'running') and (action == 'start' or job.get('request_id') == data.get('request_id')):
            return job
    return None


def finish_job(job, code=None, message='执行进程已结束，可查看日志后重试'):
    job.update(status='interrupted' if code is None else 'completed' if code == 0 else 'failed',
               exit_code=code, finished=time.time())
    save(job)
    if not job.get('batch_id'):
        return
    path = workflow.BATCHES / job['batch_id']
    if not (path / 'draft.json').exists():
        return
    if job.get('action') == 'prefetch':
        return
    workflow.update(path, executor_running=False, execution_ended_at=workflow.now_iso())
    if job.get('action') == 'publish' and code != 0:
        command = job.get('command', [])
        rid = job.get('request_id') or (command[-2] if command and command[-1].startswith('--instance=') else command[-1] if command else None)
        publications = workflow.read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
        entry = publications.get(rid, {})
        if entry.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'}:
            entry.update(status='publish_failed', error=message)
            publications = workflow.store_publication(path, rid, entry)
            pending = any(e.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'} for e in publications.values())
            workflow.update(path, 'publishing' if pending else 'review', message)
    elif workflow.runtime(path)['status'] in workflow.ACTIVE:
        workflow.update(path, 'interrupted', message)


def job_alive(job):
    if not batch_control.belongs(job):
        return False
    return batch_control.child_state(job) not in {None, 'Z'}


def recover_jobs(*, enqueue=False):
    jobs = list(stored_jobs())
    live = set()
    queued = set()
    for job in sorted(jobs, key=lambda j: j.get('created', 0)):
        if job.get('instance') and not batch_control.belongs(job):
            continue
        batch = job.get('batch_id')
        if job.get('status') == 'queued' and job.get('command'):
            if batch and not (workflow.BATCHES / batch / 'draft.json').exists():
                finish_job(job)
                continue
            children = batch_control.own_live_children(workflow.BATCHES / batch) if batch else []
            token = job.get('process_token')
            child = next((c for c in children if c.get('process_token') == token), None)
            if child is None and batch_control.valid_token(token):
                found = batch_control.PROCESSES.find(token)
                if found:
                    candidate = {**found, 'process_token': token, 'instance': instance_marker(),
                                 'lane': job.get('action'), 'request_id': job.get('request_id')}
                    if batch_control.child_state(candidate) not in {None, 'Z'}:
                        child = candidate
                        if batch:
                            batch_control.remember_child(workflow.BATCHES / batch, child)
            if child:
                job.update(status='running', adopted_after_restart=True, instance=child.get('instance'),
                           pid=child['pid'], pgid=child.get('pgid'), process_token=child['process_token'])
                save(job)
                live.add(batch)
                continue
            queued.add(batch)
            if enqueue:
                WORK.put((job, job['command']))
        elif job.get('status') == 'running' and job_alive(job):
            job.update(adopted_after_restart=True)
            save(job)
            if batch and (workflow.BATCHES / batch / 'draft.json').exists():
                batch_control.remember_child(workflow.BATCHES / batch, {**job, 'lane': job.get('action')})
            live.add(batch)
            continue
        elif job.get('status') == 'running' and (batch not in live or job.get('action') == 'prefetch'):
            code = None
            if batch and (workflow.BATCHES / batch / 'runtime.json').exists():
                state = workflow.runtime(workflow.BATCHES / batch)['status']
                if job.get('action') == 'start':
                    code = 0 if state in {'review', 'complete'} else 2 if state in {'partial', 'failed'} else None
                elif job.get('action') == 'publish':
                    path = workflow.BATCHES / batch / 'publications.json'
                    publications = workflow.read_json(path) if path.exists() else {}
                    status = publications.get(job.get('request_id'), {}).get('status')
                    code = 0 if status == 'fbbm_ready' else 2 if status == 'publish_failed' else None
                elif job.get('action') == 'prefetch':
                    from .source_prefetch import result_path
                    file = result_path(workflow.folder(batch), job['id'])
                    result = read_json(file) if file.exists() else {}
                    code = 0 if result.get('status') == 'completed' else 2 if result.get('status') == 'failed' else None
            finish_job(job, code)
    for file in workflow.BATCHES.glob('*/runtime.json'):
        path = file.parent
        if path.name in live or path.name in queued:
            continue
        if workflow.runtime(path)['status'] in workflow.ACTIVE:
            # Includes a crash between confirming a batch and persisting its job.
            publications = workflow.read_json(path / 'publications.json') if (path / 'publications.json').exists() else {}
            for rid, entry in publications.items():
                if entry.get('status') in {'upload_queued', 'uploading', 'fbbm_queued'}:
                    workflow.store_publication(path, rid, {**entry, 'status': 'publish_failed', 'error': '执行中断，可重试继续原记录'})
            workflow.update(path, 'interrupted', '执行已中断，可重试制作或继续上传')
            control = batch_control.read(path)
            control.update(paused=False, suspended=[])
            batch_control.write(path, control)


def worker_tick(pending, owned):
    while True:
        try:
            pending.append(WORK.get_nowait())
        except queue.Empty:
            break
    with LOCK, batch_control.LOCK:
        for key, (job, process, log) in list(owned.items()):
            code = process.poll()
            if code is None:
                continue
            try:
                log.close()
                finish_job(job, code)
            finally:
                del owned[key]
                WORK.task_done()
        for entry in list(pending):
            job, _ = entry
            if job.get('batch_id') and not (workflow.BATCHES / job['batch_id'] / 'draft.json').exists():
                pending.remove(entry)
                WORK.task_done()
        running = batch_control.reconcile(workflow.BATCHES)
        running |= bool(owned and any(not job.get('batch_id') for job, _, _ in owned.values()))
        if not owned:
            recover_jobs()
            running |= any(j.get('status') == 'running' and not j.get('batch_id') and job_alive(j)
                           for j in stored_jobs())
        if running:
            return
        for index, (job, cmd) in enumerate(pending):
            if job.get('batch_id'):
                control = batch_control.read(workflow.folder(job['batch_id']))
                if control.get('paused') or control.get('archived'):
                    continue
            log = (JOBS / (job['id'] + '.log')).open('ab')
            try:
                # CPython accepts arbitrary -X options without changing module argv.
                # A fresh token identifies this launch even if the OS recycles its PID.
                token = uuid.uuid4().hex
                if len(cmd) > 2 and cmd[1] == '-X' and cmd[2].startswith('exactcut_process_token='):
                    cmd = [cmd[0], *cmd[3:]]
                cmd = [cmd[0], '-X', 'exactcut_process_token=' + token, *cmd[1:]]
                job.update(process_token=token, command=cmd, instance=instance_marker())
                save(job)
                process = subprocess.Popen(cmd, cwd=CODE_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except Exception as exc:
                log.close()
                finish_job(job, -1, '启动失败，可重试：' + type(exc).__name__)
                pending.pop(index)
                WORK.task_done()
            else:
                identity = batch_control.identify(process, process_token=token,
                                                 lane=job.get('action'), request_id=job.get('request_id'))
                owned[job['id']] = (job, process, log)
                pending.pop(index)
                job.update(status='running', started=time.time(), instance=instance_marker())
                if identity:
                    job.update(pid=identity['pid'], pgid=identity['pgid'])
                save(job)
                if identity and job.get('batch_id'):
                    batch_control.remember_child(workflow.folder(job['batch_id']), identity)
            break


def worker():
    global WORKER_HEARTBEAT, WORKER_FAILURES
    pending, owned = [], {}
    while True:
        WORKER_HEARTBEAT = time.monotonic()
        try:
            worker_tick(pending, owned)
            WORKER_FAILURES = 0
        except Exception:
            WORKER_FAILURES += 1
            logging.exception('Runner tick failed; retrying without dropping queued work')
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def public_url(self, path):
        return getattr(getattr(self, 'server', None), 'base_path', '') + path

    def normalize_path(self):
        self.path = self.path.partition('?')[0]
        prefix = getattr(self.server, 'base_path', '')
        if prefix and self.path.startswith(prefix + '/'):
            self.path = self.path[len(prefix):]

    def web_page(self, name):
        body = (CODE_ROOT / 'exactcut/web' / name).read_bytes()
        base = f'<base href="{self.public_url("/")}">'.encode()
        return body.replace(b'<meta', base + b'<meta', 1)

    def log_message(self, fmt, *args):
        # Do not log credentials, query strings or request bodies.
        pass

    def respond(self, code, body, kind='application/json; charset=utf-8'):
        if isinstance(body, dict) and body.get('error'):
            from .error_guidance import guidance, redact
            body = {**body, 'error': redact(body['error']),
                    'error_info': body.get('error_info') or guidance(body['error'])}
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if getattr(self, 'response_cookie', None):
            self.send_header('Set-Cookie', self.response_cookie)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self, token):
        proto = (self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        secure = '; Secure' if proto == 'https' else ''
        self.response_cookie = f'exactcut_session={token}; Path={self.public_url("/")}; HttpOnly; SameSite=Strict; Max-Age=604800{secure}'

    def authorized(self):
        self.user = None
        self.session_token = ''
        db = getattr(self.server, 'auth_db', None)
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie',''))
        except Exception:
            pass
        if db and 'exactcut_session' in cookies:
            self.session_token = cookies['exactcut_session'].value
            self.user = accounts.session(db, self.session_token)
        else:
            try:
                scheme, encoded = self.headers.get('Authorization','').split(' ',1)
                username, password = base64.b64decode(encoded,validate=True).decode().split(':',1)
                if scheme == 'Basic':
                    if db:
                        self.user = accounts.authenticate(db,username,password)
                    elif username=='admin' and hmac.compare_digest(password,self.server.password):
                        self.user = {'username':'admin','name':'管理员','role':'admin','enabled':1}
            except (ValueError, UnicodeError):
                pass
        if self.user:
            return True
        if self.path == '/':
            self.send_response(302)
            self.send_header('Location', self.public_url('/login'))
            self.send_header('Content-Length','0')
            self.end_headers()
        else:
            self.respond(401, {'error':'请登录后继续'})
        return False

    def can_access(self, batch_id):
        path = workflow.BATCHES / batch_id
        return (path / 'draft.json').exists() and accounts.allowed(path, self.user)

    def is_admin(self):
        if self.user['role'] == 'admin':
            return True
        self.respond(403, {'error':'此操作仅限管理员'})
        return False

    def new_batch(self, result):
        for item in result.get('imported_batches', [{'id':result['id']}]):
            path = workflow.folder(item['id'])
            accounts.assign(path, self.user)
            workflow.archive_draft(path)
        fresh = workflow.detail(result['id'])
        if 'imported_batches' in result:
            fresh['imported_batches'] = result['imported_batches']
        return fresh

    def workspace(self, data):
        """Called under the existing mutation lock; persist onboarding per account."""
        path = ROOT / '.web-state/workspaces' / (self.user['username'] + '.json')
        state = read_json(path) if path.exists() else {}
        batches = workflow.summaries()
        accessible = {b['id']: b for b in batches if self.can_access(b['id'])}
        owned = [b for b in batches if accounts.owner(workflow.folder(b['id'])) == self.user['username']]
        if 'batch_id' in data and (not isinstance(data['batch_id'], str) or data['batch_id'] not in accessible):
            raise ValueError('项目不存在或无权访问')
        if not state.get('initialized'):
            if not owned:
                created = workflow.create_blank(display_name='默认项目')
                accounts.assign(workflow.folder(created['id']), self.user)
                state['last_batch_id'] = created['id']
                accessible[created['id']] = created
            state['initialized'] = True
        chosen = data.get('batch_id', state.get('last_batch_id'))
        if chosen not in accessible:
            candidates = [b for b in owned if not b.get('archived')] or owned
            chosen = candidates[0]['id'] if candidates else None
        state['last_batch_id'] = chosen
        write_json_atomic(path, state)
        return {'batch_id': chosen}

    def do_GET(self):
        self.normalize_path()
        if self.path == '/login':
            return self.respond(200, self.web_page('login.html'), 'text/html; charset=utf-8')
        if self.path == '/healthz':
            runner = getattr(self.server, 'worker_thread', None)
            heartbeat_age = max(0, time.monotonic() - WORKER_HEARTBEAT) if runner else None
            healthy = runner is None or (runner.is_alive() and heartbeat_age < 30 and WORKER_FAILURES < 3)
            return self.respond(200 if healthy else 503, {'status': 'ok' if healthy else 'degraded', 'release': release_id(), 'environment': environment(), 'maintenance': maintenance_file().exists(), 'worker_alive': runner.is_alive() if runner else None, 'worker_heartbeat_age_seconds': round(heartbeat_age, 1) if runner else None})
        if not self.authorized():
            return
        if self.path == '/api/me':
            return self.respond(200, {**self.user, 'features': feature_flags()})
        if self.path == '/api/users':
            if not self.is_admin(): return
            return self.respond(200, {'users': accounts.users(self.server.auth_db)})
        if self.path == '/api/storage':
            if not self.is_admin():return
            from .storage import report
            return self.respond(200,report(ROOT))
        if self.path == '/':
            if getattr(self.server, 'auth_db', None) and not self.session_token:
                self.cookie(accounts.login(self.server.auth_db, self.user))
            return self.respond(200, self.web_page('index.html'), 'text/html; charset=utf-8')
        if self.path == '/helper.js':
            return self.respond(200, (CODE_ROOT / 'exactcut/web/helper.js').read_bytes(), 'text/javascript; charset=utf-8')
        if self.path == '/app.js':
            return self.respond(200, (CODE_ROOT / 'exactcut/web/app.js').read_bytes(), 'text/javascript; charset=utf-8')
        if self.path == '/api/batches':
            with LOCK:
                return self.respond(200, {'batches': [{**b, 'owner': accounts.owner(workflow.BATCHES / b['id'])} for b in workflow.summaries() if self.can_access(b['id'])]})
        if self.path == '/api/feedback':
            return self.respond(200, workflow.feedback_summary(self.can_access))
        match = re.fullmatch(r'/api/batches/([0-9a-f]{32})(?:/(.*))?', self.path)
        if match:
            try:
                batch_id, suffix = match.groups()
                if not self.can_access(batch_id):
                    return self.respond(404, {'error':'项目不存在或无权访问'})
                path = workflow.folder(batch_id)
                if not suffix:
                    with LOCK:
                        return self.respond(200, batch_response(batch_id))
                if suffix.startswith('video/'):
                    output, _ = workflow.preview_file(batch_id, unquote(suffix[6:]))
                    return self.stream_video(output)
                if suffix.startswith('preview/'):
                    output, _ = workflow.preview_file(batch_id, unquote(suffix[8:]), lightweight=True)
                    return self.stream_video(output)
                if suffix == 'manifest':
                    manifest = path / 'confirmed.json'
                    return self.respond(200, manifest.read_bytes() if manifest.exists() else (path / 'draft.json').read_bytes())
                if suffix.startswith('ending-image/'):
                    image = workflow.ending_image_file(batch_id, unquote(suffix[len('ending-image/'):]))
                    kind = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[image.suffix]
                    return self.respond(200, image.read_bytes(), kind)
                if suffix == 'download-log':
                    from .error_guidance import redact
                    log = path / 'download.log'
                    return self.respond(200, {'log': redact(log.read_text(errors='replace')[-64000:], limit=64000) if log.exists() else ''})
            except (ValueError, FileNotFoundError) as exc:
                return self.respond(400, {'error': str(exc)})
            return self.respond(404, {'error': '不存在'})
        if self.path == '/api/state':
            if not self.is_admin(): return
            projects = []
            for p in sorted((ROOT / 'projects').glob('[0-9]' * 6)):
                if p.is_symlink() or not (p / 'project.json').is_file():
                    continue
                meta = json.loads((p / 'project.json').read_text())
                projects.append({'id': p.name, 'title': meta.get('title', p.name),
                                 'videos': len(list((p / 'source/videos').glob('*.mp4'))),
                                 'indexes': len(list((p / 'data/episodes').glob('*.video.json'))),
                                 'inputs': [f.name for f in sorted((p / 'requests').glob('*.xlsx'))]})
            jobs = [json.loads(p.read_text()) for p in JOBS.glob('*.json')]
            catalog = load_catalog()
            return self.respond(200, {'projects': projects, 'catalog': catalog['dramas'], 'catalog_updated': catalog['updated'], 'jobs': sorted(jobs, key=lambda j: j['created'], reverse=True)[:100]})
        match = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/log', self.path)
        if match:
            if not self.is_admin(): return
            path = JOBS / (match[1] + '.log')
            content = ''
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 64000))
                    content = stream.read().decode('utf-8', errors='replace')
            # Remove configured secrets from diagnostics.
            env = ROOT / '.env'
            if env.exists():
                for line in env.read_text().splitlines():
                    if '=' in line and not line.lstrip().startswith('#'):
                        key, value = line.split('=', 1)
                        value = value.strip().strip('\"\'')
                        if any(x in key for x in ('KEY', 'SECRET', 'TOKEN', 'PASSWORD')) and value:
                            content = content.replace(value, '[REDACTED]')
            return self.respond(200, {'log': content})
        if self.path == '/template.xlsx':
            return self.respond(200, (CODE_ROOT / 'templates/标准剪辑需求_模板.xlsx').read_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.respond(404, {'error': '不存在'})

    def stream_video(self, path):
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        requested = self.headers.get('Range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or not any(match.groups()):
                return self.respond(416, {'error': '无效视频范围'})
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            else:
                start = max(0, size - int(right))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            # Bound each range so proxies need not buffer an entire HD video.
            end = min(end, start + 2 * 1024 * 1024 - 1)
            code = 206
        self.send_response(code)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('Content-Length', str(end - start + 1))
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as stream:
            stream.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    chunk = stream.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        self.normalize_path()
        match = re.fullmatch(r'/api/batches/([0-9a-f]{32})/hook-preview', self.path)
        if match:
            return self.hook_preview(match[1])
        with mutation_lock():
            self.handle_post()

    def hook_preview(self, batch_id):
        from .hook_preview import compute
        from .intake import validate_draft
        from .runtime import protect_path
        if not self.authorized():
            return
        if self.headers.get('X-Exactcut-Request') != '1':
            return self.respond(403, {'error':'无效请求来源'})
        acquired = False
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 1 <= size <= 10000:
                raise ValueError('无效预览请求')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError('无效预览请求')
            with mutation_lock(), LOCK:
                if maintenance_file().exists():
                    return self.respond(503,{'error':'正在发布维护，请稍后重试'})
                if not self.can_access(batch_id):
                    return self.respond(404,{'error':'项目不存在或无权访问'})
                snapshot = workflow.detail(batch_id)
                if snapshot['runtime']['status'] != 'draft' or snapshot['runtime'].get('archived'):
                    raise ValueError('请复制为新草稿后调整 Hook')
                if data.get('revision') != snapshot['revision']:
                    return self.respond(409,{'error':'草稿已变化，请重新保存再预览'})
                index = data.get('index')
                if type(index) is not int or not 0 <= index < len(snapshot['draft']['rows']):
                    raise ValueError('无效素材序号')
                manifest, errors = validate_draft(snapshot['draft'])
                if errors:
                    raise ValueError('请先修正并保存制作内容：'+'；'.join(errors))
                if not re.fullmatch(r'[0-9]{6}',manifest.drama_id):
                    raise ValueError('请填写六位剧 ID')
                project = ROOT/'projects'/manifest.drama_id
                if project.is_symlink():
                    raise ValueError('无效项目目录')
                protect_path(project)
                request = manifest.requests[index]
                fingerprint = json_sha256(snapshot['draft'])
            acquired = HOOK_PREVIEWS.acquire(blocking=False)
            if not acquired:
                return self.respond(429,{'error':'已有两条 Hook 在预览，请稍后再试'})
            # Network/model calls and media reads use temporary files, outside the write barrier.
            with tempfile.TemporaryDirectory(prefix='exactcut-hook-preview-') as temp:
                result, doc, cache = compute(project,request,Path(temp))
                with mutation_lock(), LOCK:
                    if maintenance_file().exists():
                        return self.respond(503,{'error':'正在发布维护，预览结果未保存，请稍后重试'})
                    if not self.authorized():
                        return
                    if not self.can_access(batch_id):
                        return self.respond(404,{'error':'项目不存在或无权访问'})
                    latest = workflow.detail(batch_id)
                    if latest['runtime']['status']!='draft' or latest['runtime'].get('archived') or latest['revision']!=snapshot['revision'] or json_sha256(latest['draft'])!=fingerprint:
                        return self.respond(409,{'error':'预览期间草稿已变化，结果未采用，请重新预览'})
                    for directory in ('hook-emphasis','hook-placement'):
                        for file in (cache/directory).glob('*.json'):
                            write_json_atomic(project/'cache'/directory/file.name,read_json(file))
                    write_json_atomic(project/'cache/hook-previews'/f"{result['preview_key']}.json",doc)
                    return self.respond(200,result)
        except (ValueError,KeyError,TypeError,RuntimeError,OSError) as exc:
            self.respond(400,{'error':str(exc)})
        finally:
            if acquired:
                HOOK_PREVIEWS.release()

    def handle_post(self):
        if self.path != '/api/login' and not self.authorized():
            return
        if self.headers.get('X-Exactcut-Request') != '1':
            return self.respond(403, {'error': '无效请求来源'})
        if maintenance_file().exists() and self.path not in ('/api/login', '/api/logout'):
            return self.respond(503, {'error': '系统正在发布维护，请稍后再试；已提交任务继续执行'})
        if os.environ.get('EXACTCUT_WORKER_ENABLED', '1') != '1' and (self.path == '/api/jobs' or re.fullmatch(r'/api/batches/[0-9a-f]{32}/(start|publish|preview|sources|pause|resume|archive|unarchive|delete)', self.path)):
            return self.respond(503, {'error': '验收实例禁止启动或控制生产任务'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 15_000_000:
                raise ValueError('请求大小必须在 15 MB 以内')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('无效请求')
            if self.path == '/api/login':
                user = accounts.authenticate(self.server.auth_db, data.get('username',''), data.get('password',''))
                if not user:
                    return self.respond(401, {'error':'账户或密码错误，连续失败请5分钟后再试'})
                self.cookie(accounts.login(self.server.auth_db, user))
                return self.respond(200, user)
            if self.path == '/api/help':
                batch_id = data.get('batch_id')
                detail = None
                if batch_id:
                    if not isinstance(batch_id, str) or not re.fullmatch(r'[0-9a-f]{32}', batch_id):
                        raise ValueError('无效项目')
                    with LOCK, batch_control.LOCK:
                        if not self.can_access(batch_id):
                            return self.respond(404, {'error':'项目不存在或无权访问'})
                        detail = workflow.detail(batch_id)
                return self.respond(200, production_helper.ask(self.user, data, detail))
            if self.path == '/api/logout':
                accounts.logout(self.server.auth_db, self.session_token)
                self.cookie('loggedout')
                return self.respond(200, {'ok':True})
            if self.path == '/api/users':
                if not self.is_admin(): return
                return self.respond(200, {'users':accounts.manage(self.server.auth_db,self.user['username'],data)})
            if self.path == '/api/workspace':
                with LOCK:
                    return self.respond(200, self.workspace(data))
            if self.path == '/api/storage/cleanup':
                if not self.is_admin():return
                from .storage import cleanup,report
                if not isinstance(data.get('token'),str):raise ValueError('请先查看缓存清单并确认')
                result=cleanup(ROOT,token=data['token'])
                return self.respond(200,{**report(ROOT),'result':result})
            if self.path in ('/api/projects','/api/jobs','/api/upload') and not self.is_admin():
                return
            if self.path == '/api/batches/blank':
                result = workflow.create_blank(self.user.get('name') or '')
                with LOCK:
                    accounts.assign(workflow.folder(result['id']), self.user)
                    return self.respond(201, result)
            if self.path == '/api/batches/import':
                result = workflow.import_excel(data)
                with LOCK:
                    return self.respond(201, self.new_batch(result))
            image_upload = re.fullmatch(r'/api/batches/([0-9a-f]{32})/ending-image', self.path)
            if image_upload:
                batch_id = image_upload[1]
                with LOCK:
                    if not self.can_access(batch_id):
                        return self.respond(404, {'error':'项目不存在或无权访问'})
                    return self.respond(200, workflow.save_ending_image(batch_id, data.get('request_id'), data.get('content')))
            match = re.fullmatch(r'/api/batches/([0-9a-f]{32})/(save|start|publish|preview|sources|feedback|clone|pause|resume|archive|unarchive|delete)', self.path)
            if match:
                batch_id, action = match.groups()
                with LOCK:
                    if not self.can_access(batch_id):
                        return self.respond(404, {'error':'项目不存在或无权访问'})
                    if action == 'sources':
                        return self.respond(200, prefetch_sources(batch_id, data, self.user['username']))
                    if action == 'publish':
                        workflow.reject_review_sha(
                            workflow.result_records(workflow.folder(batch_id)).get(data.get('request_id'), {}),
                            data.get('sha256'))
                        require_publish()
                    if getattr(self.server, 'auth_db', None):
                        accounts.audit(self.server.auth_db,self.user['username'],action,batch_id)
                    if action == 'delete':
                        return self.respond(200, workflow.delete_batch(batch_id, data.get('confirmation')))
                    if action in ('pause', 'resume', 'archive', 'unarchive'):
                        batch_control.change(workflow.folder(batch_id), action)
                        return self.respond(200, workflow.detail(batch_id))
                    if action == 'save':
                        return self.respond(200, workflow.save_draft(batch_id, data))
                    if action == 'clone':
                        return self.respond(201, self.new_batch(workflow.clone_batch(batch_id, purpose=data.get('purpose', 'copy'))))
                    if action == 'feedback':
                        return self.respond(201, workflow.add_feedback(batch_id, data))
                    previous = existing_submission(batch_id, action, data, self.user['username'])
                    if previous:
                        return self.respond(200, batch_response(batch_id, previous))
                    if queue_size() >= 10:
                        return self.respond(429, {'error': '任务队列已满，请稍后再试'})
                    if action == 'start':
                        cmd = workflow.start_batch(batch_id, data.get('revision'))
                    elif action == 'preview':
                        path = workflow.folder(batch_id)
                        batch_control.ensure_editable(path, execution=True)
                        if workflow.runtime(path)['status'] in workflow.ACTIVE:
                            raise ValueError('请等待当前制作完成，再生成预览')
                        cmd = [sys.executable, '-u', '-m', 'exactcut.workflow', 'preview', batch_id, instance_marker()]
                    else:
                        cmd = workflow.queue_publish(batch_id, data.get('request_id'), data.get('sha256'))
                    job = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'project': workflow.detail(batch_id)['draft']['drama_id'],
                           'actor': self.user['username'], 'action': action, 'created': time.time(), 'status': 'queued',
                           'request_id': data.get('request_id'), 'submission_token': data.get('submission_token'),
                           'submission_payload': json_sha256({k: v for k, v in data.items() if k != 'submission_token'})}
                    job['command'] = cmd
                    save(job)
                    WORK.put((job, cmd))
                    return self.respond(202, batch_response(batch_id, job))
            if self.path == '/api/projects':
                drama_id = data.get('project')
                entry = next((item for item in load_catalog()['dramas'] if item['id'] == drama_id), None)
                if entry is None:
                    raise ValueError('剧 ID 不在已核实候选中，请刷新剧目清单')
                project = ROOT / 'projects' / drama_id
                with LOCK:
                    if project.is_symlink():
                        raise ValueError('无效项目目录')
                    project.mkdir(exist_ok=True)
                    config = project / 'project.json'
                    if not config.exists():
                        config.write_text(json.dumps({'project_id': drama_id, 'title': entry['title'], 'excluded_episodes': []}, ensure_ascii=False, indent=2))
                    for folder in ('requests', 'source/videos', 'data/episodes', 'outputs'):
                        (project / folder).mkdir(parents=True, exist_ok=True)
                return self.respond(201, {'id': drama_id})
            if self.path == '/api/jobs':
                if data.get('action') == 'publish':
                    raise ValueError('请先通过新版流程制作并预览，再逐条确认上传 FBBM')
                if data.get('action') == 'refresh_catalog':
                    cmd = [sys.executable, '-u', '-m', 'exactcut.catalog']
                    data['project'] = 'catalog'
                else:
                    cmd = job_command(data)
                with LOCK:
                    if queue_size() >= 10:
                        return self.respond(429, {'error': '任务队列已满，请稍后再试'})
                    job = {'id': uuid.uuid4().hex, 'project': data['project'], 'action': data['action'],
                           'input': data.get('input'), 'status': 'queued', 'created': time.time()}
                    job['command'] = cmd
                    save(job)
                    WORK.put((job, cmd))
                return self.respond(202, job)
            if self.path == '/api/upload':
                project = project_path(data.get('project'))
                content = base64.b64decode(data['content'], validate=True)
                if len(content) > 10_000_000 or not content.startswith(b'PK'):
                    raise ValueError('请上传 10 MB 以内的 XLSX 文件')
                folder = project / 'requests'
                folder.mkdir(exist_ok=True)
                name = 'upload-' + uuid.uuid4().hex[:12] + '.xlsx'
                (folder / name).write_bytes(content)
                return self.respond(201, {'name': name})
            self.respond(404, {'error': '不存在'})
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            from .error_guidance import guidance, redact
            self.respond(400, {'error': redact(exc), 'error_info': guidance(str(exc))})
        except Exception as exc:
            self.respond(400, {'error': f'处理失败（{type(exc).__name__}），请检查文件格式或查看服务日志'})


def main():
    import fcntl
    env_file = project_env_file()
    if env_file.is_file() or os.environ.get('EXACTCUT_ENV_FILE'):
        load_env_file(env_file)
    base_path = web_base_path()
    claim_data_root()
    worker_enabled = os.environ.get('EXACTCUT_WORKER_ENABLED', '1') == '1'
    JOBS.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    worker_lock = None
    if worker_enabled:
        worker_lock = (STATE / 'worker.lock').open('a')
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('此数据目录已有任务执行器，禁止重复启动')
    password = (STATE / 'password').read_text().strip()
    if len(password) < 24:
        raise RuntimeError('控制台密码需至少 24 位')
    host = os.environ.get('EXACTCUT_BIND', '127.0.0.1').strip() or '127.0.0.1'
    if host not in {'127.0.0.1', '0.0.0.0'}:
        raise RuntimeError('EXACTCUT_BIND 只能是 127.0.0.1 或 0.0.0.0')
    server = ThreadingHTTPServer((host, int(os.environ.get('EXACTCUT_PORT', '8790'))), Handler)
    server.password = password
    server.base_path = base_path
    server.auth_db = STATE / 'accounts.sqlite3'
    accounts.initialize(server.auth_db, password)
    for path in workflow.BATCHES.glob('*/draft.json'):
        if not (path.parent / 'access.json').exists():
            workflow.write_json_atomic(path.parent / 'access.json', {'owner':'admin'})
    if worker_enabled:
        recover_jobs(enqueue=True)
        server.worker_thread = threading.Thread(target=worker, daemon=True)
        server.worker_thread.start()
    server.serve_forever()


if __name__ == '__main__':
    main()
