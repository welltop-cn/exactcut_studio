"""真实文件的 queue 只读扫描、并发与调度隔离回归。"""
import threading
from pathlib import Path

import pytest

from exactcut import accounts, webserver as web
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs
from test_worker_bad_job_isolation import isolated, reserve_bad, assert_released, install_controlled_signal


ADMIN = {'username': 'admin', 'role': 'admin'}


def test_queue_completes_while_another_thread_holds_both_global_locks(isolated):
    batch = web.workflow.BATCHES / ('c' * 32)
    write_json_atomic(batch / 'draft.json', {'display_name': '持锁回归'})
    write_json_atomic(batch / 'runtime.json', {'status': 'producing'})
    job = queued('a', 'render', batch=batch.name)
    history = queued('b', 'fetch')
    web.save({**history, 'status': 'completed'})
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []

    def holder():
        with web.LOCK, web.batch_control.LOCK:
            held.set()
            release.wait(5)

    def query():
        try:
            results.append(web.queue_snapshot(ADMIN))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    holding = threading.Thread(target=holder)
    reading = threading.Thread(target=query)
    holding.start()
    try:
        assert held.wait(2)
        reading.start()
        assert done.wait(1), 'queue 被另一线程持有的全局锁阻塞'
        assert not errors
        assert [task['id'] for task in results[0]['tasks']] == [job['id']]
    finally:
        release.set()
        holding.join(3)
        if reading.ident is not None:
            reading.join(3)
        assert not holding.is_alive() and not reading.is_alive()


@pytest.mark.parametrize('payload', ['structure', 'json'])
def test_queue_is_readonly_then_worker_still_quarantines(isolated, monkeypatch, payload):
    bad = queued('a', 'render')
    bad.update(status='running', pid=123, pgid=123, process_token='c' * 32)
    web.batch_control.PROCESSES.rows[123] = {
        'pgid': 123, 'state': 'S',
        'arguments': ['python', '-X', 'exactcut_process_token=' + 'c' * 32]}
    file = web.JOBS / (bad['id'] + '.json')
    write_json_atomic(file, {**bad, 'admission': ['invalid']})
    if payload == 'json':
        file.write_bytes(b'{truncated')
    reserve_bad(bad, isolated)
    healthy = queued('b', 'fetch')
    before = {p: p.read_bytes() for p in isolated.rglob('*') if p.is_file()}
    for _ in range(2):
        assert [task['id'] for task in web.queue_snapshot(ADMIN)['tasks']] == [healthy['id']]
    assert {p: p.read_bytes() for p in isolated.rglob('*') if p.is_file()} == before
    assert web.batch_control.PROCESSES.inspect(123)['state'] == 'S'
    calls = install_controlled_signal(monkeypatch, 123)
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert set(owned) == {healthy['id']}
        assert len(calls) == (1 if payload == 'structure' else 0)
        assert_released(bad['id'])
        if payload == 'structure':
            assert read_json(file)['quarantine']
        else:
            assert file.read_bytes() == before[file]
            assert (web.JOBS.parent / 'job-quarantine' / (file.name + '.json')).is_file()
    finally:
        close_logs(owned)


@pytest.mark.parametrize('stage', ['stat', 'open'])
def test_queue_skips_file_disappearing_after_glob(isolated, monkeypatch, stage):
    job = queued('a', 'fetch')
    file = web.JOBS / (job['id'] + '.json')
    original = getattr(Path, stage)

    def vanished(path, *args, **kwargs):
        if path == file:
            file.unlink(missing_ok=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, stage, vanished)
    assert web.queue_snapshot(ADMIN)['tasks'] == []
    assert not (web.JOBS.parent / 'job-quarantine').exists()


@pytest.mark.parametrize('error', [PermissionError, OSError])
@pytest.mark.parametrize('stage', ['stat', 'open'])
def test_queue_exposes_real_io_errors(isolated, monkeypatch, stage, error):
    job = queued('a', 'fetch')
    file = web.JOBS / (job['id'] + '.json')
    original = getattr(Path, stage)

    def failed(path, *args, **kwargs):
        if path == file:
            raise error('synthetic storage failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, stage, failed)
    with pytest.raises(error, match='synthetic storage failure'):
        web.queue_snapshot(ADMIN)
    assert not (web.JOBS.parent / 'job-quarantine').exists()


def test_queue_atomic_replace_between_stat_and_open_reads_complete_new_job(isolated, monkeypatch):
    job = queued('a', 'fetch')
    file = web.JOBS / (job['id'] + '.json')
    original = Path.open
    replaced = False

    def replace_before_open(path, *args, **kwargs):
        nonlocal replaced
        if path == file and not replaced:
            replaced = True
            write_json_atomic(file, {**job, 'lane': 'render', 'status': 'running', 'started': 100})
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', replace_before_open)
    task, = web.queue_snapshot(ADMIN)['tasks']
    assert replaced and task['lane'] == 'render' and task['status'] == 'running'
    assert not (web.JOBS.parent / 'job-quarantine').exists()


def test_queue_open_inode_survives_atomic_replace(isolated, monkeypatch):
    job = queued('a', 'fetch')
    file = web.JOBS / (job['id'] + '.json')
    original = Path.open
    replaced = False

    def replace_after_open(path, *args, **kwargs):
        nonlocal replaced
        stream = original(path, *args, **kwargs)
        if path == file and not replaced:
            replaced = True
            write_json_atomic(file, {**job, 'lane': 'render'})
        return stream

    monkeypatch.setattr(Path, 'open', replace_after_open)
    assert web.queue_snapshot(ADMIN)['tasks'][0]['lane'] == 'fetch'
    assert web.queue_snapshot(ADMIN)['tasks'][0]['lane'] == 'render'


def test_active_selection_matches_legacy_display_before_sorting(isolated, monkeypatch):
    from exactcut import material_versions as media
    monkeypatch.setattr(web.time, 'time', lambda: 500)
    for key, control in [('c', {}), ('d', {'paused': True}), ('e', {'archived': True})]:
        path = web.workflow.BATCHES / (key * 32)
        write_json_atomic(path / 'draft.json', {'display_name': key})
        write_json_atomic(path / 'runtime.json', {'status': 'producing'})
        write_json_atomic(path / 'control.json', control)
        accounts.assign(path, {'username': 'alice' if key == 'c' else 'bob'})
    for key, lane, status, batch in [
        ('a', 'fetch', 'queued', None), ('b', 'render', 'running', 'c' * 32),
        ('c', 'render', 'queued', 'd' * 32), ('d', 'fetch', 'running', 'e' * 32),
        ('e', 'render', 'completed', None), ('f', 'upload', 'queued', None),
        ('g', 'poll', 'running', None), ('h', 'fetch', 'failed', None),
        ('i', 'render', 'queued', 'f' * 32), ('j', 'fetch', 'queued', None),
    ]:
        job = queued(key, lane, batch=batch)
        job.update(status=status, created=200 - ord(key), started=120, request_id=None)
        if key == 'a': job['not_before'] = 600
        if key == 'j': job['instance'] = 'foreign-instance'
        web.save(job)
    # 真实子项目草稿；父映射保持旧展示语义，父状态读取用固定 fixture。
    child = web.workflow.BATCHES / ('b' * 32)
    identity = {'parent_batch_id': 'c' * 32, 'asset_id': 'asset', 'kind': 'current'}
    write_json_atomic(child / 'draft.json', {'media_internal': identity})
    write_json_atomic(child / 'runtime.json', {'status': 'producing'})
    monkeypatch.setattr(media, 'read', lambda _: {'deleted_at': None, 'assets': {'asset': {'deleted_at': None}},
                                               'control': {'paused': False, 'archived': False}})
    queued('k', 'render', batch=child.name)
    legacy_jobs = sorted(web.stored_jobs(), key=lambda item: item.get('created', 0))
    # 旧循环：先全量排序，再在 queue_snapshot 内过滤。
    actual = {role: web.queue_snapshot({'username': 'alice', 'role': role}) for role in ('admin', 'user')}
    with monkeypatch.context() as patch:
        patch.setattr(web, 'queue_jobs', lambda: iter(legacy_jobs))
        expected = {role: web.queue_snapshot({'username': 'alice', 'role': role}) for role in ('admin', 'user')}
    assert actual == expected
    tasks = actual['admin']['tasks']
    assert [t['id'] for t in tasks] == ['d' * 32, 'c' * 32, 'b' * 32, 'a' * 32, 'k' * 32]
    assert {t['status'] for t in tasks} == {'queued', 'running', 'paused'}
    assert tasks[-1]['batch_id'] == 'c' * 32 and tasks[-1]['owner'] == 'alice'
    assert tasks[-1]['observation']
    active = list(web.queue_jobs())
    assert {j['id'] for j in active} == {j['id'] for j in legacy_jobs
        if j.get('status') in {'queued', 'running'} and web.concurrency.lane(j) in {'fetch', 'render'}}

