"""Request-scoped scheduling and durable fanout acceptance cases."""
import copy
import signal
import uuid
from unittest.mock import Mock

import pytest
from exactcut import batch_control as control, concurrency, stage_results, workflow as w, webserver as web
from exactcut.runtime import instance_marker
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs
from test_round1_integration import chain
from test_workflow import workspace
from test_round1_delivery import api


def batch_path(key='d'):
    path = w.BATCHES / (key * 32)
    path.mkdir(exist_ok=True)
    write_json_atomic(path / 'draft.json', {})
    write_json_atomic(path / 'runtime.json', {'status': 'review'})
    return path


def request_job(key, lane, path, rid):
    job = queued(key, lane, batch=path.name)
    job['request_id'] = rid
    web.save(job)
    return job


def test_per_project_defaults_to_one(monkeypatch):
    for config in ({}, {'concurrency': {'render': 4}}):
        monkeypatch.setattr(concurrency, 'load_config', lambda: config)
        assert concurrency.settings()['per_project'] == 1


@pytest.mark.parametrize('value', [0, -1, True, False, 1.5, '2', None])
def test_per_project_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'per_project': value}})
    with pytest.raises(ValueError, match='per_project'):
        concurrency.settings()


def test_same_batch_two_renders_and_other_lanes_admit(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 4, 'per_project': 2}})
    monkeypatch.setattr(web.storage, 'scheduler_job_budget', lambda *args: {})
    path = batch_path()
    for key, lane, rid in [('a', 'render', 'R1'), ('b', 'render', 'R2'), ('c', 'render', 'R3'),
                            ('e', 'upload', 'R4'), ('f', 'poll', 'R5'), ('g', 'poll', 'R1')]:
        request_job(key, lane, path, rid)
    owned = {}
    try:
        web.worker_tick([], owned)
        assert set(owned) == {key * 32 for key in 'abef'}
    finally:
        close_logs(owned)


@pytest.mark.parametrize('legacy_first', [True, False])
def test_legacy_batch_fence_blocks_every_request(runner, monkeypatch, legacy_first):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 4, 'per_project': 2}})
    path = batch_path()
    if legacy_first:
        queued('a', 'fetch', batch=path.name); request_job('b', 'render', path, 'R1')
    else:
        request_job('a', 'render', path, 'R1'); queued('b', 'fetch', batch=path.name)
    owned = {}
    try:
        web.worker_tick([], owned)
        assert set(owned) == {'a' * 32}
    finally:
        close_logs(owned)


def test_disabled_false_keeps_request_jobs_serial(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'enabled': False, 'render': 4, 'per_project': 2}})
    path = batch_path()
    request_job('a', 'render', path, 'R1'); request_job('b', 'render', path, 'R2')
    owned = {}
    try:
        web.worker_tick([], owned)
        assert set(owned) == {'a' * 32}
    finally:
        close_logs(owned)


def test_fetch_fanout_recovers_partial_persistence_idempotently(runner):
    path = batch_path()
    write_json_atomic(path / 'confirmed.json', {'requests': [{'request_id': 'R1'}, {'request_id': 'R2'}, {'request_id': 'R3'}]})
    job = dict(id='a' * 32, batch_id=path.name, action='start', phase='fetch', status='completed')
    web.advance_stage(job)
    jobs = [j for j in web.stored_jobs() if j.get('phase') == 'render']
    assert {j.get('request_id') for j in jobs} == {'R1', 'R2', 'R3'}
    victim = next(j for j in jobs if j['request_id'] == 'R2')
    (web.JOBS / (victim['id'] + '.json')).unlink()
    web.save(job); web.recover_jobs()
    recovered = [j for j in web.stored_jobs() if j.get('phase') == 'render']
    assert {j['id'] for j in recovered} == {j['id'] for j in jobs}
    web.advance_stage(job)
    assert len([j for j in web.stored_jobs() if j.get('phase') == 'render']) == 3
    assert all(j['command'][-2] == j['request_id'] for j in recovered)


def test_reconcile_request_resumes_share_batch_cap_only_for_render(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 4, 'per_project': 2, 'upload': 2}})
    path = batch_path()
    children = []
    for index, (rid, lane) in enumerate([('R1', 'render'), ('R2', 'render'), ('R3', 'upload')], 100):
        token = uuid.uuid4().hex
        child = dict(pid=index, pgid=index, process_token=token, request_id=rid, lane=lane, instance=instance_marker())
        children.append(child)
        control.PROCESSES.rows[index] = dict(pgid=index, state='T', arguments=['python', '-X', 'exactcut_process_token=' + token])
    write_json_atomic(path / 'control.json', {'paused': False, 'children': children, 'suspended': children})
    def signal_group(pid, sig):
        assert sig == signal.SIGCONT
        control.PROCESSES.rows[pid]['state'] = 'S'
    kill = Mock(side_effect=signal_group); monkeypatch.setattr(control.os, 'killpg', kill)
    control.reconcile(w.BATCHES)
    assert {call.args[0] for call in kill.call_args_list} == {100, 101, 102}


def multiple_batch(chain, count, drama='101460', owner='alice'):
    batch = chain.batch(drama, owner)
    path = w.folder(batch['id'])
    stored = read_json(path / 'draft.json')
    template = stored['draft']['rows'][0]
    stored['draft']['rows'] = [dict(copy.deepcopy(template), request_id='R' + str(i), seq=i) for i in range(1, count + 1)]
    write_json_atomic(path / 'draft.json', stored)
    return w.detail(batch['id'])


def test_six_requests_keep_exactly_two_render_residents(chain):
    chain.limits.update(render=4, per_project=2)
    batch = multiple_batch(chain, 6)
    chain.enqueue(batch); chain.tick(); chain.finish('fetch', batch)
    for remaining in range(6, 0, -1):
        residents = [j for j, _, _ in chain.owned.values() if j['phase'] == 'render']
        assert len(residents) == min(2, remaining)
        chain.finish('render', batch, residents[0]['request_id'])
    assert len(chain.render_calls) == 6
    assert w.runtime(w.folder(batch['id']))['status'] == 'review'
    assert len(w.result_records(w.folder(batch['id']))) == 6


def test_two_projects_fill_four_render_slots(chain):
    chain.limits.update(render=4, per_project=2, max_active_children=8)
    a = multiple_batch(chain, 2)
    b = multiple_batch(chain, 2, '101461', 'bob')
    chain.enqueue(a); chain.enqueue(b); chain.tick()
    chain.finish('fetch', a); chain.finish('fetch', b)
    assert len(chain.owned) == 4
    assert {batch['id']: sum(j['batch_id'] == batch['id'] for j, _, _ in chain.owned.values()) for batch in (a, b)} == {a['id']: 2, b['id']: 2}


def finish_specific(chain, job, code):
    _, process, _ = chain.owned[job['id']]
    stage_results.complete(job, web.JOBS, w.BATCHES, w.ROOT, code)
    process.code = code
    chain.table.rows.pop(process.pid)
    chain.tick()


def test_one_failed_render_keeps_siblings_running_and_finishes_remaining(chain, monkeypatch):
    from exactcut import result_store
    chain.limits.update(render=4, per_project=2)
    batch = multiple_batch(chain, 3)
    path = w.folder(batch['id'])
    chain.enqueue(batch); chain.tick(); chain.finish('fetch', batch)
    jobs = [j for j, _, _ in chain.owned.values()]
    failed, sibling = jobs
    result_store.patch(path / 'production', failed['request_id'], status='failed', error='fixture render failure')
    result_store.patch(path / 'production', sibling['request_id'], executor_running=True)
    finish_specific(chain, failed, 2)
    assert w.runtime(path)['status'] == 'producing'
    assert not w.runtime(path)['production_complete']
    assert w.result_records(path)[sibling['request_id']]['executor_running'] is True
    assert len(chain.owned) == 2 and sibling['id'] in chain.owned
    for job, _, _ in list(chain.owned.values()): chain.finish('render', batch, job['request_id'])
    records = w.result_records(path)
    assert records[failed['request_id']]['status'] == 'failed'
    assert records[failed['request_id']]['error'] == 'fixture render failure'
    assert sum(record['status'] == 'qc_passed' for record in records.values()) == 2
    assert w.runtime(path)['status'] == 'partial'


def test_crash_one_request_requeues_it_without_clearing_sibling(chain):
    from exactcut import result_store
    chain.limits.update(render=4, per_project=2)
    batch = multiple_batch(chain, 3)
    path = w.folder(batch['id'])
    chain.enqueue(batch); chain.tick(); chain.finish('fetch', batch)
    jobs = [j for j, _, _ in chain.owned.values()]
    crashed, sibling = jobs
    result_store.patch(path / 'production', sibling['request_id'], executor_running=True)
    _, process, log = chain.owned.pop(crashed['id'])
    log.close(); chain.table.rows.pop(process.pid)
    web.recover_jobs(enqueue=True, owned=chain.owned)
    saved = read_json(web.JOBS / (crashed['id'] + '.json'))
    assert saved['status'] == 'queued' and saved['request_id'] == crashed['request_id']
    assert w.result_records(path)[sibling['request_id']]['executor_running'] is True
    chain.tick()
    assert len(chain.owned) == 2
    assert len([j for j in web.stored_jobs() if j.get('phase') == 'render']) == 3
    assert all(j.get('phase') == 'render' for j, _, _ in chain.owned.values())


def test_same_batch_render_upload_poll_execute_independently(chain):
    chain.limits.update(render=4, per_project=2, max_active_children=8)
    batch = multiple_batch(chain, 4)
    chain.enqueue(batch); chain.tick(); chain.finish('fetch', batch)
    initial = [j for j, _, _ in chain.owned.values()]
    rid1, rid2 = [j['request_id'] for j in initial]
    chain.finish('render', batch, rid1)
    chain.enqueue(batch, 'publish', rid1); chain.tick()
    assert len(chain.owned) == 3
    chain.finish('upload', batch, rid1)
    chain.due(batch)
    assert {j['phase'] for j, _, _ in chain.owned.values()} == {'render', 'poll'}
    chain.finish('render', batch, rid2)
    chain.enqueue(batch, 'publish', rid2); chain.tick()
    assert {j['phase'] for j, _, _ in chain.owned.values()} == {'render', 'upload', 'poll'}
    assert len(chain.owned) == 4


def test_evidence_pending_fences_only_own_request(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 4, 'per_project': 2}})
    path = batch_path()
    blocked = request_job('a', 'render', path, 'R1')
    blocked.update(status='running', phase='render', attempt=1)
    web.save(blocked)
    request_job('b', 'render', path, 'R1'); request_job('c', 'render', path, 'R2')
    verify = stage_results.scheduler_verified
    def pending(job, *args):
        if job['id'] == blocked['id']: raise stage_results.EvidencePending('fixture slow evidence')
        return verify(job, *args)
    monkeypatch.setattr(stage_results, 'scheduler_verified', pending)
    owned = {}
    try:
        web.worker_tick([], owned)
        assert set(owned) == {'c' * 32}
    finally:
        close_logs(owned)


@pytest.mark.parametrize('mutation', ['heartbeat', 'result', 'legacy_result', 'publication', 'children', 'target'])
def test_approval_snapshot_tracks_target_and_allows_sibling_progress(api, workspace, monkeypatch, mutation):
    from exactcut import result_store
    from test_round1_delivery import post
    from test_workflow import imported, complete_asset
    batch = imported(); path, request, video, record = complete_asset(batch)
    original = w.approval_evidence
    def progress(*args):
        evidence = original(*args)
        if mutation == 'heartbeat': w.update(path, heartbeat_at='fixture sibling heartbeat')
        elif mutation == 'result': result_store.patch(path / 'production', 'R-sibling', status='qc_passed', executor_running=True)
        elif mutation == 'legacy_result':
            results = read_json(path / 'production/results.json')
            results['requests']['R-sibling'] = {'status': 'qc_passed'}
            write_json_atomic(path / 'production/results.json', results)
        elif mutation == 'publication': w.store_publication(path, 'R-sibling', {'status': 'fbbm_queued'})
        elif mutation == 'children':
            state = control.read(path)
            state['children'] = [{'pid': 9876, 'request_id': 'R-sibling'}]
            control.write(path, state)
        else: result_store.patch(path / 'production', request['request_id'], **dict(record, error='target changed'))
        return evidence
    monkeypatch.setattr(w, 'approval_evidence', progress)
    code, body = post(api, '/api/batches/' + batch['id'] + '/publish',
                      {'request_id': request['request_id'], 'sha256': record['output_sha256']})
    assert code == (409 if mutation == 'target' else 202), body


@pytest.mark.parametrize('rid,expected', [('R-sibling', 202), ('target', 400), (None, 400)])
def test_verify_delivery_gate_blocks_target_or_legacy_but_allows_sibling(api, workspace, rid, expected):
    from test_round1_delivery import post, uncertain
    batch, path, target, video, record, entry = uncertain(workspace)
    job = dict(id=uuid.uuid4().hex, batch_id=batch['id'], phase='render', action='start', status='queued')
    if rid is not None: job['request_id'] = target if rid == 'target' else rid
    web.save(job)
    code, body = post(api, '/api/batches/' + batch['id'] + '/verify-delivery',
                      {'request_id': target, 'sha256': record['output_sha256']})
    assert code == expected, body


def test_stopped_render_residents_hold_project_slots_and_upload_keeps_capacity(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 4, 'per_project': 2, 'upload': 2}})
    monkeypatch.setattr(web.storage, 'scheduler_job_budget', lambda *args: {})
    path = batch_path()
    children = []
    for index, rid in enumerate(['R1', 'R2'], 100):
        token = uuid.uuid4().hex
        child = dict(pid=index, pgid=index, process_token=token, request_id=rid, lane='render', instance=instance_marker())
        children.append(child)
        control.PROCESSES.rows[index] = dict(pgid=index, state='T', arguments=['python', '-X', 'exactcut_process_token=' + token])
    write_json_atomic(path / 'control.json', {'paused': False, 'children': children, 'suspended': children})
    monkeypatch.setattr(control.os, 'killpg', lambda *args: None)
    request_job('a', 'render', path, 'R3'); request_job('b', 'upload', path, 'R4')
    owned = {}
    try:
        web.worker_tick([], owned)
        assert set(owned) == {'b' * 32}
        assert web.project_render_usage(owned)[path.name] == 2
        assert web.resource_usage(owned)[0]['render'] == 2
    finally:
        close_logs(owned)


def test_parallel_approval_ignores_shared_render_message(api, workspace, monkeypatch):
    from test_round1_delivery import post
    from test_workflow import imported, complete_asset
    batch = imported(); path, request, video, record = complete_asset(batch)
    w.update(path, request_parallel=True)
    original = w.approval_evidence
    def progress(*args):
        evidence = original(*args)
        w.update(path, 'producing', 'sibling starts rendering')
        return evidence
    monkeypatch.setattr(w, 'approval_evidence', progress)
    code, body = post(api, '/api/batches/' + batch['id'] + '/publish',
                      {'request_id': request['request_id'], 'sha256': record['output_sha256']})
    assert code == 202, body
