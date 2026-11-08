"""单轮 marker 与恢复后只读 job 列表复用，保留写后可见性。"""
import threading

import pytest
from exactcut import runtime, tick_snapshot as snap, webserver as web
from exactcut.util import write_json_atomic
from test_concurrency import runner
from test_workflow import workspace
from test_worker_tick_behavior_lock import scheduler, batch, saved


def test_marker_tick_scope_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'one'))
    original, calls = runtime.hashlib.sha256, []
    def hashed(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(runtime.hashlib, 'sha256', hashed)
    with snap.scope():
        first = runtime.instance_marker()
        assert runtime.instance_marker() == first
        assert len(calls) == 1
        other = []
        thread = threading.Thread(target=lambda: other.append(runtime.instance_marker()))
        thread.start(); thread.join()
        assert other == [first] and len(calls) == 2
    assert runtime.instance_marker() == first
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'two'))
    second = runtime.instance_marker()
    assert second != first
    with snap.scope():
        assert runtime.instance_marker() == second
        assert runtime.instance_marker() == second
    assert len(calls) == 5


@pytest.mark.parametrize('threaded', [False, True])
def test_reuse_invalidates_new_and_updated_jobs(tmp_path, monkeypatch, threaded):
    monkeypatch.setattr(web, 'JOBS', tmp_path)
    write_json_atomic(tmp_path / 'one.json', {'id': 'one', 'status': 'queued'})
    with snap.scope():
        first = snap.job_list(tmp_path, web.stored_jobs)
        assert snap.job_list(tmp_path, lambda: pytest.fail('repeated scan')) is first
        def write():
            write_json_atomic(tmp_path / 'one.json', {'id': 'one', 'status': 'completed'})
            write_json_atomic(tmp_path / 'two.json', {'id': 'two', 'status': 'queued'})
        if threaded:
            thread = threading.Thread(target=write); thread.start(); thread.join()
        else:
            write()
        latest = list(web.stored_jobs(reuse=True))
        assert {j['id']: j['status'] for j in latest} == {'one': 'completed', 'two': 'queued'}


@pytest.mark.parametrize('boundary', ['recovery', 'reconcile'])
def test_tick_dispatches_job_written_after_snapshot(scheduler, monkeypatch, boundary):
    pending, owned = scheduler
    path = batch('a')
    successor = {'id': 'successor', 'batch_id': path.name, 'status': 'queued',
                 'command': ['python', '-m', 'exactcut', 'noop'], 'action': 'start'}
    if boundary == 'recovery':
        # Recovery's first jobs scan precedes verification handoff/successors.
        original = web.workflow.recover_verification_queue
        def recover(jobs, **kwargs):
            original(jobs, **kwargs)
            web.save(dict(successor))
        monkeypatch.setattr(web.workflow, 'recover_verification_queue', recover)
    else:
        # Durable scan must see writes even after the external scan used the list.
        original = web.batch_control.reconcile
        def reconcile(*args, **kwargs):
            result = original(*args, **kwargs)
            web.save(dict(successor))
            return result
        monkeypatch.setattr(web.batch_control, 'reconcile', reconcile)
    web.worker_tick(pending, owned)
    assert 'successor' in owned
    assert saved(successor)['status'] == 'running'


def test_steady_tick_reads_each_job_twice(scheduler, monkeypatch):
    pending, owned = scheduler
    web.save({'id': 'finished', 'status': 'completed'})
    original, calls = snap.job, []
    def read(file, loader):
        calls.append(file.name)
        return original(file, loader)
    monkeypatch.setattr(snap, 'job', read)
    for _ in range(2):
        calls.clear()
        web.worker_tick(pending, owned)
        assert calls == ['finished.json', 'finished.json']
