"""Round 1 evidence and physical disk accounting regressions; no external clients."""
import os
import threading
import time
from unittest.mock import Mock
import pytest
from exactcut import stage_results, storage, workflow, webserver as web
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs
from test_workflow import workspace, imported, complete_asset



class OwnerLock:
    """Expose per-thread ownership without mistaking another thread's lock for ours."""
    def __init__(self):
        self.lock = threading.RLock()
        self.owner = None
        self.depth = 0

    def acquire(self, *args, **kwargs):
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.owner = threading.get_ident(); self.depth += 1
        return acquired

    def release(self):
        self.depth -= 1
        if not self.depth: self.owner = None
        self.lock.release()

    def __enter__(self): self.acquire(); return self
    def __exit__(self, *args): self.release()

def result_fixture(runner, phase='render'):
    batch = 'd' * 32
    path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {})
    write_json_atomic(path / 'runtime.json', {'status': 'producing', 'events': []})
    job = queued('a', phase, batch=batch)
    job.update(phase=phase, attempt=1, status='completed' if phase == 'fetch' else 'running')
    web.save(job)
    return job, path


@pytest.mark.parametrize('phase', ['fetch', 'upload'])
def test_completed_missing_result_does_not_stick(runner, phase):
    job, path = result_fixture(runner, phase)
    job.update(status='completed', request_id='R1'); web.save(job)
    if phase == 'upload':
        write_json_atomic(path / 'publications.json', {'R1': {'status': 'unverified', 'intent': {'status': 'pending'}}})
    stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    stage_results.result_path(job, web.JOBS).unlink()
    web.recover_jobs()
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['status'] in {'failed', 'unverified'}
    assert '阶段结果' in saved['error']
    assert not [j for j in web.stored_jobs() if j.get('parent_id') == job['id']]
    assert workflow.runtime(path)['status'] not in workflow.ACTIVE


def test_scheduler_legacy_one_gib_hash_does_not_block_other_user(runner, monkeypatch):
    from exactcut import batch_control
    monkeypatch.setattr(web, 'LOCK', OwnerLock())
    monkeypatch.setattr(batch_control, 'LOCK', OwnerLock())
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True)
    with video.open('wb') as stream: stream.truncate(1024**3)
    write_json_atomic(stage_results.result_path(job, web.JOBS), {
        'version': 1, 'job_id': job['id'], 'phase': 'render', 'attempt': 1,
        'input_fingerprint': stage_results.fingerprint(job, workflow.BATCHES, workflow.ROOT),
        'status': 'done', 'exit_code': 0, 'artifacts': [{'path': str(video), 'bytes': video.stat().st_size, 'sha256': 'fixture'}]})
    entered, release, enqueued = threading.Event(), threading.Event(), threading.Event()
    def slow_hash(file):
        if file == video:
            assert web.LOCK.owner != threading.get_ident()
            assert batch_control.LOCK.owner != threading.get_ident()
            entered.set()
            assert release.wait(5)
            return 'fixture'
        return 'other'
    monkeypatch.setattr(stage_results, 'file_sha256', slow_hash)
    pending, owned = [], {}
    tick = threading.Thread(target=web.worker_tick, args=(pending, owned)); tick.start()
    try:
        assert entered.wait(1)
        def enqueue():
            with web.LOCK:
                queued('b', 'fetch', owner='bob'); enqueued.set()
        submit = threading.Thread(target=enqueue); submit.start()
        assert enqueued.wait(0.8), 'another user waits for video hashing under LOCK'
        tick.join(0.1)
        assert not tick.is_alive(), 'scheduler itself must not wait for the hash'
        web.worker_tick(pending, owned)
        assert 'b' * 32 in owned
    finally:
        release.set(); tick.join(5)
        if 'submit' in locals(): submit.join(5)
        close_logs(owned)


@pytest.mark.parametrize('phase', ['upload', 'poll'])
def test_delivery_reserves_approved_final_bytes(workspace, phase, monkeypatch):
    batch = imported(); path, request, video, record = complete_asset(batch)
    workflow.queue_publish(batch['id'], request['request_id'], record['output_sha256'])
    allocations = storage.job_budget({'phase': phase, 'batch_id': batch['id'], 'request_id': request['request_id']}, workflow.BATCHES, workflow.ROOT)
    assert sum(allocations.values()) == video.stat().st_size
    monkeypatch.setattr(storage, 'policy', lambda: {'reserve_bytes': 10})
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: Mock(free=10 + video.stat().st_size))
    jobs = workspace / 'jobs'; jobs.mkdir()
    assert storage.reserve('first', allocations, jobs)
    assert not storage.reserve('second', allocations, jobs)
    storage.release('first', jobs)
    assert storage.reserve('second', allocations, jobs)


def test_same_disk_render_reserves_single_output_and_temporary_peak(workspace, monkeypatch):
    batch = imported(); path, request, video, record = complete_asset(batch)
    from exactcut.models import BatchManifest
    manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
    monkeypatch.setattr(__import__('exactcut.media', fromlist=['resolve_full_episodes']), 'resolve_full_episodes', lambda m, p: m)
    allocations = storage.job_budget({'phase': 'render', 'batch_id': batch['id']}, workflow.BATCHES, workflow.ROOT)
    budget = sum(storage.render_budget([i.model_dump(mode='json') for i in r.timeline], workflow.load_config()['render']) for r in manifest.requests)
    assert sum(allocations.values()) == 2 * budget
    monkeypatch.setattr(storage, 'policy', lambda: {'reserve_bytes': 10})
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: Mock(free=10 + 2 * budget))
    jobs = workspace / 'jobs'; jobs.mkdir()
    assert storage.reserve('first', allocations, jobs)
    assert not storage.reserve('second', allocations, jobs)


def test_fast_result_rejects_same_size_change_with_restored_mtime(runner):
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'original')
    stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    assert stage_results.scheduler_verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)
    before = video.stat(); video.write_bytes(b'tampered')
    os.utime(video, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not stage_results.scheduler_verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)


def test_hash_snapshot_rejects_mutation_during_read(tmp_path, monkeypatch):
    video = tmp_path / 'x.mp4'; video.write_bytes(b'original')
    def mutate(file):
        file.write_bytes(b'tampered')
        return 'old-digest'
    monkeypatch.setattr(stage_results, 'file_sha256', mutate)
    with pytest.raises(ValueError, match='变化'):
        stage_results.digest_evidence(video)


def test_background_pool_has_no_backlog_and_releases_failed_slots():
    entered = [threading.Event(), threading.Event()]; release = threading.Event()
    prefix = 'bounded-' + str(time.monotonic_ns())
    def operation(index):
        entered[index].set(); release.wait(5)
        raise ValueError('fixture failure')
    try:
        for index in range(2):
            with pytest.raises(stage_results.EvidencePending):
                stage_results.background((prefix, index), lambda index=index: operation(index))
        assert all(event.wait(1) for event in entered)
        third = Mock(return_value='third')
        with pytest.raises(stage_results.EvidencePending): stage_results.background((prefix, 2), third)
        third.assert_not_called()
    finally: release.set()
    deadline = time.monotonic() + 2
    for index in range(2):
        while True:
            try:
                with pytest.raises(ValueError, match='fixture failure'):
                    stage_results.background((prefix, index), lambda: None)
                break
            except stage_results.EvidencePending:
                assert time.monotonic() < deadline
                time.sleep(.01)
    while True:
        try:
            assert stage_results.background((prefix, 2), third) == 'third'; break
        except stage_results.EvidencePending:
            assert time.monotonic() < deadline
            time.sleep(.01)
    third.assert_called_once()


def test_dead_owned_child_waiting_for_evidence_releases_render_slot(runner, monkeypatch):
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'fixture')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result.pop('source_evidence'); result.pop('evidence_sha256'); [row.pop('stat') for row in result['artifacts']]
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    entered, release = threading.Event(), threading.Event()
    def slow(file):
        entered.set(); assert release.wait(5)
        return result['artifacts'][0]['sha256']
    monkeypatch.setattr(stage_results, 'file_sha256', slow)
    web.WORK.get_nowait()  # This running job owns its original queue item.
    owned = {job['id']: (job, Mock(poll=lambda: 0), Mock())}
    queued('b', 'render', owner='bob')
    try:
        web.worker_tick([], owned)
        assert entered.wait(1)
        assert job['id'] not in owned
        assert 'b' * 32 in owned
    finally:
        release.set(); close_logs(owned)


def test_corrupt_persisted_digest_cannot_pass_fast_verification(runner):
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'original')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result['artifacts'][0]['sha256'] = 'corrupt'
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    assert not stage_results.scheduler_verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)


def test_render_uses_parent_fetch_bound_digest_without_hashing(workspace, monkeypatch):
    batch = imported(); path, request, video, record = complete_asset(batch)
    jobs = workspace / 'jobs'; jobs.mkdir()
    source = workspace / 'source.mp4'; source.write_bytes(b'original')
    from exactcut import media
    monkeypatch.setattr(media, 'resolve_episode_video', lambda project, episode: source)
    parent = {'id': 'f' * 32, 'phase': 'fetch', 'batch_id': batch['id'], 'attempt': 1}
    write_json_atomic(jobs / (parent['id'] + '.json'), parent)
    stage_results.complete(parent, jobs, workflow.BATCHES, workflow.ROOT, 0)
    render = {'id': 'a' * 32, 'phase': 'render', 'batch_id': batch['id'], 'parent_id': parent['id']}
    expected = stage_results.fingerprint(render, workflow.BATCHES, workflow.ROOT)
    monkeypatch.setattr(stage_results, 'file_sha256', Mock(side_effect=AssertionError('fetch digest must be reused')))
    assert stage_results.scheduler_fingerprint(render, workflow.BATCHES, workflow.ROOT, jobs=jobs) == expected


def test_pending_evidence_fences_same_batch_but_not_another_batch(runner, monkeypatch):
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'fixture')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result.pop('source_evidence'); result.pop('evidence_sha256'); [row.pop('stat') for row in result['artifacts']]
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    entered, release = threading.Event(), threading.Event()
    def slow(file):
        entered.set(); assert release.wait(5)
        return result['artifacts'][0]['sha256']
    monkeypatch.setattr(stage_results, 'file_sha256', slow)
    queued('b', 'fetch', batch=job['batch_id'])
    queued('c', 'upload', owner='bob')
    owned = {}
    try:
        web.worker_tick([], owned)
        assert entered.wait(1)
        assert 'b' * 32 not in owned and 'c' * 32 in owned
    finally: release.set(); close_logs(owned)


def test_legacy_publish_action_reserves_final_bytes(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch)
    allocations = storage.job_budget({'action': 'publish', 'batch_id': batch['id'], 'request_id': request['request_id']}, workflow.BATCHES, workflow.ROOT)
    assert sum(allocations.values()) == video.stat().st_size


def test_legacy_render_budget_probe_is_deferred_without_global_lock(runner, monkeypatch):
    workbook = runner / 'fixture.xlsx'; workbook.write_bytes(b'fixture')
    job = queued('a', 'render'); job['command'] += ['--input', str(workbook), '--project', str(runner / 'project')]; web.save(job)
    entered, release = threading.Event(), threading.Event()
    from exactcut import compiler
    def slow_compile(*args):
        entered.set(); assert release.wait(5)
        raise ValueError('fixture compile failure')
    monkeypatch.setattr(compiler, 'compile_workbook', slow_compile)
    queued('b', 'fetch', owner='bob')
    owned = {}
    tick = threading.Thread(target=web.worker_tick, args=([], owned)); tick.start()
    try:
        assert entered.wait(1)
        tick.join(.8)
        assert not tick.is_alive(), 'legacy workbook/probe cannot block scheduling'
        assert 'b' * 32 in owned
    finally: release.set(); tick.join(5); close_logs(owned)


def test_live_crash_window_is_adopted_before_legacy_result_verification(runner, monkeypatch):
    from exactcut import batch_control as control
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'fixture')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result.pop('source_evidence'); result.pop('evidence_sha256'); [row.pop('stat') for row in result['artifacts']]
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    job.update(pid=123, pgid=123, process_token='a' * 32); web.save(job)
    control.PROCESSES.rows[123] = {'pgid': 123, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'a' * 32]}
    monkeypatch.setattr(stage_results, 'file_sha256', Mock(side_effect=AssertionError('live root must not be verified')))
    web.recover_jobs(scheduler=True)
    assert control.recorded_children(path)[0]['pid'] == 123
    assert read_json(web.JOBS / (job['id'] + '.json'))['adopted_after_restart']


def test_completed_parent_with_successor_never_rechecks_old_media(runner, monkeypatch):
    job, path = result_fixture(runner, 'fetch')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result.pop('source_evidence'); result.pop('evidence_sha256')
    video = path / 'legacy.mp4'; video.write_bytes(b'fixture')
    result['artifacts'] = [{'path': str(video), 'bytes': 7, 'sha256': 'fixture'}]
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    web.advance_stage(job)
    monkeypatch.setattr(stage_results, 'file_sha256', Mock(side_effect=AssertionError('already advanced')))
    waiting = web.recover_jobs(scheduler=True)
    assert not waiting


def test_completed_invalid_artifact_has_visible_failure(runner):
    job, path = result_fixture(runner, 'fetch')
    video = path / 'source.mp4'; video.write_bytes(b'original')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result['artifacts'] = [stage_results.digest_evidence(video)]
    result.pop('evidence_sha256')
    result['evidence_sha256'] = __import__('exactcut.util', fromlist=['json_sha256']).json_sha256(result)
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    video.unlink()
    web.recover_jobs(scheduler=True)
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['status'] == 'failed' and '阶段结果' in saved['error']
    assert workflow.runtime(path)['status'] == 'interrupted'


def test_async_render_source_digest_is_bound_to_current_stat(workspace, monkeypatch):
    batch = imported(); path, request, video, record = complete_asset(batch)
    source = workspace / 'source.mp4'; source.write_bytes(b'original')
    from exactcut import media
    monkeypatch.setattr(media, 'resolve_episode_video', lambda project, episode: source)
    job = {'id': 'a' * 32, 'phase': 'render', 'batch_id': batch['id']}
    expected = stage_results.fingerprint(job, workflow.BATCHES, workflow.ROOT)
    paths = stage_results.source_paths(job, workflow.BATCHES, workflow.ROOT)
    assert all(path == source for path in paths.values())
    deadline = time.monotonic() + 2
    def prepare():
        while True:
            try: return stage_results.scheduler_fingerprint(job, workflow.BATCHES, workflow.ROOT)
            except stage_results.EvidencePending:
                assert time.monotonic() < deadline
                time.sleep(.01)
    assert prepare() == expected
    before = source.stat(); source.write_bytes(b'tampered')
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert prepare() != expected


def test_legacy_validation_mutation_fence_rejects_changed_file(runner, monkeypatch):
    job, path = result_fixture(runner)
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'original')
    result = stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    result.pop('source_evidence'); result.pop('evidence_sha256'); [row.pop('stat') for row in result['artifacts']]
    write_json_atomic(stage_results.result_path(job, web.JOBS), result)
    digest = result['artifacts'][0]['sha256']
    def mutate(file):
        before = file.stat(); file.write_bytes(b'tampered')
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        return digest
    monkeypatch.setattr(stage_results, 'file_sha256', mutate)
    with pytest.raises(stage_results.EvidencePending):
        stage_results.scheduler_verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)
    deadline = time.monotonic() + 2
    while any(not future.done() for future in stage_results._PENDING.values()):
        assert time.monotonic() < deadline; time.sleep(.01)
    # Cached pre-mutation strong verification must not be used with the new stat.
    with pytest.raises(stage_results.EvidencePending):
        stage_results.scheduler_verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)

    # Finish all fixture work before monkeypatch restores globals or temp root vanishes.
    while any(not future.done() for future in stage_results._PENDING.values()):
        assert time.monotonic() < deadline; time.sleep(.01)


def test_startup_recovery_does_not_wait_for_legacy_media_hash(runner, monkeypatch):
    state = runner / 'state'; state.mkdir(); (state / 'password').write_text('fixture-password-' * 2)
    monkeypatch.setattr(web, 'STATE', state)
    monkeypatch.setattr(web, 'load_env_file', lambda *args: None)
    monkeypatch.setattr(web, 'claim_data_root', lambda: None)
    monkeypatch.setattr(web.accounts, 'initialize', Mock())
    monkeypatch.setattr(web, 'ThreadingHTTPServer', Mock())
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
    def recovery(**kwargs):
        assert kwargs == {'enqueue': True, 'scheduler': True}
        raise RuntimeError('fixture stops before threads and HTTP')
    monkeypatch.setattr(web, 'recover_jobs', recovery)
    with pytest.raises(RuntimeError, match='fixture stops'):
        web.main()
