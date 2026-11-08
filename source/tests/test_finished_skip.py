"""输入清单审计、跳过门槛及与全量扫描的逐轮业务等价性。"""
import builtins
import io
from pathlib import Path
import threading

import pytest

from exactcut import finished_skip as fs, lock_timing, material_workers as mw
from exactcut import material_versions as m, webserver as web, workflow as w
from exactcut.util import read_json, write_json_atomic
from skip_equivalence_helpers import state_digest
from test_skip_finished_equivalence import finished
from test_workflow import workspace, imported
from test_material_versions import token


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    monkeypatch.setenv('EXACTCUT_SKIP_FINISHED', '1')
    memo = fs.Memory()
    previous = getattr(fs._local, 'memory', None)
    fs._local.memory = memo
    yield memo
    fs._local.memory = previous


@pytest.fixture
def counts(monkeypatch):
    values = []
    original = lock_timing.Phases.finish

    def finish(self):
        values.append(dict(self.counts))
        return original(self)

    monkeypatch.setattr(lock_timing.Phases, 'finish', finish)
    return values


def snapshot():
    return [(p, read_json(p)) for p in web.JOBS.glob('*.json')]


def edit(path, **fields):
    value = read_json(path)
    value.update(fields)
    write_json_atomic(path, value)


def verification(f):
    parent = f.parents[0]
    child, delivery = f.deliveries[0]
    sha = delivery['sha256']
    write_json_atomic(child / 'publications.json', {delivery['asset_id']: {
        'status': 'unverified', 'approved_sha256': sha,
        'publication': {'sha256': sha, 'feishu_record_id': 'record-fixture',
                        'oss_key': 'fixture/video.mp4', 'oss_url': 'https://fixture.invalid/video'},
        'intent': {'status': 'pending', 'receipt': {'material_id': 'fixture'}}}})
    mw.recover_outbox()
    state = m.queue_verification(parent, dict(revision=m.read(parent)['revision'],
                                mutation_id=token(), delivery_id=delivery['delivery_id']))
    return next(e for e in state['outbox'].values() if e['kind'] == 'verification')


def record_reads(monkeypatch, operation):
    reads = set()

    def wrap(original):
        def opened(file, mode='r', *args, **kwargs):
            if isinstance(file, (str, bytes, Path)) and 'r' in mode:
                reads.add(Path(file).absolute())
            return original(file, mode, *args, **kwargs)
        return opened

    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', wrap(builtins.open))
        patch.setattr(io, 'open', wrap(io.open))
        operation()
    return {p for p in reads if not fs.audit_exempt(p)}


@pytest.mark.parametrize('case', ['delivery', 'candidate', 'verification', 'invalid', 'legacy'])
def test_opened_files_are_declared(finished, monkeypatch, case):
    parent = finished.parents[0]
    child, delivery = finished.deliveries[0]
    candidate, event, _ = finished.candidates[0]
    if case == 'delivery':
        # 强制经过 materialize（已存在的确认快照、成片 hash、access 和 _job）。
        write_json_atomic(child / 'publications.json', {delivery['asset_id']: {'status': 'upload_queued'}})
    elif case == 'candidate':
        with m.locked(parent):
            state = m._read(parent)
            state['versions'][event['version_id']]['status'] = 'processing'
            m._write(parent, state)
    elif case == 'verification':
        verification(finished)  # 未落 job 的 pending 核实，覆盖 evidence/runtime/确认快照。
    elif case == 'invalid':
        with m.locked(parent):
            state = m._read(parent)
            state['versions'][event['version_id']]['status'] = 'superseded'
            m._write(parent, state)
        # 无 token 的 queued job 可安全取消，覆盖 reservations 和 end_execution。
        finished.save_job(candidate, 'render', 'queued', request_id=event['asset_id'])
    snap = snapshot()
    if case == 'candidate':
        snap = [(p, j) for p, j in snap if j.get('batch_id') != candidate.name]
    if case == 'legacy':
        path = finished.legacy[0]
        write_json_atomic(path / 'confirmed.json', {'requests': [{'request_id': 'fixture'}]})
        write_json_atomic(path / 'production/results.json', {'requests': {'fixture': {'status': 'qc_passed'}}})
        write_json_atomic(path / 'production/results/fixture.json', {'status': 'qc_passed'})
        write_json_atomic(path / 'publications.json', {})
        edit(path / 'runtime.json', request_parallel=True)
        declared = fs.crash_inputs(path, web.JOBS, snap)
        reads = record_reads(monkeypatch, lambda: w.runtime(path, jobs=[j for _, j in snap
                                                                      if j.get('batch_id') == path.name]))
    else:
        declared = fs.outbox_inputs(parent, web.JOBS, snap)
        reads = record_reads(monkeypatch, lambda: mw._recover_parent(parent, web.JOBS, snap))
        # 新创建文件也必须由集中清单发现，不豁免任何业务读取。
        declared |= fs.outbox_inputs(parent, web.JOBS, snapshot())
    assert reads
    declared = {p.absolute() for p in declared}
    # 目录自身参与指纹时，直属成员新增/删除/replace 由目录 stat 覆盖。
    # 内容依赖另用下面的具体文件断言约束，不能仅靠目录替代。
    missing = {p for p in reads if p not in declared
               and not (p.parent in declared and p.parent.is_dir())}
    assert not missing, sorted(missing)
    if case == 'verification':
        assert (child / 'production/videos' / delivery['manifest']['requests'][0]['output_filename']) in reads
    if case in {'delivery', 'verification'}:
        assert child / 'production/videos' / delivery['manifest']['requests'][0]['output_filename'] in declared
    if case == 'delivery':
        assert child / 'confirmed.json' in reads
        assert parent / 'access.json' in reads


def test_first_second_disabled_and_full_sweep(finished, counts, memory, monkeypatch):
    now = [0]
    memory.clock = lambda: now[0]
    memory.last_full = 0
    finished.tick()
    first = counts[-1]
    assert first['outbox_processed'] == len(finished.parents)
    assert first['crash_computed'] > 0
    assert first['outbox_skipped'] == first['crash_skipped'] == 0
    finished.tick()
    assert counts[-1]['outbox_skipped'] > 0
    assert counts[-1]['crash_skipped'] > 0
    now[0] = 300
    finished.tick()
    assert counts[-1] == first
    monkeypatch.setenv('EXACTCUT_SKIP_FINISHED', '0')
    finished.tick(2)
    assert counts[-1] == counts[-2] == first
    assert not hasattr(fs._local, 'memory')


@pytest.mark.parametrize('kind', ['publications', 'runtime', 'media-state', 'job', 'job_add', 'job_delete',
                                  'result', 'result_add', 'registry', 'confirmed'])
def test_inputs_reopen_corresponding_check(finished, memory, kind):
    finished.tick(2)
    parent = finished.parents[0]
    child, _ = finished.deliveries[0]
    path = finished.legacy[0]
    snap = snapshot()
    if kind in {'runtime', 'result', 'result_add', 'confirmed', 'registry'}:
        check_kind, target = 'crash', path
        file = {'runtime': path / 'runtime.json', 'result': path / 'production/results.json',
                'result_add': path / 'production/results/new.json', 'confirmed': path / 'confirmed.json',
                'registry': m.registry(path)}[kind]
    else:
        check_kind, target = 'outbox', parent
        file = {'publications': child / 'publications.json', 'media-state': parent / 'media-state.json',
                'job': web.JOBS / (finished.candidates[0][2]['id'] + '.json'),
                'job_add': web.JOBS / (token() + '.json'),
                'job_delete': web.JOBS / (finished.candidates[0][2]['id'] + '.json')}[kind]
    assert fs.fingerprint(check_kind, target, web.JOBS, snap) == memory.entries[check_kind][target]
    if kind == 'job_delete':
        file.unlink()
    elif kind == 'job_add':
        write_json_atomic(file, {'id': file.stem, 'batch_id': child.name, 'status': 'failed'})
    else:
        value = read_json(file) if file.exists() else {}
        value['fixture_changed'] = True
        write_json_atomic(file, value)
    with fs.worker_scope():
        assert not fs.Check(check_kind, target, web.JOBS, snapshot()).skip


@pytest.mark.parametrize('case', ['verification', 'media-upload', 'not_before', 'children', 'job_token'])
def test_volatile_conditions_never_skip(finished, memory, case):
    parent = finished.parents[0]
    child, event, job = finished.candidates[0]
    if case == 'verification':
        verification(finished)
    elif case == 'not_before':
        edit(web.JOBS / (job['id'] + '.json'), not_before=web.time.time() + 100000)
    else:
        with m.locked(parent):
            state = m._read(parent)
            if case == 'media-upload':
                state['outbox']['fixture-upload'] = {**event, 'kind': 'media-upload', 'status': 'pending'}
            else:
                state['versions'][event['version_id']]['status'] = 'superseded'
            m._write(parent, state)
        if case == 'children':
            write_json_atomic(child / 'control.json', {'children': [{'pid': 999, 'process_token': token()}]})
        elif case == 'job_token':
            edit(web.JOBS / (job['id'] + '.json'), status='queued', phase='render', process_token=token(),
                 instance=web.instance_marker())
    for _ in range(2):
        with fs.worker_scope():
            check = fs.Check('outbox', parent, web.JOBS, snapshot())
            assert check.before is None and not check.skip
            check.finish()
        assert parent not in memory.entries['outbox']


def test_no_action_only_and_stale_snapshot(finished, memory):
    parent = finished.parents[0]
    with fs.worker_scope():
        check = fs.Check('outbox', parent, web.JOBS, snapshot())
        edit(parent / 'media-state.json', fixture_changed=True)
        check.finish()
    assert parent not in memory.entries['outbox']
    # 模拟 snapshot 已读，但 job 在 Check 开始前被替换的竞态。
    stale = snapshot()
    job = finished.candidates[0][2]
    edit(web.JOBS / (job['id'] + '.json'), fixture_changed=True)
    with fs.worker_scope():
        check = fs.Check('outbox', parent, web.JOBS, stale)
        check.finish()
        assert not fs.Check('outbox', parent, web.JOBS, snapshot()).skip


def test_direct_and_other_thread_calls_are_full(finished, memory, monkeypatch):
    finished.tick(2)
    calls = []
    original = mw._recover_parent
    monkeypatch.setattr(mw, '_recover_parent', lambda parent, *a: (calls.append(parent), original(parent, *a)))
    mw.recover_outbox()
    assert set(calls) == set(finished.parents)
    calls.clear()
    with fs.worker_scope():
        thread = threading.Thread(target=mw.recover_outbox)
        thread.start()
        thread.join()
    assert set(calls) == set(finished.parents)
    assert not fs.Check('crash', finished.legacy[0], web.JOBS, snapshot()).skip


def test_skip_and_full_have_identical_multitick_digests(finished, monkeypatch):
    baseline = {p: p.read_bytes() for p in (finished.root / '.web-state').rglob('*.json')}

    def run(enabled):
        monkeypatch.setenv('EXACTCUT_SKIP_FINISHED', enabled)
        monkeypatch.setattr(fs._local, 'memory', fs.Memory())
        digests = []
        for tick in range(10):
            if tick == 2:
                child, delivery = finished.deliveries[0]
                pubs = read_json(child / 'publications.json')
                pubs[delivery['asset_id']]['status'] = 'fbbm_ready'
                write_json_atomic(child / 'publications.json', pubs)
            if tick == 4:
                edit(finished.legacy[0] / 'runtime.json', status='producing')
            if tick == 6:
                parent = finished.parents[0]
                with m.locked(parent):
                    state = m._read(parent)
                    state['versions'][finished.candidates[0][1]['version_id']]['status'] = 'processing'
                    m._write(parent, state)
            if tick == 8:
                job = finished.candidates[0][2]
                edit(web.JOBS / (job['id'] + '.json'), status='queued', not_before=web.time.time() + 100000)
            finished.tick()
            digests.append(state_digest(finished.root))
        return digests

    skipped = run('1')
    for p in (finished.root / '.web-state').rglob('*.json'):
        if p not in baseline:
            p.unlink()
    for p, data in baseline.items():
        p.write_bytes(data)
    finished.pending.clear()
    full = run('0')
    for a, b in zip(skipped, full):
        assert a.digest == b.digest, '\n'.join(a.diff(b))


def test_runtime_has_no_clock_or_process_dependency(finished, monkeypatch):
    expected = w.runtime(finished.legacy[0], jobs=[])

    def forbidden(*args, **kwargs):
        pytest.fail('runtime 不应读取当前时间或查询进程表')

    monkeypatch.setattr(w.time, 'time', forbidden)
    monkeypatch.setattr(web.batch_control.PROCESSES, 'inspect', forbidden)
    monkeypatch.setattr(web.batch_control.PROCESSES, 'find', forbidden)
    assert w.runtime(finished.legacy[0], jobs=[]) == expected


def test_skip_counts_reach_existing_diagnostics(monkeypatch):
    events = {}
    monkeypatch.setattr(lock_timing, 'SLOW_MS', -1)
    monkeypatch.setattr(lock_timing, 'SUMMARY_SECONDS', -1)
    monkeypatch.setattr(lock_timing, '_tick_counters', {})
    monkeypatch.setattr(lock_timing.diag, 'event', lambda name, **fields: events.update({name: fields}))
    monkeypatch.setattr(lock_timing.diag, 'throttled', lambda name, key, **fields: events.update({name: fields}))
    expected = dict(zip(fs.COUNTERS, (2, 3, 4, 5)))
    with lock_timing.phases() as timer:
        timer.counts = expected
        timer.phase('recover_outbox')
    for name in ('scheduler.tick_slow', 'scheduler.tick_summary'):
        assert {key: events[name][key] for key in fs.COUNTERS} == expected
        assert 'phases_ms' in events[name]


def test_materialize_missing_child_access_invalidates_noop(finished):
    parent = finished.parents[0]
    child, delivery = finished.deliveries[0]
    write_json_atomic(child / 'publications.json', {delivery['asset_id']: {'status': 'upload_queued'}})
    for _ in range(2):
        with fs.worker_scope():
            mw.recover_outbox()
    with fs.worker_scope():
        assert fs.Check('outbox', parent, web.JOBS, snapshot()).skip
    (child / 'access.json').unlink()
    with fs.worker_scope():
        assert not fs.Check('outbox', parent, web.JOBS, snapshot()).skip
        mw.recover_outbox()
    assert read_json(child / 'access.json') == read_json(parent / 'access.json')


@pytest.mark.parametrize('status,error,active,executor,phase,owned,expected', [
    ('completed', '', True, False, 'render', True, False),
    ('failed', '', True, False, 'render', True, False),
    ('interrupted', '历史错误', True, False, 'render', True, False),
    ('interrupted', '候选已取消或被替代；原成片已保留', False, False, 'render', True, False),
    ('interrupted', '候选已取消或被替代；原成片已保留', True, False, 'render', True, True),
    ('interrupted', '候选已取消或被替代；原成片已保留', False, True, 'render', True, True),
    ('queued', '', False, False, 'fetch', True, True),
    ('waiting_resource', '', False, False, 'media-upload', True, True),
    ('queued', '', True, False, 'poll', True, False),
    ('queued', '', True, False, 'render', False, False),
])
def test_cancel_process_gate_matches_real_query(finished, monkeypatch, status, error, active,
                                               executor, phase, owned, expected):
    parent = finished.parents[0]
    child, event, job = finished.candidates[0]
    with m.locked(parent):
        state = m._read(parent)
        state['versions'][event['version_id']]['status'] = 'superseded'
        m._write(parent, state)
    edit(child / 'runtime.json', status='producing' if active else 'interrupted', executor_running=executor)
    edit(web.JOBS / (job['id'] + '.json'), status=status, error=error, phase=phase,
         process_token=token(), pid=123)
    monkeypatch.setattr(web.batch_control, 'belongs', lambda job: owned)
    queries = []
    monkeypatch.setattr(web.batch_control.PROCESSES, 'find', lambda token: queries.append(token) or True)
    assert fs.outbox_volatile(parent, m.read(parent), snapshot()) == expected
    mw.cancel_queued_candidate(parent, event, child,
                              job_files=[web.JOBS / (job['id'] + '.json')])
    assert bool(queries) == expected


@pytest.mark.parametrize('operation', ['overwrite', 'replace', 'add', 'delete'])
def test_delivery_media_content_and_membership_invalidate(finished, operation):
    finished.tick(2)
    parent = finished.parents[0]
    child, delivery = finished.deliveries[0]
    video = child / 'production/videos' / delivery['manifest']['requests'][0]['output_filename']
    if operation == 'overwrite':
        video.write_bytes(b'changed content')
    elif operation == 'replace':
        replacement = video.with_suffix('.tmp')
        replacement.write_bytes(video.read_bytes())
        replacement.replace(video)
    elif operation == 'delete':
        video.unlink()
    else:
        (video.parent / 'new.mp4').write_bytes(b'new')
    with fs.worker_scope():
        assert not fs.Check('outbox', parent, web.JOBS, snapshot()).skip


def test_skipped_check_fingerprints_once(finished, monkeypatch):
    finished.tick(2)
    calls = []
    original = fs.fingerprint
    def fingerprint(*args):
        calls.append(args[0])
        return original(*args)
    monkeypatch.setattr(fs, 'fingerprint', fingerprint)
    with fs.worker_scope():
        check = fs.Check('outbox', finished.parents[0], web.JOBS, snapshot())
        assert check.skip
    assert calls == ['outbox']


def test_history_does_not_depend_on_reservation_rebuild(finished):
    parent = finished.parents[0]
    child, event, job = finished.candidates[0]
    with m.locked(parent):
        state = m._read(parent)
        state['versions'][event['version_id']]['status'] = 'superseded'
        m._write(parent, state)
    finished.tick(2)
    write_json_atomic(web.JOBS.parent / 'reservations.json', {'jobs': {}})
    with fs.worker_scope():
        check = fs.Check('outbox', parent, web.JOBS, snapshot())
        assert check.skip
        check.finish()
    finished.save_job(child, 'render', 'queued')
    files = fs.outbox_inputs(parent, web.JOBS, snapshot())
    assert web.JOBS.parent / 'reservations.json' in files
    with fs.worker_scope():
        assert not fs.Check('outbox', parent, web.JOBS, snapshot()).skip


def test_cached_input_inventory_rebuilds_on_membership_change(finished, monkeypatch):
    finished.tick(2)
    parent = finished.parents[0]
    child, _ = finished.deliveries[0]
    original = fs.outbox_inputs
    calls = []
    def inputs(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(fs, 'outbox_inputs', inputs)
    with fs.worker_scope():
        assert fs.Check('outbox', parent, web.JOBS, snapshot()).skip
    assert not calls
    write_json_atomic(child / 'production/results/new.json', {'status': 'failed'})
    with fs.worker_scope():
        assert not fs.Check('outbox', parent, web.JOBS, snapshot()).skip
    assert calls == [parent]


@pytest.mark.parametrize('kind', ['outbox', 'crash'])
@pytest.mark.parametrize('operation', ['replace', 'add', 'delete'])
def test_unrelated_job_changes_keep_other_projects_skipped(finished, kind, operation):
    finished.tick(2)
    target = finished.parents[0] if kind == 'outbox' else finished.legacy[0]
    other = finished.legacy[-1]
    file = web.JOBS / (token() + '.json')
    write_json_atomic(file, {'id': file.stem, 'batch_id': other.name, 'status': 'failed'})
    if operation == 'replace':
        edit(file, fixture_changed=True)
    elif operation == 'delete':
        file.unlink()
    with fs.worker_scope():
        assert fs.Check(kind, target, web.JOBS, fs.JobSnapshot(snapshot())).skip


@pytest.mark.parametrize('kind', ['outbox', 'crash'])
def test_new_job_reopens_previously_skipped_project(finished, kind, monkeypatch):
    finished.tick(2)
    target = finished.parents[0] if kind == 'outbox' else finished.legacy[0]
    batch = finished.deliveries[0][0] if kind == 'outbox' else target
    with fs.worker_scope():
        assert fs.Check(kind, target, web.JOBS, fs.JobSnapshot(snapshot())).skip
    finished.save_job(batch, 'render', 'failed')
    calls = []
    if kind == 'outbox':
        original = mw._recover_parent
        def recover(parent, *args):
            calls.append(parent)
            return original(parent, *args)
        monkeypatch.setattr(mw, '_recover_parent', recover)
    else:
        original = w.runtime
        def runtime(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(w, 'runtime', runtime)
    finished.tick()
    assert target in calls  # 使用实际 snapshot/latest_jobs 接入，不只是直接构造 Check。


@pytest.mark.parametrize('kind', ['outbox', 'crash'])
@pytest.mark.parametrize('related', [True, False])
def test_job_created_during_processing_is_not_memorized(finished, memory, kind, related):
    target = finished.parents[0] if kind == 'outbox' else finished.legacy[0]
    batch = finished.deliveries[0][0] if kind == 'outbox' else target
    with fs.worker_scope():
        check = fs.Check(kind, target, web.JOBS, fs.JobSnapshot(snapshot()))
        assert check.before is not None and not check.skip
        finished.save_job(batch if related else finished.legacy[-1], 'render', 'failed')
        fs.finish_checks([check])
    assert (target in memory.entries[kind]) is (not related)


def test_global_job_scan_tracks_unrelated_membership_and_values(finished, memory):
    parent = finished.parents[0]
    child, event, _ = finished.candidates[0]
    with m.locked(parent):
        state = m._read(parent)
        state['versions'][event['version_id']]['status'] = 'superseded'
        m._write(parent, state)
    finished.save_job(child, 'render', 'queued')
    with fs.worker_scope():
        check = fs.Check('outbox', parent, web.JOBS, fs.JobSnapshot(snapshot()))
        assert check.before[2] is None
        check.finish()
        assert fs.Check('outbox', parent, web.JOBS, fs.JobSnapshot(snapshot())).skip
        job = finished.save_job(finished.legacy[-1], 'render', 'failed')
        assert not fs.Check('outbox', parent, web.JOBS, fs.JobSnapshot(snapshot())).skip
        memory.entries['outbox'].clear()
        check = fs.Check('outbox', parent, web.JOBS, fs.JobSnapshot(snapshot()))
        check.finish()
        edit(web.JOBS / (job['id'] + '.json'), fixture_changed=True)
        assert not fs.Check('outbox', parent, web.JOBS, fs.JobSnapshot(snapshot())).skip
