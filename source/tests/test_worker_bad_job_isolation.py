"""坏持久化任务必须逐条隔离；所有数据和执行器均为本地 fixture。"""
import json
import socket
import pytest

from exactcut import diagnostics as diag, stage_results, storage, webserver as web, workflow
from exactcut.util import read_json, write_json_atomic
from test_concurrency import close_logs, queued, runner
from test_prod_monitor import m as monitor


@pytest.fixture
def isolated(runner, monkeypatch):
    monkeypatch.setattr(workflow, 'ROOT', runner)
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(runner / 'source-cache'))
    monkeypatch.setattr(diag, '_root', runner / 'logs')
    monkeypatch.setattr(diag, '_settings', {'max_file_bytes': 1024 * 1024,
                                         'max_total_bytes': 4 * 1024 * 1024,
                                         'retention_days': 14})
    monkeypatch.setattr(diag, '_throttled', {})

    def forbidden(*args, **kwargs):
        pytest.fail('隔离测试禁止外部连接、交付或进程信号')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(web.batch_control.os, 'killpg', forbidden)
    from exactcut import publish
    monkeypatch.setattr(publish, 'publish_to_queue', forbidden)
    monkeypatch.setattr(publish, 'sync_record_once', forbidden)
    monkeypatch.setattr(publish, 'FeishuClient', forbidden)
    monkeypatch.setattr(publish, 'CfAdsClient', forbidden)
    monkeypatch.setattr(publish.OssPublisher, 'connect', forbidden)
    return runner


def install_controlled_signal(monkeypatch, pid, *, exits=True):
    """只替换该用例的信号发送器；禁止任何真实进程信号。"""
    calls = []
    def signal_group(group, sig):
        import signal
        assert group == pid and sig == signal.SIGKILL
        row = web.batch_control.PROCESSES.inspect(pid)
        assert row and row['pgid'] == pid
        calls.append((group, sig))
        if exits: web.batch_control.PROCESSES.rows.pop(pid)
    monkeypatch.setattr(web.batch_control.os, 'killpg', signal_group)
    return calls


def batch_files(batch_id, *, draft=True):
    # 故意不用 folder：非法 ID 的伪造目录正是最小触发条件。
    path = workflow.BATCHES / batch_id
    path.mkdir()
    if draft:
        write_json_atomic(path / 'draft.json', {})
    write_json_atomic(path / 'runtime.json', {'status': 'queued', 'events': []})
    return path


def reserve_bad(job, root):
    assert storage.reserve(job['id'], {str(root): 1}, web.JOBS)


def assert_released(job_id):
    assert job_id not in read_json(web.JOBS.parent / 'reservations.json')['jobs']


def assert_one_alert(root, job_id, batch_id):
    rows = [json.loads(line) for file in (root / 'logs').glob('events-*.jsonl')
            for line in file.read_text().splitlines()]
    alerts = [row for row in rows if row.get('job_id') == job_id
              and row.get('level') in {'warning', 'error'}]
    assert len(alerts) == 1, '每条坏任务只能产生一次结构化告警'
    assert 'batch_id' in alerts[0] and alerts[0]['batch_id'] == batch_id
    assert alerts[0].get('reason') or alerts[0].get('error'), '告警必须包含原因'
    # 只调用线上脚本的离线扫描器；不调用 Monitor 或任何通知发送器。
    config = dict(monitor.DEFAULTS, log_dir=str(root / 'logs'))
    state = {'scan_initialized': True}
    groups = monitor.scan_events(config, state)
    assert sum(group['count'] for group in groups.values()) == 1, '默认线上告警配置必须能消费此事件'
    assert monitor.scan_events(config, state) == {}


def run_round(entrypoint, pending, owned):
    if entrypoint == 'startup':
        web.recover_jobs(enqueue=True)
    web.worker_tick(pending, owned)


@pytest.mark.parametrize('entrypoint', ['tick', 'startup'])
@pytest.mark.parametrize('status', ['queued', 'running'])
def test_invalid_batch_id_is_terminal_without_blocking_healthy_job(isolated, entrypoint, status):
    batch_id = 'invalid-batch'
    batch_files(batch_id)
    bad = queued('a', 'render', batch=batch_id)
    bad.update(status=status)
    if status == 'running':
        bad['action'] = 'prefetch'
    web.save(bad)
    reserve_bad(bad, isolated)
    healthy = queued('b', 'fetch')
    pending, owned = [], {}
    try:
        run_round(entrypoint, pending, owned)
        assert healthy['id'] in owned, '同轮正常任务必须启动'
        assert read_json(web.JOBS / (bad['id'] + '.json'))['status'] in {'failed', 'interrupted'}
        assert_released(bad['id'])
        # 模拟诊断节流失效/重启；仅靠一分钟节流不能防止坏记录永久重报。
        diag._throttled.clear()
        web.recover_jobs(enqueue=True)
        web.worker_tick(pending, owned)
        assert_one_alert(isolated, bad['id'], batch_id)
        assert bad['id'] not in owned
    finally:
        close_logs(owned)


@pytest.mark.parametrize('entrypoint', ['tick', 'startup'])
def test_running_prefetch_with_runtime_but_no_draft_does_not_block_recovery(isolated, entrypoint):
    batch_id = 'd' * 32
    path = batch_files(batch_id, draft=False)
    original = (path / 'runtime.json').read_bytes()
    bad = queued('a', 'fetch', batch=batch_id)
    bad.update(action='prefetch', status='running')
    web.save(bad)
    reserve_bad(bad, isolated)
    healthy = queued('b', 'render')
    pending, owned = [], {}
    try:
        run_round(entrypoint, pending, owned)
        assert healthy['id'] in owned
        assert read_json(web.JOBS / (bad['id'] + '.json'))['status'] in {'failed', 'interrupted'}
        assert_released(bad['id'])
        diag._throttled.clear()
        web.recover_jobs(enqueue=True)
        web.worker_tick(pending, owned)
        assert_one_alert(isolated, bad['id'], batch_id)
        assert (path / 'runtime.json').read_bytes() == original
        assert not (path / 'draft.json').exists()
    finally:
        close_logs(owned)


@pytest.mark.parametrize('entrypoint', ['tick', 'startup'])
@pytest.mark.parametrize('changed', [
    {'batch_id': 7}, {'created': 'yesterday'}, {'not_before': []},
    {'attempt': 'one'}, {'admission': ['invalid']},
    {'batch_id': []}, {'status': []}, {'process_token': []},
], ids=['batch-id-type', 'created-type', 'not-before-type', 'attempt-type', 'admission-type',
       'batch-list', 'status-list', 'token-list'])
def test_wrong_field_type_is_isolated(isolated, entrypoint, changed, monkeypatch):
    bad = queued('a', 'render')
    bad.update(changed)
    if 'admission' in changed:
        bad.update(status='running', pid=123, pgid=123, process_token='c' * 32)
        install_controlled_signal(monkeypatch, 123)
        web.batch_control.PROCESSES.rows[123] = {
            'pgid': 123, 'state': 'S',
            'arguments': ['python', '-X', 'exactcut_process_token=' + 'c' * 32]}
    web.save(bad)
    reserve_bad(bad, isolated)
    healthy = queued('b', 'fetch')
    pending, owned = [], {}
    try:
        run_round(entrypoint, pending, owned)
        assert healthy['id'] in owned
        assert read_json(web.JOBS / (bad['id'] + '.json'))['status'] in {'failed', 'interrupted'}
        assert_released(bad['id'])
        diag._throttled.clear()
        web.recover_jobs(enqueue=True)
        web.worker_tick(pending, owned)
        assert_one_alert(isolated, bad['id'], bad.get('batch_id'))
        assert bad['id'] not in owned
    finally:
        close_logs(owned)


@pytest.mark.parametrize('entrypoint', ['tick', 'startup', 'rebuild'])
def test_bad_json_preserved_and_reservations_rebuilt_for_healthy_job(isolated, entrypoint, caplog):
    bad_id = 'a' * 32
    file = web.JOBS / (bad_id + '.json')
    original = b'{"id": "unfinished",\n  '
    file.write_bytes(original)
    assert storage.reserve(bad_id, {str(isolated): 1}, web.JOBS)
    healthy = queued('b', 'fetch')
    healthy['admission'] = {'paths': {str(isolated): 2}}
    web.save(healthy)
    pending, owned = [], {}
    try:
        if entrypoint == 'rebuild':
            storage.rebuild_reservations(web.JOBS, {healthy['id']}, workflow.BATCHES, workflow.ROOT)
            ledger = read_json(web.JOBS.parent / 'reservations.json')['jobs']
            assert sum(row['bytes'] for row in ledger[healthy['id']].values()) == 2
        else:
            run_round(entrypoint, pending, owned)
            assert healthy['id'] in owned
        assert_released(bad_id)
        assert file.read_bytes() == original
        diag._throttled.clear()
        caplog.clear()
        if entrypoint == 'rebuild':
            storage.rebuild_reservations(web.JOBS, {healthy['id']}, workflow.BATCHES, workflow.ROOT)
        else:
            web.recover_jobs(enqueue=True)
            web.worker_tick(pending, owned)
        assert file.read_bytes() == original
        assert not [record for record in caplog.records if record.levelno >= 30]
        assert_one_alert(isolated, bad_id, None)
    finally:
        close_logs(owned)


@pytest.mark.parametrize('batch_id', ['d' * 32, 'invalid-batch'])
def test_queued_without_draft_already_cleans_up_without_aborting_tick(isolated, batch_id):
    bad = queued('a', 'render', batch=batch_id)
    healthy = queued('b', 'fetch')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert healthy['id'] in owned
        assert read_json(web.JOBS / (bad['id'] + '.json'))['status'] == 'interrupted'
    finally:
        close_logs(owned)


@pytest.mark.parametrize('bad_neighbor', [False, True], ids=['control', 'bad-neighbor'])
def test_bad_job_does_not_block_verified_fetch_successor_in_same_round(isolated, bad_neighbor):
    if bad_neighbor:
        batch_files('invalid-batch')
        bad = queued('a', 'fetch', batch='invalid-batch')
        bad.update(action='prefetch', status='running')
        web.save(bad)
    path = batch_files('d' * 32)
    finished = queued('b', 'fetch', batch=path.name)
    finished.update(action='start', phase='fetch', status='completed', attempt=1)
    stage_results.complete(finished, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    web.save(finished)
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        successors = [job for job in web.stored_jobs() if job.get('parent_id') == finished['id']]
        assert len(successors) == 1 and successors[0]['phase'] == 'render'
        assert successors[0]['id'] in owned, '已验证 fetch 必须在同轮推进并启动 render'
        web.worker_tick(pending, owned)
        assert len([job for job in web.stored_jobs() if job.get('parent_id') == finished['id']]) == 1
    finally:
        close_logs(owned)


@pytest.mark.parametrize('phase', ['upload', 'poll'])
@pytest.mark.parametrize('bad_neighbor', [False, True], ids=['control', 'bad-neighbor'])
def test_bad_neighbor_cannot_replay_delivery_with_pending_intent(isolated, phase, bad_neighbor):
    path = batch_files('d' * 32)
    receipt = {'feishu_record_id': 'fixture-record', 'oss_key': 'fixture-key'}
    write_json_atomic(path / 'publications.json', {'R1': {
        'status': 'uploading', 'publication': receipt,
        'intent': {'status': 'pending', 'phase': phase, 'receipt': receipt}}})
    delivery = queued('b', phase, batch=path.name)
    delivery.update(action='publish', phase=phase, status='running', request_id='R1', attempt=1)
    web.save(delivery)
    # 排序更早的坏任务不能阻塞后面的交付恢复。
    if bad_neighbor:
        bad = queued('a', 'fetch')
        bad['created'] = 'yesterday'
        web.save(bad)
    healthy = queued('c', 'render')
    pending, owned = [], {}
    try:
        web.recover_jobs(enqueue=True)
        web.worker_tick(pending, owned)
        assert healthy['id'] in owned
        assert read_json(web.JOBS / (delivery['id'] + '.json'))['status'] == 'unverified'
        entry = read_json(path / 'publications.json')['R1']
        assert entry['status'] == 'unverified'
        assert entry['publication'] == receipt and entry['intent']['receipt'] == receipt
        diag._throttled.clear()
        web.recover_jobs(enqueue=True)
        web.worker_tick(pending, owned)
        assert delivery['id'] not in owned
        # runner 的 Popen 是唯一执行器替身，任何交付进程启动都视为重放。
        assert set(owned) == {healthy['id']}
        assert web.subprocess.Popen.call_count == 1
        if bad_neighbor:
            assert_one_alert(isolated, bad['id'], None)
    finally:
        close_logs(owned)


@pytest.mark.parametrize('phase', ['upload', 'poll'])
@pytest.mark.parametrize('changed', [{'created': 'yesterday'}, {'admission': []}])
def test_corrupt_publish_job_itself_preserves_intent_and_receipt(isolated, phase, changed):
    path = batch_files('d' * 32)
    receipt = {'feishu_record_id': 'fixture-record', 'oss_key': 'fixture-key'}
    entry = {'status': 'uploading', 'publication': receipt,
             'intent': {'status': 'pending', 'phase': phase, 'receipt': receipt}}
    write_json_atomic(path / 'publications.json', {'R1': entry})
    bad = queued('a', phase, batch=path.name)
    bad.update(action='publish', phase=phase, status='running', request_id='R1', attempt=1, **changed)
    web.save(bad)
    reserve_bad(bad, isolated)
    healthy = queued('b', 'render')
    pending, owned = [], {}
    try:
        run_round('startup', pending, owned)
        assert read_json(web.JOBS / (bad['id'] + '.json'))['status'] == 'unverified'
        actual = read_json(path / 'publications.json')['R1']
        assert actual['status'] == 'unverified'
        assert actual['publication'] == receipt and actual['intent'] == entry['intent']
        assert set(owned) == {healthy['id']}
        diag._throttled.clear()
        run_round('startup', pending, owned)
        assert web.subprocess.Popen.call_count == 1
        assert_one_alert(isolated, bad['id'], path.name)
        assert_released(bad['id'])
    finally:
        close_logs(owned)


@pytest.mark.parametrize('exits', [True, False])
def test_live_corrupt_admission_requires_confirmed_termination(isolated, monkeypatch, exits):
    bad = queued('a', 'render')
    bad.update(status='running', pid=123, pgid=123, process_token='c' * 32, admission=[])
    web.save(bad)
    reserve_bad(bad, isolated)
    web.batch_control.PROCESSES.rows[123] = {
        'pgid': 123, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'c' * 32]}
    calls = install_controlled_signal(monkeypatch, 123, exits=exits)
    if exits:
        web.recover_jobs()
        assert_released(bad['id'])
        assert not web.batch_control.PROCESSES.inspect(123)
    else:
        with pytest.raises(RuntimeError, match='终止尚未确认'):
            web.recover_jobs()
        assert bad['id'] in read_json(web.JOBS.parent / 'reservations.json')['jobs']
        assert read_json(web.JOBS / (bad['id'] + '.json'))['status'] == 'running'
    assert len(calls) == 1


@pytest.mark.parametrize('mismatch', ['token', 'group', 'instance'])
def test_corrupt_record_never_signals_unowned_process(isolated, mismatch):
    bad = queued('a', 'render')
    bad.update(status='running', pid=123, pgid=123, process_token='c' * 32, admission=[])
    if mismatch == 'instance': bad['instance'] = 'foreign-instance'
    web.save(bad)
    web.batch_control.PROCESSES.rows[123] = {
        'pgid': 999 if mismatch == 'group' else 123, 'state': 'S',
        'arguments': ['python', '-X', 'exactcut_process_token=' + ('b' if mismatch == 'token' else 'c') * 32]}
    web.recover_jobs()
    assert web.batch_control.PROCESSES.inspect(123)['state'] == 'S'


def test_owned_exit_with_corrupt_record_completes_queue_accounting(isolated):
    job = queued('a', 'render')
    pending, owned = [], {}
    web.worker_tick(pending, owned)
    process = owned[job['id']][1]
    process.poll = lambda: 1
    actual = read_json(web.JOBS / (job['id'] + '.json'))
    actual['attempt'] = 'broken'
    write_json_atomic(web.JOBS / (job['id'] + '.json'), actual)
    web.worker_tick(pending, owned)
    assert owned == {} and pending == [] and web.WORK.unfinished_tasks == 0
    assert_released(job['id'])
    assert_one_alert(isolated, job['id'], None)


@pytest.mark.parametrize('payload', [b'{"unfinished":', b'[]', b'null', b'\xff'])
def test_quarantine_sidecar_is_stable_and_repaired_file_is_reconsidered(isolated, payload):
    file = web.JOBS / ('a' * 32 + '.json')
    file.write_bytes(payload)
    web.recover_jobs()
    markers = list((web.JOBS.parent / 'job-quarantine').glob('*.json'))
    assert len(markers) == 1
    before = markers[0].stat().st_mtime_ns
    assert file.read_bytes() == payload
    diag._throttled.clear()
    web.recover_jobs()
    assert markers[0].stat().st_mtime_ns == before
    assert_one_alert(isolated, file.stem, None)
    healthy = queued('a', 'fetch')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert healthy['id'] in owned
    finally:
        close_logs(owned)


def test_job_read_permission_failure_is_not_quarantined_as_bad_data(isolated, monkeypatch):
    from pathlib import Path
    job = queued('a', 'fetch')
    original = Path.open
    def denied(path, *args, **kwargs):
        if path == web.JOBS / (job['id'] + '.json'):
            raise PermissionError('synthetic unreadable storage')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(PermissionError, match='synthetic'):
        web.recover_jobs()
    assert not (web.JOBS.parent / 'job-quarantine').exists()


def test_deleted_owned_exit_releases_without_recreating_and_starts_healthy(isolated):
    job = queued('a', 'render')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        _, process, log = owned[job['id']]
        process.poll = lambda: -9
        reserve_bad(job, isolated)
        file = web.JOBS / (job['id'] + '.json')
        file.unlink()  # 正常物理删除已完成，owned 尚待下轮收割。
        healthy = queued('b', 'render')
        web.worker_tick(pending, owned)
        assert set(owned) == {healthy['id']}
        assert log.closed and web.WORK.unfinished_tasks == 1 and not pending
        assert_released(job['id'])
        assert not file.exists()
        web.worker_tick(pending, owned)
        assert not file.exists() and web.WORK.unfinished_tasks == 1
    finally:
        close_logs(owned)


def test_stored_jobs_disappearance_after_glob_is_normal(isolated, monkeypatch):
    from pathlib import Path
    job = queued('a', 'fetch')
    file = web.JOBS / (job['id'] + '.json')
    original = Path.stat
    def vanished(path, *args, **kwargs):
        if path == file:
            file.unlink(missing_ok=True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', vanished)
    assert list(web.stored_jobs()) == []
    assert not file.exists()


@pytest.mark.parametrize('payload', ['structure', 'json'])
def test_stored_jobs_is_read_only_then_scheduler_quarantines(isolated, monkeypatch, payload):
    bad = queued('a', 'render')
    bad.update(status='running', pid=123, pgid=123, process_token='c' * 32, admission=['invalid'])
    file = web.JOBS / (bad['id'] + '.json')
    write_json_atomic(file, bad)
    if payload == 'json': file.write_bytes(b'{broken')
    original = file.read_bytes()
    reserve_bad(bad, isolated)
    web.batch_control.PROCESSES.rows[123] = {
        'pgid': 123, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'c' * 32]}
    before_logs = {p: p.read_bytes() for p in (isolated / 'logs').glob('events-*.jsonl')}
    assert list(web.stored_jobs()) == []
    assert list(web.stored_jobs()) == []
    assert file.read_bytes() == original
    assert not (web.JOBS.parent / 'job-quarantine').exists()
    assert {p: p.read_bytes() for p in (isolated / 'logs').glob('events-*.jsonl')} == before_logs
    assert bad['id'] in read_json(web.JOBS.parent / 'reservations.json')['jobs']
    calls = install_controlled_signal(monkeypatch, 123)
    web.recover_jobs()
    assert len(calls) == (1 if payload == 'structure' else 0)
    if payload == 'structure': assert read_json(file)['quarantine']
    else: assert list((web.JOBS.parent / 'job-quarantine').glob('*.json'))
    assert_one_alert(isolated, bad['id'], None)


@pytest.mark.parametrize('exits', [True, False])
@pytest.mark.parametrize('changed', [{'admission': ['bad']}, {'admission': {'paths': []}}, {'status': []}])
def test_direct_rebuild_invalid_structure_confirms_exit_before_release(isolated, monkeypatch, exits, changed):
    bad = queued('a', 'render')
    bad.update(status='running', pid=123, pgid=123, process_token='c' * 32)
    bad.update(changed)
    file = web.JOBS / (bad['id'] + '.json')
    write_json_atomic(file, bad)
    reserve_bad(bad, isolated)
    healthy = queued('b', 'fetch')
    healthy['admission'] = {'paths': {str(isolated): 2}}
    web.save(healthy)
    web.batch_control.PROCESSES.rows[123] = {
        'pgid': 123, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'c' * 32]}
    calls = install_controlled_signal(monkeypatch, 123, exits=exits)
    def rebuild():
        storage.rebuild_reservations(web.JOBS, {bad['id'], healthy['id']}, workflow.BATCHES, workflow.ROOT)
    if exits:
        rebuild()
        assert read_json(file)['quarantine']
        assert_released(bad['id'])
        ledger = read_json(web.JOBS.parent / 'reservations.json')['jobs']
        assert sum(row['bytes'] for row in ledger[healthy['id']].values()) == 2
    else:
        with pytest.raises(RuntimeError, match='终止尚未确认'): rebuild()
        assert read_json(file) == bad
        assert bad['id'] in read_json(web.JOBS.parent / 'reservations.json')['jobs']
    assert len(calls) == 1


def test_direct_rebuild_default_paths_quarantines_bad_admission(isolated):
    bad = queued('a', 'render', batch='d' * 32)
    file = web.JOBS / (bad['id'] + '.json')
    write_json_atomic(file, {**bad, 'admission': ['bad']})
    reserve_bad(bad, isolated)
    storage.rebuild_reservations(web.JOBS, {bad['id']})
    assert read_json(file)['quarantine']
    assert_released(bad['id'])


def test_direct_rebuild_unreadable_live_identity_keeps_existing_reservation(isolated):
    job = queued('a', 'render')
    file = web.JOBS / (job['id'] + '.json')
    file.write_bytes(b'{truncated')
    reserve_bad(job, isolated)
    before = read_json(web.JOBS.parent / 'reservations.json')['jobs'][job['id']]
    storage.rebuild_reservations(web.JOBS, {job['id']})
    assert read_json(web.JOBS.parent / 'reservations.json')['jobs'][job['id']] == before
    assert file.read_bytes() == b'{truncated'


def test_stored_job_disappears_between_stat_and_open(isolated, monkeypatch):
    from pathlib import Path
    job = queued('a', 'fetch')
    file = web.JOBS / (job['id'] + '.json')
    original = Path.open
    def vanished(path, *args, **kwargs):
        if path == file: file.unlink(missing_ok=True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', vanished)
    assert list(web.stored_jobs()) == []
    assert not (web.JOBS.parent / 'job-quarantine').exists()
