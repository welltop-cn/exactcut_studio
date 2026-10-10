"""Private, bounded JSONL diagnostics shared by web and worker processes.

Never pass request bodies, provider responses, prompts or command lines here.
Observability must not change the outcome of a business operation.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import traceback

from .error_guidance import redact

_context = ContextVar('diagnostic_context', default={})
_root = None
_settings = {}
_lock = threading.Lock()
_last_failure = 0.0
_throttled = {}
_sensitive = re.compile(r'password|secret|token|cookie|authorization|api.?key|signature', re.I)


def safe(value, depth=0):
    if depth > 5:
        return '[truncated]'
    if isinstance(value, BaseException):
        return {'type': type(value).__name__, 'message': safe(str(value)),
                'frames': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                           for f in traceback.extract_tb(value.__traceback__)[-12:]],
                'cause_type': type(value.__cause__).__name__ if value.__cause__ else None}
    if isinstance(value, dict):
        return {str(k): '[redacted]' if _sensitive.search(str(k)) else safe(v, depth + 1)
                for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [safe(v, depth + 1) for v in value[:60]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    text = re.sub(r'''(?ix)(["']?(?:password|secret|[a-z_]*token|cookie|authorization|api[_-]?key|signature)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r'\1[redacted]', text)
    return redact(text, limit=1800)


def configure(root=None, settings=None):
    global _root, _settings
    from .runtime import data_root
    from .util import load_config
    _settings = {'retention_days': 14, 'max_file_bytes': 20 * 1024**2,
                 'max_total_bytes': 1024**3, 'enabled': True}
    _settings.update(settings if settings is not None else load_config().get('logging', {}))
    for key in ('retention_days', 'max_file_bytes', 'max_total_bytes'):
        if not isinstance(_settings[key], int) or isinstance(_settings[key], bool) or _settings[key] <= 0:
            raise ValueError('logging.' + key + ' must be a positive integer')
    if _settings['max_file_bytes'] > _settings['max_total_bytes']:
        raise ValueError('logging.max_file_bytes exceeds max_total_bytes')
    _root = Path(root if root is not None else data_root()) / 'logs' if _settings['enabled'] else None


def disable():
    global _root
    _root = None


def current():
    return dict(_context.get())


def bind(**fields):
    """Enrich an already scoped request/worker context."""
    _context.set({**current(), **{k: v for k, v in fields.items() if v is not None}})


@contextmanager
def context(**fields):
    token = _context.set({**current(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def _append(line):
    root = _root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise OSError('diagnostic directory must not be a symlink')
    root.chmod(0o700)
    # Each write opens its file after acquiring the shared lock, so no process
    # keeps writing to a renamed/deleted inode during rotation.
    fd = os.open(root / '.writer.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + .1
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('diagnostic writer busy')
                time.sleep(.005)
        now = time.time()
        files = []
        for path in sorted(root.glob('events-*.jsonl')):
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            if now - stat.st_mtime > _settings['retention_days'] * 86400:
                path.unlink()
            else:
                files.append((path, stat.st_size))
        prefix = 'events-' + datetime.now(timezone.utc).strftime('%Y%m%d') + '-'
        maximum = _settings['max_file_bytes']
        if len(line) > maximum:
            raise ValueError('diagnostic record exceeds file budget')
        target = files[-1][0] if files and files[-1][0].name.startswith(prefix) and files[-1][1] + len(line) <= maximum else None
        total = sum(size for _, size in files)
        while files and total + len(line) > _settings['max_total_bytes']:
            old, size = files.pop(0)
            old.unlink()
            total -= size
            if old == target:
                target = None
        if target is None:
            target = root / (prefix + str(time.time_ns()) + '.jsonl')
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(out, 0o600)
            with os.fdopen(out, 'ab', closefd=False) as stream:
                stream.write(line)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def event(name, *, level='info', **fields):
    global _last_failure
    if _root is None:
        return
    try:
        from .runtime import environment, release_id
        row = {'at': datetime.now(timezone.utc).isoformat(), 'event': name, 'level': level,
               'pid': os.getpid(), 'environment': environment(), 'release': release_id(),
               **current(), **fields}
        line = (json.dumps(safe(row), ensure_ascii=False, allow_nan=False) + '\n').encode()
        if len(line) > min(_settings['max_file_bytes'], 32768):
            row = {key: row[key] for key in ('at', 'event', 'level', 'pid', 'release', 'trace_id', 'job_id',
                                            'batch_id', 'request_id', 'phase', 'attempt') if key in row}
            row['record_truncated'] = True
            line = (json.dumps(safe(row), ensure_ascii=False) + '\n').encode()
        if not _lock.acquire(timeout=.1):
            raise TimeoutError('diagnostic thread busy')
        try:
            _append(line)
        finally:
            _lock.release()
    except Exception as exc:
        now = time.monotonic()
        if now - _last_failure >= 60:
            _last_failure = now
            try:
                os.write(2, ('diagnostic_write_failed type=' + type(exc).__name__ + '\n').encode())
            except OSError:
                pass


def throttled(name, key, *, interval=60, **fields):
    now = time.monotonic()
    marker = (name, str(key))
    if now - _throttled.get(marker, -float('inf')) < interval:
        return
    if len(_throttled) > 4096:
        _throttled.clear()
    _throttled[marker] = now
    event(name, **fields)


@contextmanager
def span(name, **fields):
    started = time.monotonic()
    event(name + '.started', **fields)
    try:
        yield
    except BaseException as exc:
        event(name + '.failed', level='error', duration_ms=round((time.monotonic()-started)*1000), error=exc, **fields)
        raise
    else:
        event(name + '.completed', duration_ms=round((time.monotonic()-started)*1000), **fields)


def traced(name):
    """Timing only: arguments and return payloads are deliberately not serialized."""
    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapper
    return decorate


class DiagnosticHandler(logging.Handler):
    def emit(self, record):
        event('python.log', level=record.levelname.lower(), logger=record.name,
              message=record.getMessage(), error=record.exc_info[1] if record.exc_info else None)


def install_logging():
    logger = logging.getLogger()
    if not any(isinstance(handler, DiagnosticHandler) for handler in logger.handlers):
        logger.addHandler(DiagnosticHandler())


def main():
    """Read-only bounded query; printed records are already redacted on write."""
    import argparse
    from collections import deque
    from .runtime import data_root
    parser = argparse.ArgumentParser(description='Query persistent diagnostic JSONL')
    parser.add_argument('--root', type=Path, default=None, help='data root (not logs directory)')
    for field in ('batch', 'job', 'trace', 'request'):
        parser.add_argument('--' + field)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--errors', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000 or args.hours <= 0:
        parser.error('limit must be 1..10000 and hours must be positive')
    cutoff = datetime.fromtimestamp(time.time()-args.hours*3600, timezone.utc).isoformat()
    matches = deque(maxlen=args.limit)
    for path in sorted(((args.root or data_root()) / 'logs').glob('events-*.jsonl')):
        if path.is_symlink(): continue
        try:
            with path.open() as stream:
                for line in stream:
                    try: row = json.loads(line)
                    except ValueError: continue
                    if not isinstance(row, dict) or row.get('at', '') < cutoff: continue
                    if args.errors and row.get('level') not in {'warning', 'error', 'critical'}: continue
                    if any(value and row.get(field + '_id') != value for field, value in
                           (('batch', args.batch), ('job', args.job), ('trace', args.trace), ('request', args.request))): continue
                    matches.append(row)
        except FileNotFoundError:
            continue  # A concurrent writer may rotate/delete between listing and open.
    for row in matches:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
