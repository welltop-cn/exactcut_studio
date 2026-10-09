import json
import logging
import signal
from pathlib import Path
from unittest.mock import Mock

import pytest
from exactcut import batch_control as control, concurrency, webserver as web, workflow
from exactcut.runtime import instance_marker
from exactcut.util import load_config, read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs


def resident(key, pid, lane='render', owner='alice', paused=True, batch=None):
    path = workflow.BATCHES / (batch or key * 32)
    path.mkdir(exist_ok=True)
    write_json_atomic(path / 'draft.json', {})
    write_json_atomic(path / 'runtime.json', {'status': 'producing'})
    write_json_atomic(path / 'access.json', {'owner': owner})
    child = dict(pid=pid, pgid=pid, process_token=key * 32, lane=lane, instance=instance_marker())
    state = read_json(path / 'control.json') if (path / 'control.json').exists() else {'children': [], 'suspended': []}
    state['paused'] = paused
    state['children'].append(child); state['suspended'].append(child)
    write_json_atomic(path / 'control.json', state)
    control.PROCESSES.rows[pid] = dict(pgid=pid, state='T', arguments=['python', '-X', 'exactcut_process_token=' + key * 32])
    return path, child


@pytest.mark.parametrize('lane,capacity', [('render', 3), ('fetch', 1)])
def test_stopped_children_hold_lane_while_another_lane_can_start(runner, monkeypatch, lane, capacity):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {lane: capacity, 'max_active_children': 8}})
    for i in range(capacity): resident(chr(97 + i), 100 + i, lane=lane)
    blocked = queued('d', lane)
    allowed = queued('e', 'upload')
    owned = {}
    try:
        web.worker_tick([], owned)
        assert blocked['id'] not in owned
        assert set(owned) == {allowed['id']}
        assert web.resource_usage(owned)[0][lane] == capacity
    finally: close_logs(owned)


def test_stopped_legacy_and_owned_children_hold_lane(runner):
    legacy = queued('a', 'fetch'); legacy.update(status='running', pid=123, pgid=123, process_token='a' * 32)
    web.save(legacy)
    control.PROCESSES.rows[123] = dict(pgid=123, state='T', arguments=['python', '-X', 'exactcut_process_token=' + 'a' * 32])
    assert web.resource_usage()[0]['fetch'] == 1
    assert web.resource_usage({legacy['id']: (legacy, Mock(), Mock())})[0]['fetch'] == 1


@pytest.mark.parametrize('limits,count,resume', [({'render': 3, 'max_active_children': 3}, 3, True),
    ({'render': 2, 'max_active_children': 8}, 3, False), ({'render': 3, 'max_active_children': 2}, 3, False)])
def test_resume_counts_residents_once_and_blocks_overcapacity(runner, monkeypatch, limits, count, resume):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': limits})
    for i in range(count): resident(chr(97 + i), 100 + i, paused=False)
    kill = Mock(); monkeypatch.setattr(control.os, 'killpg', kill)
    control.reconcile(workflow.BATCHES)
    assert kill.call_count == (count if resume else 0)


def test_resume_retains_batch_serial_limit(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 2, 'max_active_children': 2}})
    resident('a', 100, paused=False)
    resident('b', 101, paused=False, batch='a' * 32)
    kill = Mock(); monkeypatch.setattr(control.os, 'killpg', kill)
    control.reconcile(workflow.BATCHES)
    assert kill.call_count == 1


@pytest.mark.parametrize('config', [{}, {'concurrency': {'render': 3, 'fetch': 2}}])
def test_missing_total_is_conservative_without_default_merge(monkeypatch, config):
    monkeypatch.setattr(concurrency, 'load_config', lambda: config)
    assert concurrency.settings()['max_active_children'] == 1


@pytest.mark.parametrize('segment', [None, {'render': 3}, {'enabled': True, 'render': 3, 'fetch': 2, 'upload': 2, 'poll': 4, 'max_active_children': 8}])
def test_startup_logs_merged_effective_limits_before_initializing_data(runner, monkeypatch, caplog, segment):
    config = runner / 'runtime.json'
    values = {'environment': 'development'}
    if segment is not None: values['concurrency'] = segment
    write_json_atomic(config, values)
    monkeypatch.setenv('EXACTCUT_CONFIG_FILE', str(config))
    monkeypatch.setattr(concurrency, 'load_config', load_config)
    limits = concurrency.settings()
    assert limits['max_active_children'] == (8 if segment and 'max_active_children' in segment else 1)
    monkeypatch.setattr(web, 'load_env_file', lambda *a: None)
    def claim():
        message = next(record.getMessage() for record in caplog.records if 'Effective concurrency settings:' in record.getMessage())
        assert json.loads(message.split(': ', 1)[1]) == limits
        raise RuntimeError('stop before data initialization')
    monkeypatch.setattr(web, 'claim_data_root', claim)
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match='stop before data'):
        web.main()


def test_production_allows_four_polls_with_render_within_total(runner, monkeypatch):
    config = read_json(Path(concurrency.__file__).parents[1] / 'config/production.json')
    monkeypatch.setattr(concurrency, 'load_config', lambda: config)
    assert concurrency.settings()['poll'] == 4
    for key in 'abcd': queued(key, 'poll')
    queued('e', 'render')
    owned = {}
    try:
        web.worker_tick([], owned)
        assert len(owned) == 5
    finally: close_logs(owned)


def test_resume_owner_round_robin_uses_durable_dispatch_cursor(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 4, 'max_active_children': 4}})
    for key, pid, owner in [('a', 100, 'alice'), ('b', 101, 'alice'), ('c', 102, 'alice'), ('d', 103, 'bob')]:
        resident(key, pid, owner=owner, paused=False)
    write_json_atomic(runner / 'scheduler.json', {'owner': 'alice'})
    kill = Mock(); monkeypatch.setattr(control.os, 'killpg', kill)
    control.reconcile(workflow.BATCHES)
    assert [call.args[0] for call in kill.call_args_list] == [103, 100, 101, 102]
    assert read_json(runner / 'scheduler.json')['owner'] == 'alice'
    kill.reset_mock()
    write_json_atomic(runner / 'scheduler.json', {'owner': 'bob'})
    control.reconcile(workflow.BATCHES)
    assert [call.args[0] for call in kill.call_args_list] == [100, 103, 101, 102]


def test_response_queue_rank_includes_delayed_and_lane_blocked_without_persisting(runner):
    a = queued('a', 'render', owner='alice'); queued('b', 'render', owner='alice')
    c = queued('c', 'render', owner='bob'); c['not_before'] = web.time.time() + 60; web.save(c)
    write_json_atomic(runner / 'scheduler.json', {'owner': 'alice'})
    assert web.job_response(c)['queue_position'] == 1
    assert web.job_response(a)['queue_position'] == 2
    assert 'queue_position' not in read_json(web.JOBS / (a['id'] + '.json'))
    a['status'] = 'succeeded'; web.save(a)
    assert web.job_response(a)['queue_position'] is None


def handler(path, user):
    result = web.Handler.__new__(web.Handler)
    result.path = path
    result.server = Mock(base_path='', auth_db=None)
    result.user = user
    result.authorized = lambda: True
    result.respond = Mock()
    return result


def test_batch_queue_rank_response_obeys_existing_access_filter(runner, monkeypatch):
    alice_path, _ = resident('a', 100, owner='alice')
    bob_path, _ = resident('b', 101, owner='bob')
    a = queued('c', 'render', batch=alice_path.name)
    b = queued('d', 'render', batch=bob_path.name)
    monkeypatch.setattr(workflow, 'detail', lambda batch: {'runtime': {'status': 'producing'}, 'draft': {}})
    request = handler('/api/batches/' + alice_path.name, {'username': 'alice', 'role': 'user'})
    request.do_GET()
    code, body = request.respond.call_args.args
    assert code == 200 and body['submission']['id'] == a['id']
    assert body['submission']['queue_position'] == 1
    assert b['id'] not in json.dumps(body)
    request.path = '/api/batches/' + bob_path.name
    request.do_GET()
    assert request.respond.call_args.args[0] == 404
    assert 'submission' not in request.respond.call_args.args[1]
    request.path = '/api/state'
    request.do_GET()
    assert request.respond.call_args.args[0] == 403


def test_job_submission_cannot_set_or_persist_queue_position(runner):
    import io
    payload = json.dumps({'action': 'refresh_catalog', 'queue_position': 999}).encode()
    request = handler('/api/jobs', {'username': 'admin', 'role': 'admin'})
    request.headers = {'X-Exactcut-Request': '1', 'Content-Length': str(len(payload))}
    request.rfile = io.BytesIO(payload)
    request.handle_post()
    code, body = request.respond.call_args.args
    assert code == 202 and body['queue_position'] == 1
    assert 'queue_position' not in read_json(web.JOBS / (body['id'] + '.json'))


def test_queue_rank_remains_visible_for_paused_batch(runner):
    path, _ = resident('a', 100)
    job = queued('b', 'render', batch=path.name)
    assert web.job_response(job)['queue_position'] == 1


def test_queue_rank_handles_old_queued_job_after_its_batch_was_deleted(runner):
    orphan = queued('a', 'render', batch='d' * 32)
    ordinary = queued('b', 'fetch')
    assert web.job_response(orphan)['queue_position'] == 1
    assert web.job_response(ordinary)['queue_position'] == 2


def test_distinct_live_roots_with_same_token_block_resume_at_cap_one(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 3, 'max_active_children': 1}})
    first, child = resident('a', 100, paused=False)
    second, duplicate_token = resident('b', 101, paused=False)
    duplicate_token['process_token'] = child['process_token']
    write_json_atomic(second / 'control.json', {'children': [duplicate_token], 'paused': False})
    control.PROCESSES.rows[101]['arguments'] = control.PROCESSES.rows[100]['arguments']
    kill = Mock(); monkeypatch.setattr(control.os, 'killpg', kill)
    assert control.reconcile(workflow.BATCHES)
    kill.assert_not_called()
    counts, residents, _, _ = web.resource_usage()
    assert counts['render'] == 2 and len(residents) == 2


def test_same_verified_root_in_owned_and_control_counts_once_before_pid_persistence(runner):
    path, child = resident('a', 100)
    job = queued('b', 'render', batch=path.name)
    job.update(status='running', process_token=child['process_token'])
    web.save(job)
    counts, residents, _, _ = web.resource_usage({job['id']: (job, Mock(), Mock())})
    assert counts['render'] == 1 and len(residents) == 1


def test_same_token_different_legacy_roots_hold_global_budget(runner, monkeypatch):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 3, 'max_active_children': 2}})
    for key, pid in [('a', 100), ('b', 101)]:
        job = queued(key, 'render')
        job.update(status='running', pid=pid, pgid=pid, process_token='c' * 32)
        web.save(job)
        control.PROCESSES.rows[pid] = dict(pgid=pid, state='T', arguments=['python', '-X', 'exactcut_process_token=' + 'c' * 32])
    queued('d', 'fetch')
    owned = {}
    try:
        web.worker_tick([], owned)
        assert not owned
        counts, residents, _, _ = web.resource_usage()
        assert counts['render'] == 2 and len(residents) == 2
    finally: close_logs(owned)


@pytest.mark.parametrize('status', ['running', 'queued', 'owned'])
def test_worker_registers_unrecorded_live_roots_before_resuming(runner, monkeypatch, status):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 1, 'max_active_children': 1}})
    resident('a', 100, paused=False)
    path, child = resident('b', 101, paused=False)
    (path / 'control.json').unlink()
    control.PROCESSES.rows[101]['state'] = 'S'
    job = queued('c', 'render', batch=path.name)
    job.update(status='running' if status == 'owned' else status, process_token=child['process_token'])
    if status in {'running', 'owned'}: job.update(pid=101, pgid=101)
    web.save(job)
    kill = Mock(); monkeypatch.setattr(control.os, 'killpg', kill)
    owned = {job['id']: (job, Mock(poll=lambda: None), Mock())} if status == 'owned' else {}
    web.worker_tick([], owned)
    kill.assert_not_called()
    assert control.recorded_children(path)[0]['pid'] == 101
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'running'
    assert web.resource_usage()[0]['render'] == 2
