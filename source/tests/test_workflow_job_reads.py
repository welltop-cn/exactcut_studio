"""Workflow 的任务扫描必须保持只读，隔离仍由调度器负责。"""
import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from exactcut import diagnostics as diag, workflow as w, webserver as web
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued
from test_workflow import workspace
from test_worker_bad_job_isolation import isolated, assert_one_alert
from test_worker_tick_behavior_lock import scheduler, finish_owned, saved
from test_web_base_path import app, login, request


def production_projects():
    ids = []
    for _ in range(3):
        bid = w.create_blank()['id']
        ids.append(bid)
        path = w.folder(bid)
        state = read_json(path / 'runtime.json')
        state.update(request_parallel=True, status='review')
        write_json_atomic(path / 'runtime.json', state)
        draft = read_json(path / 'draft.json')
        draft['draft'].update(drama_id='123456', business_date='20261108', author='fixture',
                              rows=[{'request_id': 'r1', 'seq': 1, 'timeline_text': '字卡|fixture|1.5s'}])
        write_json_atomic(path / 'draft.json', draft)
        manifest, errors = w.validate_draft(draft['draft'], str(path / 'source.xlsx'))
        assert not errors
        write_json_atomic(path / 'confirmed.json', manifest.model_dump(mode='json'))
        write_json_atomic(path / 'production/results.json', {'requests': {'r1': {'status': 'qc_passed'}}})
    return ids


@pytest.mark.parametrize('mode', ['load', 'summary'])
@pytest.mark.parametrize('payload', ['valid', 'structure', 'json'])
def test_read_only_jobs_finish_while_control_lock_is_held(workspace, monkeypatch, mode, payload):
    from exactcut import job_records
    bid = production_projects()[0]
    jobs = workspace / '.web-state/jobs'
    jobs.mkdir()
    file = jobs / 'fixture.json'
    job = {'id': 'fixture', 'batch_id': bid, 'phase': 'render',
           'status': 'queued', 'request_id': 'r1'}
    write_json_atomic(file, {**job, 'status': []} if payload == 'structure' else job)
    if payload == 'json':
        file.write_bytes(b'{broken')
    before = {p.relative_to(workspace): p.read_bytes()
              for p in workspace.rglob('*') if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail('只读调用不得隔离、告警或发送信号')
    monkeypatch.setattr(job_records, 'quarantine', forbidden)
    monkeypatch.setattr(job_records, 'alert', forbidden)
    monkeypatch.setattr(w.batch_control, 'signal_identity', forbidden)

    def read():
        if mode == 'load':
            return job_records.load(file, jobs, w.BATCHES, isolate=False)
        return w.production_summary(w.folder(bid), {'request_parallel': True})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with w.batch_control.LOCK:
            future = pool.submit(read)
            result = future.result(timeout=2)
    if mode == 'load':
        assert result == (job if payload == 'valid' else None)
    else:
        assert result['status'] == ('producing' if payload == 'valid' else 'review')
    assert {p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob('*') if p.is_file()} == before


def test_isolating_load_waits_for_control_lock(workspace, monkeypatch):
    from exactcut import job_records
    jobs = workspace / '.web-state/jobs'
    jobs.mkdir(parents=True)
    file = jobs / 'broken.json'
    file.write_bytes(b'{broken')
    marker = jobs.parent / 'job-quarantine/broken.json.json'
    attempted = threading.Event()
    original_lock = w.batch_control.LOCK

    class ObservedLock:
        def __enter__(self):
            attempted.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    monkeypatch.setattr(w.batch_control, 'LOCK', ObservedLock())
    alerts = []
    monkeypatch.setattr(job_records, 'alert', lambda *args: alerts.append(args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original_lock:
            future = pool.submit(job_records.load, file, jobs, w.BATCHES, isolate=True)
            assert attempted.wait(timeout=2)
            assert not future.done()
            assert not marker.exists() and not alerts
        assert future.result(timeout=2) is None
    assert marker.exists() and len(alerts) == 1
    assert file.read_bytes() == b'{broken'


@pytest.mark.parametrize('content', [b'{bad\n ', b'\xff', b'[]',
    b'{"id":"broken","phase":"render","status":[]}',
    b'{"id":"broken","phase":"render","status":"queued","request_id":[]}',
    b'{"id":"broken","phase":"render","status":"queued","created":"bad"}',
    b'{"id":"mismatch","phase":"render","status":"queued"}',
    b'{"id":"broken","phase":"render","status":"queued","quarantine":{"reason":"fixture"}}'])
@pytest.mark.parametrize('mode', ['compact', 'list', 'production', 'detail'])
def test_workflow_reads_skip_invalid_jobs_without_isolation(workspace, monkeypatch, content, mode):
    ids = production_projects()
    jobs = workspace / '.web-state/jobs'
    jobs.mkdir()
    broken = jobs / 'broken.json'
    if content.startswith(b'{"id"'):
        content = json.dumps({**json.loads(content), 'batch_id': ids[1]}).encode()
    broken.write_bytes(content)
    write_json_atomic(jobs / 'healthy.json', {'id': 'healthy', 'batch_id': ids[0],
                      'phase': 'render', 'status': 'queued', 'request_id': 'r1'})
    before = {file.name: file.read_bytes() for file in jobs.iterdir()}
    # 即使坏任务可能关联活进程，读取也绝不能进入隔离/信号路径或告警。
    def forbidden(*args, **kwargs):
        pytest.fail('只读任务扫描不得隔离、发送信号或告警')
    from exactcut import job_records
    monkeypatch.setattr(job_records, 'quarantine', forbidden)
    monkeypatch.setattr(job_records, 'alert', forbidden)
    monkeypatch.setattr(w.batch_control, 'signal_identity', forbidden)
    if mode == 'production':
        states = [w.production_summary(w.folder(bid), {'request_parallel': True})['status'] for bid in ids]
    elif mode == 'detail':
        details = [w.detail(bid) for bid in ids]
        assert all(not item['errors'] for item in details)
        states = [item['runtime']['status'] for item in details]
    else:
        rows = w.summaries(compact=mode == 'compact')
        states = [next(row['status'] for row in rows if row['id'] == bid) for bid in ids]
    assert states == ['producing', 'review', 'review']
    assert broken.read_bytes() == content
    assert {file.name: file.read_bytes() for file in jobs.iterdir()} == before
    assert not (jobs.parent / 'job-quarantine').exists()


@pytest.mark.parametrize('compact', [True, False])
@pytest.mark.parametrize('error', [PermissionError(errno.EACCES, 'fixture'), OSError(errno.EIO, 'fixture')])
def test_workflow_job_io_errors_propagate(workspace, monkeypatch, compact, error):
    production_projects()
    jobs = workspace / '.web-state/jobs'
    jobs.mkdir()
    file = jobs / 'broken.json'
    file.write_bytes(b'{}')
    original = w.read_json
    def failing(path):
        if path == file: raise error
        return original(path)
    monkeypatch.setattr(w, 'read_json', failing)
    with pytest.raises(type(error)) as caught:
        w.summaries(compact=compact)
    assert caught.value is error


def test_workflow_read_before_scheduler_preserves_unique_alert(scheduler, isolated):
    pending, owned = scheduler
    production_projects()
    broken = web.JOBS / 'broken.json'
    original = b'{bad\n '
    broken.write_bytes(original)
    healthy = queued('a', 'fetch')
    for compact in (True, False):
        assert len(w.summaries(compact=compact)) == 3
    assert broken.read_bytes() == original
    assert not (web.JOBS.parent / 'job-quarantine').exists()
    assert not [row for file in (isolated / 'logs').glob('events-*.jsonl')
                for row in map(json.loads, file.read_text().splitlines())
                if row['event'] == 'job.quarantined']
    web.worker_tick(pending, owned)
    assert list(owned) == [healthy['id']]
    finish_owned(owned)
    diag._throttled.clear()
    web.recover_jobs(enqueue=True)
    web.worker_tick(pending, owned)
    assert saved(healthy)['status'] == 'completed' and not owned
    assert broken.read_bytes() == original
    assert_one_alert(isolated, 'broken', None)


def test_http_workflow_routes_preserve_broken_json(app, monkeypatch):
    headers = login(app)
    ids = production_projects()
    jobs = w.ROOT / '.web-state/jobs'
    jobs.mkdir(exist_ok=True)
    broken = jobs / 'broken.json'
    original = b'{bad\n '
    broken.write_bytes(original)
    from exactcut import job_records
    def forbidden(*args, **kwargs):
        pytest.fail('HTTP 读取不得隔离或告警')
    monkeypatch.setattr(job_records, 'quarantine', forbidden)
    monkeypatch.setattr(job_records, 'alert', forbidden)
    for route in ('/home/', '/home/api/batches/sidebar', '/home/api/batches',
                  *('/home/api/batches/' + bid for bid in ids)):
        assert request(app, 'GET', route, headers=headers)[0] == 200
    assert broken.read_bytes() == original
    assert not (jobs.parent / 'job-quarantine').exists()
