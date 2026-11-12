"""Bounded, persistent application alerts; only the web process sends notifications.

Worker processes enqueue via diagnostics. SQLite transactions protect aggregation
across processes; a separate file lock elects one sender. No network in enqueue.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib import request
from urllib.parse import urlsplit

from .alert_format import context_clean, event_context, signature, stamp, valid_batch

_queue = None
_last_failure = 0.0


def report_failure(exc):
    global _last_failure
    now = time.monotonic()
    if now - _last_failure >= 60:
        _last_failure = now
        try:
            os.write(2, f'alert_failed type={type(exc).__name__}\n'.encode())
        except OSError:
            pass


def configure(root, settings):
    global _queue
    _queue = None
    if not settings.get('enabled', False):
        return
    for key in ('cooldown_seconds', 'poll_seconds'):
        value = settings.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError('alerts.' + key + ' must be a positive integer')
    try:
        _queue = AlertQueue(root, settings)
    except Exception as exc:
        report_failure(exc)


def enabled():
    return _queue is not None


def submit(event):
    if _queue is not None:
        try:
            _queue.enqueue(event)
        except Exception as exc:
            report_failure(exc)


class AlertQueue:
    def __init__(self, root, settings):
        self.settings = dict(settings)
        self.directory = Path(root) / '.web-state' / 'alerts'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError('alert directory must not be a symlink')
        self.database = self.directory / 'queue.sqlite3'
        fd = os.open(self.database, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS groups (signature TEXT PRIMARY KEY, pending INTEGER NOT NULL, first TEXT, last TEXT, sample TEXT, sent_at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL)')

    @contextmanager
    def connection(self):
        if self.database.is_symlink():
            raise ValueError('alert database must not be a symlink')
        db = sqlite3.connect(self.database, timeout=.05)
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, event):
        if str(event.get('level', '')).lower() not in ('error', 'critical'):
            return
        error = event.get('error')
        error = error if isinstance(error, dict) else {'message': str(error or '')}
        if (event.get('event') in ('http.failed', 'http.handler_failed')
                and error.get('type') in ('BrokenPipeError', 'ConnectionResetError')):
            return
        # Persist only bounded diagnostic metadata, never arbitrary event payloads.
        keys = ('at', 'event', 'batch_id', 'parent_batch_id', 'project_name', 'actor',
                'request_id', 'trace_id', 'job_id', 'asset_id', 'version_id',
                'phase', 'release', 'environment', 'method', 'route', 'status',
                'duration_ms', 'client_request_id')
        sample = {key: context_clean(event[key], 300) for key in keys if event.get(key) is not None}
        if not valid_batch(sample.get('parent_batch_id')) and valid_batch(sample.get('batch_id')):
            from .material_versions import internal
            try:
                identity = internal(self.directory.parent / 'batches' / sample['batch_id'])
                if isinstance(identity, dict) and valid_batch(identity.get('parent_batch_id')):
                    sample['parent_batch_id'] = identity['parent_batch_id']
            except (OSError, ValueError, AttributeError):
                pass
        sample['error'] = {'type': context_clean(error.get('type') or event.get('error_type') or '', 100),
                           'message': context_clean(error.get('message') or event.get('message') or event.get('reason') or '', 800),
                           'frames': [{'file': Path(str(frame.get('file', ''))).name[:100],
                                       'function': context_clean(frame.get('function', ''), 100),
                                       'line': context_clean(frame.get('line', ''), 20)}
                                      for frame in (error.get('frames') or [])[-12:] if isinstance(frame, dict)]}
        key = signature(sample)
        at = sample.get('at', '')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM groups WHERE pending=0 AND sent_at < ?', (time.time() - 86400,))
            previous = db.execute('SELECT sample FROM groups WHERE signature=?', (key,)).fetchone()
            if previous is None:
                if db.execute('SELECT count(*) FROM groups').fetchone()[0] >= 1000:
                    raise OverflowError('alert signature capacity reached')
            batches = json.loads(previous[0]).get('batches', []) if previous and previous[0] else []
            batch = sample.get('parent_batch_id') or sample.get('batch_id')
            sample['batches'] = list(dict.fromkeys(batches + ([batch] if valid_batch(batch) else [])))[:3]
            db.execute('INSERT INTO groups VALUES (?,1,?,?,?,NULL) ON CONFLICT(signature) DO UPDATE SET pending=min(pending+1,1000000000), first=CASE WHEN pending=0 THEN excluded.first ELSE first END, last=excluded.last, sample=excluded.sample',
                       (key, at, at, json.dumps(sample, ensure_ascii=False)))

    def flush(self, *, sender=None, now=None, stop_event=None):
        """Persist notifications before sending; failed sends stay queued for retry."""
        now = time.time() if now is None else now
        fd = os.open(self.directory / 'sender.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                size = db.execute('SELECT count(*) FROM outbox').fetchone()[0]
                due = db.execute('SELECT signature,pending,first,last,sample FROM groups WHERE pending>0 AND (sent_at IS NULL OR sent_at<=?) ORDER BY rowid LIMIT ?',
                                 (now - self.settings['cooldown_seconds'], 10 if size < 100 else 0)).fetchall()
                header = f'【ExactCut 告警】{context_clean(self.settings["host_label"], 40)} {stamp(now)}\n报错事件合并'
                message = header
                for key, count, first, last, raw in due:
                    sample = json.loads(raw)
                    lines = [f'{sample["event"]}：{count} 次；{stamp(first, seconds=True)}—{stamp(last, seconds=True)}']
                    for name in ('phase', 'release', 'environment', 'version_id'):
                        if sample.get(name):
                            lines.append(f'{name}：{sample[name]}')
                    lines.extend(event_context(sample, self.settings))
                    other = [batch for batch in sample.get('batches', [])
                             if batch != (sample.get('parent_batch_id') or sample.get('batch_id'))]
                    if other:
                        lines.append('同组其他项目 id：' + '、'.join(other))
                    block = '\n'.join(lines).encode()
                    if len(block) > 10000:
                        block = block[:9900].decode(errors='ignore').encode() + '\n[摘要已截断]'.encode()
                    text = message + '\n——\n' + block.decode()
                    if len(text.encode()) > 12000:
                        break
                    message = text
                    db.execute('UPDATE groups SET pending=0, sample=NULL, sent_at=? WHERE signature=?', (now, key))
                if message != header:
                    db.execute('INSERT INTO outbox(text) VALUES (?)', (message,))
            for _ in range(10):
                if stop_event is not None and stop_event.is_set():
                    break
                with self.connection() as db:
                    item = db.execute('SELECT id,text FROM outbox ORDER BY id LIMIT 1').fetchone()
                if item is None:
                    break
                (sender or send_message)(item[1])
                with self.connection() as db:
                    db.execute('DELETE FROM outbox WHERE id=?', (item[0],))
        finally:
            os.close(fd)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_message(text):
    from .runtime import require_external
    require_external('飞书告警')
    webhook = os.environ.get('FEISHU_ALERT_WEBHOOK', '')
    url = urlsplit(webhook)
    if (url.scheme != 'https' or url.hostname not in ('open.feishu.cn', 'open.larksuite.com')
            or not url.path.startswith('/open-apis/bot/v2/hook/') or url.username or url.password
            or url.query or url.fragment or url.port not in (None, 443)):
        raise ValueError('invalid alert webhook')
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}}).encode()
    req = request.Request(webhook, data=payload, headers={'Content-Type': 'application/json'})
    with request.build_opener(NoRedirect).open(req, timeout=10) as response:
        if response.status != 200 or json.loads(response.read(65536)).get('code') != 0:
            raise ValueError('notification rejected')


class Dispatcher:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.queue = _queue

    def start(self):
        if self.queue is None:
            return
        self.thread = threading.Thread(target=self.run, name='alert-sender', daemon=True)
        try:
            self.thread.start()
        except Exception as exc:
            self.thread = None
            report_failure(exc)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.queue.flush(stop_event=self.stop_event)
            except Exception as exc:
                report_failure(exc)
            self.stop_event.wait(self.queue.settings['poll_seconds'])

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=12)
