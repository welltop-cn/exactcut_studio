"""Observations preserve threading and scheduler behavior."""
import json
import threading
import time

import pytest
from exactcut import batch_control, diagnostics as diag, lock_timing as timing, webserver as web
from test_web_base_path import app, login, request
from test_concurrency import runner


def records(root):
    return [json.loads(line) for path in (root / 'logs').glob('events-*.jsonl')
            for line in path.read_text().splitlines()]


@pytest.fixture
def logs(tmp_path, monkeypatch):
    diag.configure(tmp_path, {})
    monkeypatch.setattr(diag, '_throttled', {})
    yield tmp_path
    diag.disable()


def test_semantics():
    lock = timing.TimedLock(threading.Lock(), 'web')
    assert not lock.locked()
    assert lock.acquire(blocking=False)
    assert lock.locked()
    assert not lock.acquire(False)
    assert not lock.acquire(timeout=.01)
    with pytest.raises(ValueError):
        lock.acquire(False, .01)
    lock.release()
    with pytest.raises(RuntimeError):
        lock.release()
    with pytest.raises(LookupError):
        with lock:
            raise LookupError()
    assert not lock.locked()
    recursive = timing.TimedLock(threading.RLock(), 'web')
    with timing.observe('recursive') as observation:
        with recursive:
            with recursive:
                assert recursive.locked()
            assert recursive.locked()
        assert observation.fields()['lock_count'] == 1
    assert not recursive.locked()


def test_wait_hold_and_mutual_exclusion(logs):
    lock = timing.TimedLock(threading.Lock(), 'web')
    ready, waiting = threading.Event(), threading.Event()
    result = {}

    def holder():
        with timing.observe('holder') as observation:
            with lock:
                ready.set()
                assert waiting.wait(2)
                time.sleep(.3)
            result['hold'] = observation.fields()

    def waiter():
        assert ready.wait(2)
        with diag.context(trace_id='lock-test'), timing.observe('waiter') as observation:
            waiting.set()
            with lock:
                pass
            result['wait'] = observation.fields()

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert 250 <= result['wait']['lock_wait_ms'] < 2000
    assert 250 <= result['hold']['lock_hold_ms'] < 2000
    rows = [r for r in records(logs) if r['event'] == 'lock.slow']
    row = next(r for r in rows if r['owner'] == 'waiter')
    assert row['blocked_by'] == 'holder' and row['trace_id'] == 'lock-test'
    assert {'lock', 'owner', 'blocked_by', 'wait_ms', 'hold_ms'} <= row.keys()


def test_threshold_throttle_summary(logs, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(timing, '_clock', lambda: clock[0])
    monkeypatch.setattr(diag.time, 'monotonic', lambda: clock[0])
    lock = timing.TimedLock(threading.Lock(), 'web')
    with timing.observe('test'):
        with lock:
            clock[0] += .01
        assert not records(logs)
        for _ in range(2):
            with lock:
                clock[0] += .3
        assert len([r for r in records(logs) if r['event'] == 'lock.slow']) == 1
        clock[0] += 60
        with lock:
            clock[0] += .3
        with lock:
            pass
        rows = records(logs)
        assert len([r for r in rows if r['event'] == 'lock.slow']) == 2
        summaries = [r for r in rows if r['event'] == 'lock.summary']
        assert len(summaries) == 1
        row = summaries[0]['owners']['test']
        assert row['count'] == 4 and row['slow_count'] == 3
        assert row['hold_ms_max'] == pytest.approx(300)
        clock[0] += 60
        with lock:
            pass
        assert len([r for r in records(logs) if r['event'] == 'lock.summary']) == 2


def test_route_owner():
    assert timing.route_owner('GET', '/api/batches/' + 'a' * 32 + '?user=private') == 'GET /api/batches/:id'
    assert timing.route_owner('POST', '/api/users/private-name') == 'POST /api/users/:part'
    assert timing.route_owner('GET', '/api/batches/sidebar') == 'GET /api/batches/sidebar'


def test_http_metrics(app, logs):
    headers = login(app)
    bid = json.loads(request(app, 'POST', '/home/api/batches/blank', {}, headers)[2])['id']
    for route in ('/api/batches/sidebar', '/api/batches/' + bid):
        status, response_headers, _ = request(app, 'GET', '/home' + route, headers=headers)
        assert status == 200
        deadline = time.monotonic() + 2
        while True:
            rows = [r for r in records(logs) if r['event'] == 'http.completed'
                    and r.get('trace_id') == response_headers['X-Request-ID']]
            if rows or time.monotonic() > deadline:
                break
            time.sleep(.005)
        assert len(rows) == 1
        assert rows[0]['route'] == route
        assert {'lock_wait_ms', 'lock_hold_ms', 'lock_count'} <= rows[0].keys()
        assert rows[0]['lock_count'] > 0


def test_worker_phases_and_blocker(runner, logs, monkeypatch):
    entered, started = threading.Event(), threading.Event()
    original = batch_control.reconcile
    errors = []

    def reconcile(*args, **kwargs):
        entered.set()
        assert started.wait(2)
        time.sleep(.25)
        return original(*args, **kwargs)

    monkeypatch.setattr(batch_control, 'reconcile', reconcile)
    monkeypatch.setattr(timing, '_tick_last', timing._clock() - 61)
    monkeypatch.setattr(timing, '_tick_stats', {})
    monkeypatch.setattr(timing, '_tick_count', 0)
    monkeypatch.setattr(web, 'LOCK', timing.TimedLock(threading.Lock(), 'web'))
    web.LOCK._last_summary -= 61

    def tick():
        try:
            web.worker_tick([], {})
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=tick)
    thread.start()
    assert entered.wait(2)
    with diag.context(trace_id='concurrent-request'), timing.observe('GET /api/batches/sidebar'):
        started.set()
        with web.LOCK:
            pass
    thread.join(5)
    assert not thread.is_alive() and not errors
    rows = records(logs)
    slow = next(r for r in rows if r['event'] == 'scheduler.tick_slow')
    assert max(slow['phases_ms'], key=slow['phases_ms'].get) == 'reconcile'
    assert slow['phases_ms']['reconcile'] >= 240
    assert {'recover_outbox', 'drain', 'lock_wait', 'poll_finish', 'recover_jobs',
            'external', 'reconcile', 'durable_scan', 'resource_usage', 'dispatch'} <= slow['phases_ms'].keys()
    wait = next(r for r in rows if r['event'] == 'lock.slow' and r.get('trace_id') == 'concurrent-request')
    assert wait['blocked_by'] == 'worker.reconcile'
    assert wait['owner'] == 'GET /api/batches/sidebar'
    holds = [r for r in rows if r['event'] == 'lock.slow' and r['kind'] == 'hold']
    assert holds and all(r['owner'] == 'worker.tick' for r in holds)
    lock_summaries = [r for r in rows if r['event'] == 'lock.summary']
    assert lock_summaries
    assert all('worker.tick' in r['owners'] and 'worker.lock_wait' not in r['owners']
               for r in lock_summaries)
    summary = next(r for r in rows if r['event'] == 'scheduler.tick_summary')
    assert summary['tick_count'] == 1
    # A second tick inside the minute must not emit a summary.
    monkeypatch.setattr(batch_control, 'reconcile', original)
    web.worker_tick([], {})
    assert len([r for r in records(logs) if r['event'] == 'scheduler.tick_summary']) == 1


def test_no_diagnostic_io_inside_nested_locks(monkeypatch):
    outer = timing.TimedLock(threading.Lock(), 'web')
    inner = timing.TimedLock(threading.RLock(), 'batch_control')
    writes = []

    def emit(*args, **kwargs):
        assert not outer.locked() and not inner.locked()
        writes.append(args[0])

    monkeypatch.setattr(timing, 'SLOW_MS', 0)
    monkeypatch.setattr(diag, 'throttled', emit)
    with outer, inner:
        with inner:
            pass
        assert not writes
    assert writes == ['lock.slow'] * 4


@pytest.mark.parametrize('failure', ['clock', 'record', 'diagnostic'])
def test_observation_failure_does_not_change_business(monkeypatch, failure):
    lock = timing.TimedLock(threading.Lock(), 'web')

    def fail(*args, **kwargs):
        raise RuntimeError('observation failure')

    if failure == 'clock':
        monkeypatch.setattr(timing.time, 'perf_counter', fail)
    elif failure == 'record':
        monkeypatch.setattr(lock, '_record', fail)
    else:
        monkeypatch.setattr(timing, 'SLOW_MS', 0)
        monkeypatch.setattr(diag, 'throttled', fail)
    with lock:
        assert lock.locked()
    assert not lock.locked()
    assert lock.acquire(False)
    lock.release()


def test_uncontended_overhead():
    def elapsed(lock):
        started = time.perf_counter()
        for _ in range(100000):
            lock.acquire()
            lock.release()
        return time.perf_counter() - started

    bare = elapsed(threading.Lock())
    wrapped = elapsed(timing.TimedLock(threading.Lock(), 'benchmark'))
    assert (wrapped - bare) / 100000 < .00001


def test_handler_in_memory(runner, logs, monkeypatch):
    """Exercise parsing, real routes and response headers without a listening socket."""
    from io import BytesIO
    from types import SimpleNamespace
    from exactcut import workflow

    def authorized(self):
        self.user = {'username': 'admin', 'role': 'admin'}
        self.session_token = ''
        return True

    monkeypatch.setattr(web.Handler, 'authorized', authorized)
    bid = workflow.create_blank()['id']
    for route in ('/api/batches/sidebar', '/api/batches/' + bid):
        handler = web.Handler.__new__(web.Handler)
        handler.rfile = BytesIO(('GET /home' + route + ' HTTP/1.0\r\n\r\n').encode())
        handler.wfile = BytesIO()
        handler.server = SimpleNamespace(base_path='/home')
        handler.client_address = ('fixture', 0)
        handler.handle_one_request()
        headers = handler.wfile.getvalue().split(b'\r\n\r\n')[0].decode()
        assert headers.startswith('HTTP/1.0 200'), handler.wfile.getvalue()
        trace = next(line.partition(': ')[2] for line in headers.split('\r\n')
                     if line.startswith('X-Request-ID:'))
        row = next(row for row in records(logs) if row['event'] == 'http.completed'
                   and row.get('trace_id') == trace)
        assert row['lock_count'] > 0
        assert row['lock_wait_ms'] >= 0 and row['lock_hold_ms'] >= 0


def test_worker_timing_failure(runner, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError('diagnostics unavailable')

    monkeypatch.setattr(timing.time, 'perf_counter', fail)
    monkeypatch.setattr(timing.diag, 'event', fail)
    monkeypatch.setattr(timing, 'SLOW_MS', -1)
    # Existing business diagnostics use throttled too; isolate the new event failures.
    original = timing.diag.throttled
    monkeypatch.setattr(timing.diag, 'throttled', lambda name, *a, **kw:
                        fail() if name.startswith(('lock.', 'scheduler.tick_')) else original(name, *a, **kw))
    # Keep pre-existing scheduler.snapshot from attempting a write via the patched event.
    monkeypatch.setitem(diag._throttled, ('scheduler.snapshot', 'worker'), time.monotonic())
    web.worker_tick([], {})
    assert not web.LOCK.locked()


def test_failed_http_includes_lock_metrics(logs, monkeypatch):
    from http.server import BaseHTTPRequestHandler
    handler = web.Handler.__new__(web.Handler)
    handler.command = 'GET'

    def fail(self):
        self.diagnostic_route = '/api/batches/sidebar'
        with web.LOCK:
            raise LookupError('business exception')

    monkeypatch.setattr(BaseHTTPRequestHandler, 'handle_one_request', fail)
    with pytest.raises(LookupError, match='business exception'):
        handler.handle_one_request()
    row = next(row for row in records(logs) if row['event'] == 'http.failed')
    assert row['trace_id'] == handler.trace_id
    assert row['lock_count'] == 1 and row['lock_hold_ms'] >= 0 and row['lock_wait_ms'] >= 0
    assert not web.LOCK.locked()
