"""Best-effort lock observations; diagnostic I/O runs after all wrapped locks exit."""
from contextvars import ContextVar
from contextlib import contextmanager
import re
import threading
import time

from . import diagnostics as diag

SLOW_MS = 200
SUMMARY_SECONDS = 60
_current = ContextVar('lock_timing_owner', default=None)
_local = threading.local()
_ROUTE_PARTS = frozenset({
    ':id', 'accounts', 'admin', 'api', 'app.js', 'archive', 'batches', 'blank',
    'brand-style.jpg', 'cleanup', 'client-events', 'clip-text', 'clone', 'confirm', 'control',
    'cuts', 'cutting.js', 'delete', 'diagnostics.js', 'download', 'draft', 'editor-busy.js',
    'editor-source.js', 'ending-image', 'feedback', 'healthz', 'help', 'helper.js',
    'hook-preview', 'hook-previews', 'import', 'jobs', 'log', 'login', 'logout', 'me',
    'mosaic.js', 'mosaics', 'originals', 'pause', 'preview', 'preview-subtitles',
    'preview-subtitles.js', 'preview-url', 'projects', 'publish', 'queue', 'render', 'resume',
    'runtime', 'save', 'sidebar', 'sources', 'start', 'state', 'storage', 'template.xlsx',
    'unarchive', 'unrecognized', 'upload', 'usage', 'users', 'verify-delivery', 'video',
    'workspace',
})


def _clock():
    try:
        return time.perf_counter()
    except Exception:
        return 0.0


def route_owner(method, route):
    try:
        route = route.partition('?')[0]
        route = re.sub(r'(?<=/)[0-9a-fA-F]{32}(?=/|$)', ':id', route)
        # Account names and arbitrary path components must never become owner keys.
        parts = route.split('/')
        for index in range(1, len(parts)):
            if parts[index - 1] in {'users', 'accounts'} and parts[index]:
                parts[index] = ':part'
        return method + ' ' + '/'.join(p if p in _ROUTE_PARTS or not p else ':part' for p in parts)
    except Exception:
        return 'unknown'


def set_owner(owner):
    try:
        observation = _current.get()
        if observation is not None:
            observation.owner = owner
    except Exception:
        pass


class Observation:
    def __init__(self, owner='unknown'):
        self.owner = owner
        self.holder_owner = None
        self.metrics = {}

    def fields(self):
        result = dict(lock_wait_ms=0.0, lock_hold_ms=0.0, lock_count=0)
        try:
            for name, (count, wait, hold) in self.metrics.items():
                prefix = '' if name == 'web' else name + '_'
                result.update({prefix + 'lock_count': count,
                               prefix + 'lock_wait_ms': round(wait, 1),
                               prefix + 'lock_hold_ms': round(hold, 1)})
        except Exception:
            pass
        return result


@contextmanager
def observe(owner='unknown'):
    observation = Observation(owner)
    token = _current.set(observation)
    try:
        yield observation
    finally:
        _current.reset(token)


class TimedLock:
    def __init__(self, inner, name):
        self.inner = inner
        self.name = name
        self._state = {}
        self._holder = None
        self._stats = {}
        self._stats_lock = threading.Lock()
        self._last_summary = _clock()

    def acquire(self, blocking=True, timeout=-1):
        ident = threading.get_ident()
        try:
            state = self._state.get(ident)
        except Exception:
            state = None
        if state is not None:
            acquired = self.inner.acquire(blocking, timeout)
            if acquired:
                try:
                    state[0] += 1
                except Exception:
                    pass
            return acquired
        started = _clock()
        try:
            holder = self._holder
            blocked_by = holder.owner if holder is not None else 'unknown'
        except Exception:
            blocked_by = 'unknown'
        acquired = self.inner.acquire(blocking, timeout)
        if acquired:
            try:
                if ident in self._state:
                    self._state[ident][0] += 1
                else:
                    observation = _current.get() or Observation()
                    self._state[ident] = [1, _clock(), started, observation, blocked_by, observation.owner]
                    self._holder = observation
                    _local.held = getattr(_local, 'held', 0) + 1
            except Exception:
                pass
        return acquired

    def release(self):
        record = None
        try:
            ident = threading.get_ident()
            state = self._state.get(ident)
            if state and state[0] == 1:
                record = (state, _clock())
                del self._state[ident]
                self._holder = None
            elif state:
                state[0] -= 1
        except Exception:
            pass
        self.inner.release()
        try:
            if record is not None:
                _local.held -= 1
                deferred = getattr(_local, 'deferred', None)
                if deferred is None:
                    deferred = _local.deferred = []
                deferred.append((self, record))
            if not getattr(_local, 'held', 0):
                deferred = getattr(_local, 'deferred', [])
                _local.deferred = []
                for lock, item in deferred:
                    try:
                        lock._record(*item)
                    except Exception:
                        pass
        except Exception:
            pass

    def _record(self, state, ended):
        _, acquired, started, observation, blocked_by, owner = state
        owner = observation.holder_owner or owner
        wait = max(0, (acquired - started) * 1000)
        hold = max(0, (ended - acquired) * 1000)
        old = observation.metrics.get(self.name, (0, 0, 0))
        observation.metrics[self.name] = (old[0] + 1, old[1] + wait, old[2] + hold)
        slow = wait >= SLOW_MS or hold >= SLOW_MS
        now = _clock()
        summary = None
        with self._stats_lock:
            row = self._stats.setdefault(owner, dict(count=0, wait_ms_total=0, wait_ms_max=0,
                                                     hold_ms_total=0, hold_ms_max=0, slow_count=0))
            row['count'] += 1
            row['wait_ms_total'] += wait
            row['hold_ms_total'] += hold
            row['wait_ms_max'] = max(row['wait_ms_max'], wait)
            row['hold_ms_max'] = max(row['hold_ms_max'], hold)
            row['slow_count'] += int(slow)
            if now - self._last_summary >= SUMMARY_SECONDS:
                summary, self._stats = self._stats, {}
                self._last_summary = now
        for kind, duration in (('wait', wait), ('hold', hold)):
            if duration >= SLOW_MS:
                diag.throttled('lock.slow', (self.name, owner, kind), interval=30,
                               lock=self.name, owner=owner, kind=kind,
                               blocked_by=blocked_by if kind == 'wait' else None,
                               wait_ms=round(wait, 1), hold_ms=round(hold, 1))
        if summary is not None:
            diag.event('lock.summary', lock=self.name, owners={
                owner: {key: round(value, 1) if isinstance(value, float) else value
                        for key, value in row.items()} for owner, row in summary.items()})

    def locked(self):
        method = getattr(self.inner, 'locked', None)
        return method() if method else bool(self._state)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


_tick_lock = threading.Lock()
_tick_last = _clock()
_tick_count = 0
_tick_stats = {}
_tick_counters = {}


class Phases:
    def __init__(self, observation):
        self.observation = observation
        self.started = self.last = _clock()
        self.name = None
        self.durations = {}
        self.counts = {}
        self.hold_started = None
        self.hold_ms = 0
        self.hold_ended = False

    def phase(self, name):
        try:
            now = _clock()
            if self.name is not None:
                self.durations[self.name] = self.durations.get(self.name, 0) + (now - self.last) * 1000
            self.last, self.name = now, name
            self.observation.owner = 'worker.' + name
            if name == 'poll_finish':
                self.hold_started = now
        except Exception:
            pass

    @contextmanager
    def holding(self):
        self.phase('poll_finish')
        try:
            yield
        finally:
            try:
                now = _clock()
                self.hold_ms = (now - self.hold_started) * 1000
                if self.name is not None:
                    self.durations[self.name] = self.durations.get(self.name, 0) + (now - self.last) * 1000
                self.name = None
                self.hold_ended = True
            except Exception:
                pass

    def finish(self):
        global _tick_last, _tick_count, _tick_stats, _tick_counters
        try:
            now = _clock()
            if self.hold_started is not None and not self.hold_ended:
                self.hold_ms = (now - self.hold_started) * 1000
            if not self.hold_ended:
                self.phase('done')
            self.hold_ms = self.observation.metrics.get('web', (0, 0, self.hold_ms))[2]
            summary = None
            with _tick_lock:
                _tick_count += 1
                for name, duration in self.durations.items():
                    row = _tick_stats.setdefault(name, {'total_ms': 0, 'max_ms': 0})
                    row['total_ms'] += duration
                    row['max_ms'] = max(row['max_ms'], duration)
                # 计数沿用 finished_skip.COUNTERS；汇总为窗口内处理/跳过的项目总数。
                for name, count in self.counts.items():
                    _tick_counters[name] = _tick_counters.get(name, 0) + count
                if now - _tick_last >= SUMMARY_SECONDS:
                    summary = (_tick_count, _tick_stats, _tick_counters)
                    _tick_counters = {}
                    _tick_count, _tick_stats, _tick_last = 0, {}, now
            if self.hold_ms >= SLOW_MS:
                diag.throttled('scheduler.tick_slow', 'worker', interval=60,
                               total_ms=round((now - self.started) * 1000, 1),
                               lock_hold_ms=round(self.hold_ms, 1),
                               phases_ms={k: round(v, 1) for k, v in self.durations.items()},
                               **self.counts)
            if summary:
                diag.event('scheduler.tick_summary', tick_count=summary[0], phases_ms={
                    k: {key: round(value, 1) for key, value in row.items()}
                    for k, row in summary[1].items()}, **summary[2])
        except Exception:
            pass


@contextmanager
def phases():
    with observe('worker.recover_outbox') as observation:
        observation.holder_owner = 'worker.tick'
        timer = Phases(observation)
        try:
            yield timer
        finally:
            try:
                timer.finish()
            except Exception:
                pass
