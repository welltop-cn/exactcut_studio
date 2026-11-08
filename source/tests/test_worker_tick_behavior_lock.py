"""Behavior contract for the scheduler, before read/lock optimizations.

Existing coverage retained rather than duplicated:
* 2: test_round1_scheduler.test_stopped_children_hold_lane_while_another_lane_can_start
  and test_resume_counts_residents_once_and_blocks_overcapacity cover stopped roots;
  test_scheduler_admission_policy.test_four_real_lane_children_overlap_and_second_of_each_stays_queued
  covers real ordinary lane overlap. Cut/mosaic uses separate editor slots, not a
  worker_tick lane (test_production_own_slots.test_editor_media_still_waits_for_its_own_slots).
* 3: test_round1_scheduler.test_resume_retains_batch_serial_limit and
  test_intra_project_scheduler.test_reconcile_counts_running_render_before_resuming_siblings
  cover legacy/request resume fences.
* 4: test_pause_archive_cancel_cutting.test_pause_is_refused_while_cut_task_is_queued
  also covers managed parents and queued/cancelling cut and mosaic work.
* 5: test_phase1_recovery.test_restart_adopts_launch_before_pid_or_child_was_saved,
  test_concurrency_recovery.test_dead_stage_requeues_without_result and
  test_managed_scheduler_verification.test_managed_verification_handoff_survives_process_exit
  cover further crash windows and managed verification.
* 6: test_concurrency.test_fetch_completion_queues_one_render_and_poll_reschedules
  and test_concurrency_recovery.test_completed_result_recovers_successor_gap cover
  stage advancement/idempotency; test_intra_project_scheduler.test_fetch_fanout_recovers_partial_persistence_idempotently
  covers per-request fanout.
* Separate editor admission is already fixed by test_no_preclaim_slots,
  test_no_transfer_slots, test_short_ops_no_slots and test_final_queue_only.

Execution/process discovery, disk-size estimation and cutting queue reports are
faked here. JSON,
recovery, admission, reconciliation, ownership and durable cursors remain real.
"""
import json
import signal
from unittest.mock import Mock

import pytest

from exactcut import batch_control as control, concurrency, material_workers, workflow as w, webserver as web
from exactcut.runtime import instance_marker
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs
from test_material_versions import enrolled, token
from test_round1_scheduler import resident
from test_workflow import workspace


@pytest.fixture
def scheduler(workspace, runner, monkeypatch):
    # Use the same durable jobs directory as the real managed outbox adapter.
    monkeypatch.setattr(w, 'BATCHES', workspace / '.web-state/batches')
    w.BATCHES.mkdir(parents=True)
    monkeypatch.setattr(web, 'JOBS', workspace / '.web-state/jobs')
    web.JOBS.mkdir()
    monkeypatch.setattr(web, 'ROOT', workspace)
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setattr(web.storage, 'scheduler_job_budget', lambda *args: {})
    monkeypatch.setattr(control.os, 'getpgid', lambda pid: pid)
    processes = []

    def spawn(command, **kwargs):
        pid = 1000 + len(processes)
        process = Mock(pid=pid, returncode=None)
        process.poll = lambda: process.returncode
        control.PROCESSES.rows[pid] = dict(pgid=pid, state='S', arguments=command)
        processes.append(process)
        return process

    def signal_group(pid, sig):
        control.PROCESSES.rows[pid]['state'] = 'T' if sig == signal.SIGSTOP else 'S'

    monkeypatch.setattr(web.subprocess, 'Popen', spawn)
    monkeypatch.setattr(control.os, 'killpg', signal_group)
    pending, owned = [], {}
    try:
        yield pending, owned
    finally:
        close_logs(owned)


def limits(monkeypatch, **values):
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': values})


def saved(job):
    return read_json(web.JOBS / (job['id'] + '.json'))


def batch(key, owner='alice', paused=False):
    path = w.BATCHES / (key * 32)
    path.mkdir()
    for name, value in [('draft', {}), ('runtime', {'status': 'review', 'events': []}),
                        ('access', {'owner': owner}),
                        ('control', {'paused': paused, 'archived': False, 'children': [], 'suspended': []})]:
        write_json_atomic(path / (name + '.json'), value)
    return path


def finish_owned(owned):
    for _, process, _ in owned.values():
        process.returncode = 0
        control.PROCESSES.rows.pop(process.pid)


def managed_candidate():
    m, parent, state = enrolled()
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()})
    event = next(iter(state['outbox'].values()))
    child = material_workers.materialize(parent, event)
    return m, parent, child, event


def test_created_fifo_and_cursor_advance_across_three_ticks(scheduler, monkeypatch):
    pending, owned = scheduler
    limits(monkeypatch, render=1, max_active_children=1)
    # Enqueue out of created order, including the same owner's two jobs.
    jobs = [queued(key, 'render', owner=owner) for key, owner in
            [('d', 'bob'), ('b', 'alice'), ('c', 'bob'), ('a', 'alice')]]
    write_json_atomic(web.JOBS.parent / 'scheduler.json', {'owner': 'alice'})
    trace = []
    for _ in range(3):
        web.worker_tick(pending, owned)
        trace.append((list(owned), read_json(web.JOBS.parent / 'scheduler.json')))
        finish_owned(owned)
    assert trace == [(['c' * 32], {'owner': 'bob'}),
                     (['a' * 32], {'owner': 'alice'}),
                     (['d' * 32], {'owner': 'bob'})]
    assert {j['id']: saved(j)['status'] for j in jobs} == {
        'a' * 32: 'completed', 'b' * 32: 'queued', 'c' * 32: 'completed', 'd' * 32: 'running'}


@pytest.mark.parametrize('mode', ['independent', 'total', 'serial'])
def test_lane_and_total_limits_with_serial_switch(scheduler, monkeypatch, mode):
    pending, owned = scheduler
    config = {lane: 2 for lane in concurrency.LANES}
    config.update(max_active_children=3 if mode == 'total' else 8, enabled=mode != 'serial')
    limits(monkeypatch, **config)
    for i, lane in enumerate(concurrency.LANES):
        for offset in range(3):
            queued(chr(97 + i * 3 + offset), lane)
    web.worker_tick(pending, owned)
    expected = {'independent': 'abdeghjk', 'total': 'abd', 'serial': 'a'}[mode]
    assert list(owned) == [key * 32 for key in expected]
    counts, roots, _, _ = web.resource_usage(owned)
    assert len(roots) == len(expected)
    assert counts == {lane: sum(concurrency.lane(j) == lane for j, _, _ in owned.values())
                      for lane in concurrency.LANES}
    assert all(counts[lane] <= concurrency.settings()[lane] for lane in concurrency.LANES)
    assert all(saved(j)['status'] == 'queued' for j, _ in pending)
    web.worker_tick(pending, owned)
    assert list(owned) == [key * 32 for key in expected]


def test_pause_then_resume_waits_for_resident_capacity(scheduler, monkeypatch):
    pending, owned = scheduler
    limits(monkeypatch, render=1)
    path, child = resident('a', 100, paused=False)
    control.PROCESSES.rows[100]['state'] = 'S'
    write_json_atomic(path / 'control.json', {'paused': True, 'children': [child], 'suspended': [child]})
    waiting = queued('b', 'render', batch=path.name)
    web.worker_tick(pending, owned)
    assert control.PROCESSES.rows[100]['state'] == 'T'
    assert saved(waiting)['status'] == 'queued' and not owned
    control.change(path, 'resume')
    other, _ = resident('c', 101, paused=True)
    web.worker_tick(pending, owned)
    assert control.PROCESSES.rows[100]['state'] == 'T'
    assert control.read(path)['suspended'] and not control.read(path)['paused']
    control.PROCESSES.rows.pop(101)
    web.worker_tick(pending, owned)
    assert control.PROCESSES.rows[100]['state'] == 'S'
    assert control.read(path)['suspended'] == []
    assert control.read(other)['children'] == []
    assert saved(waiting)['status'] == 'queued' and not owned


@pytest.mark.parametrize('action,expected', [('pause', (True, False)), ('archive', (True, True))])
def test_internal_child_inherits_parent_media_control(scheduler, monkeypatch, action, expected):
    pending, owned = scheduler
    m, parent, child, event = managed_candidate()
    # Durable outbox job, with its real phase/command, still waits on parent flags.
    job = material_workers._job(parent, event, child)
    web.save(job)
    child_control = dict(paused=False, archived=False, children=[], suspended=[])
    write_json_atomic(child / 'control.json', child_control)
    identity = dict(pid=120, pgid=120, process_token='a' * 32, lane='render',
                    instance=instance_marker(), request_id=event['asset_id'])
    control.PROCESSES.rows[120] = dict(pgid=120, state='S',
                                     arguments=['python', '-X', 'exactcut_process_token=' + 'a' * 32])
    control.remember_child(child, identity)
    state = m.read(parent)
    m.control(parent, action, {'revision': state['revision'], 'mutation_id': token()})
    # The local child file still says false; effective flags come from media-state.
    assert not read_json(child / 'control.json')['paused']
    assert (control.read(child)['paused'], control.read(child)['archived']) == expected
    web.worker_tick(pending, owned)
    value = control.read(child)
    assert (value['paused'], value['archived']) == expected
    assert saved(job)['status'] == 'queued' and not owned
    assert control.PROCESSES.rows[120]['state'] == 'T'
    assert [c['pid'] for c in value['children']] == [120]
    if action == 'pause':
        m.control(parent, 'resume', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
        web.worker_tick(pending, owned)
        assert control.PROCESSES.rows[120]['state'] == 'S'
        assert not control.read(child)['paused']
        assert saved(job)['status'] == 'queued' and not owned


@pytest.mark.parametrize('managed', [False, True])
def test_pause_rejected_while_cutting_is_running(scheduler, monkeypatch, managed):
    from exactcut import mosaic_queue
    if managed:
        m, path, _ = enrolled()
    else:
        path = batch('a')
    monkeypatch.setattr(mosaic_queue, 'load', lambda: {'tasks': [{'batch_id': path.name}]})
    monkeypatch.setattr(mosaic_queue, 'report', lambda item: {'status': 'running'})
    with pytest.raises(ValueError, match=control.PAUSE_CUTTING_ERROR):
        if managed:
            m.control(path, 'pause', {'revision': m.read(path)['revision'], 'mutation_id': token()})
        else:
            control.change(path, 'pause')
    assert not (m.read(path)['control']['paused'] if managed else control.read(path)['paused'])


def test_restart_adopts_live_and_recovers_dead_jobs(scheduler):
    pending, owned = scheduler
    live_jobs = []
    for key, pid, status in [('a', 100, 'running'), ('b', 101, 'queued')]:
        path, child = resident(key, pid, paused=False)
        control.PROCESSES.rows[pid]['state'] = 'S'
        (path / 'control.json').unlink()
        job = queued(key, 'render', batch=path.name)
        job.update(status=status, process_token=child['process_token'])
        if status == 'running': job.update(pid=pid, pgid=pid)
        web.save(job)
        live_jobs.append((path, job, pid))
    dead = queued('c', 'render'); dead.update(status='running'); web.save(dead)
    stage = queued('d', 'fetch'); stage.update(status='running', phase='fetch', attempt=1); web.save(stage)
    web.recover_jobs(owned={})
    assert saved(dead)['status'] == 'interrupted'
    assert saved(stage)['status'] == 'queued'
    assert 'pid' not in saved(stage) and 'process_token' not in saved(stage)
    web.worker_tick(pending, owned)
    for path, job, pid in live_jobs:
        assert saved(job)['status'] == 'running' and saved(job)['adopted_after_restart'] is True
        assert [c['pid'] for c in control.read(path)['children']] == [pid]
        assert job['id'] not in owned
    assert list(owned) == [stage['id']]
    assert web.resource_usage(owned)[0] == dict(fetch=1, render=2, upload=0, poll=0)


def test_finished_child_releases_slot_and_dispatches_in_same_tick(scheduler, monkeypatch):
    pending, owned = scheduler
    limits(monkeypatch, render=1, max_active_children=1)
    path = batch('d')
    first = queued('a', 'render', batch=path.name)
    second = queued('b', 'render', batch=path.name)
    web.worker_tick(pending, owned)
    assert list(owned) == [first['id']]
    assert len(control.read(path)['children']) == 1
    finish_owned(owned)
    web.worker_tick(pending, owned)
    assert saved(first)['status'] == 'completed' and saved(first)['exit_code'] == 0
    assert list(owned) == [second['id']]
    assert [c['pid'] for c in control.read(path)['children']] == [owned[second['id']][1].pid]
    assert control.read(path)['suspended'] == []
    assert web.resource_usage(owned)[0]['render'] == 1
    finish_owned(owned)
    web.worker_tick(pending, owned)
    assert saved(second)['status'] == 'completed'
    assert not owned and control.read(path)['children'] == []
    assert web.resource_usage(owned)[0] == dict.fromkeys(concurrency.LANES, 0)


@pytest.mark.parametrize('broken', ['missing_control', 'missing_draft'])
def test_partial_files_do_not_block_other_jobs(scheduler, broken):
    pending, owned = scheduler
    path = batch('d')
    affected = queued('a', 'render', batch=path.name)
    ordinary = queued('b', 'fetch')
    if broken == 'missing_control':
        (path / 'control.json').unlink()
    else:
        (path / 'draft.json').unlink()
    web.worker_tick(pending, owned)
    assert ordinary['id'] in owned
    assert saved(affected)['status'] == ('interrupted' if broken == 'missing_draft' else 'running')
    assert list(owned) == ([ordinary['id']] if broken == 'missing_draft' else [affected['id'], ordinary['id']])
    if broken == 'missing_control':
        assert not control.read(path)['paused']
        assert len(control.read(path)['children']) == 1


@pytest.mark.parametrize('content,error', [('{bad', json.JSONDecodeError), ('[]', TypeError)])
def test_bad_job_record_currently_aborts_tick(scheduler, content, error):
    pending, owned = scheduler
    ordinary = queued('a', 'fetch')
    (web.JOBS / 'broken.json').write_text(content)
    # stored_jobs skips these records, but storage.rebuild_reservations currently
    # reads them again without guarding JSON/type errors. Lock that exception too.
    with pytest.raises(error):
        web.worker_tick(pending, owned)
    assert not owned and saved(ordinary)['status'] == 'queued'


@pytest.mark.parametrize('broken', ['missing', 'corrupt'])
def test_missing_parent_state_currently_aborts_tick(scheduler, broken):
    pending, owned = scheduler
    _, parent, child, _ = managed_candidate()
    state_file = parent / 'media-state.json'
    if broken == 'missing': state_file.unlink()
    else: state_file.write_text('{bad')
    ordinary = queued('a', 'fetch')
    # Outbox catches its own error, but recover_jobs -> own_live_children -> read
    # fails closed on this internal child's parent. Preserve the current exception;
    # this regression test deliberately does not repair or skip the corrupt parent.
    with pytest.raises(ValueError, match='纳管记录缺失或损坏'):
        web.worker_tick(pending, owned)
    assert not owned and saved(ordinary)['status'] == 'queued'


def test_three_tick_golden_mixed_managed_and_legacy(scheduler, monkeypatch):
    pending, owned = scheduler
    limits(monkeypatch, render=2, max_active_children=4)
    parents, children, managed, jobs = {}, {}, {}, {}
    for label, owner, action in [('managed_a', 'alice', 'pause'), ('managed_b', 'carol', 'archive')]:
        m, parent, child, event = managed_candidate()
        write_json_atomic(parent / 'access.json', {'owner': owner})
        write_json_atomic(child / 'access.json', {'owner': owner})
        job = material_workers._job(parent, event, child)
        key = 'g' if label == 'managed_a' else 'h'
        job['created'] = ord(key)
        web.save(job)
        jobs[key] = job
        m.control(parent, action, {'revision': m.read(parent)['revision'], 'mutation_id': token()})
        parents[label], children[label + '_child'], managed[label] = parent, child, m
    parents['legacy_a'] = batch('1', 'alice')
    parents['legacy_b'] = batch('2', 'bob')
    parents['legacy_c'], identity = resident('e', 100, owner='carol', paused=True)
    parents['legacy_d'] = batch('4', 'bob')
    for key, lane, name in [('a', 'render', 'legacy_a'), ('b', 'render', 'legacy_a'),
                            ('c', 'fetch', 'legacy_b'), ('e', 'render', 'legacy_c'),
                            ('f', 'upload', 'legacy_d'), ('k', 'fetch', 'legacy_b')]:
        jobs[key] = queued(key, lane, batch=parents[name].name)
    jobs['e'].update(status='running', pid=100, pgid=100, process_token=identity['process_token'])
    web.save(jobs['e'])
    for key, name in [('i', 'legacy_a'), ('j', 'legacy_d')]:
        jobs[key] = queued(key, 'render', batch=parents[name].name)
        jobs[key]['status'] = 'completed'
        web.save(jobs[key])
    # Random managed job IDs are normalized to stable scenario names, never order.
    names = {job['id']: key for key, job in jobs.items()}
    trace = []
    for tick in range(3):
        if tick == 1:
            for key in ('a', 'c'):
                process = owned[jobs[key]['id']][1]
                process.returncode = 0
                control.PROCESSES.rows.pop(process.pid)
        if tick == 2:
            finish_owned(owned)
            parent = parents['managed_a']
            m.control(parent, 'resume', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
        before = set(owned)
        web.worker_tick(pending, owned)
        trace.append({'dispatch': [names[jid] for jid in owned if jid not in before],
                      'cursor': read_json(web.JOBS.parent / 'scheduler.json')['owner']})

    pid_names = {saved(job).get('pid'): key for key, job in jobs.items() if saved(job).get('pid')}
    controls = {}
    for name, path in {**parents, **children}.items():
        value = control.read(path)
        controls[name] = {'paused': bool(value.get('paused')), 'archived': bool(value.get('archived')),
                          'suspended': [pid_names[c['pid']] for c in value.get('suspended', [])],
                          'children': [pid_names[c['pid']] for c in value.get('children', [])]}
    actual = {'ticks': trace, 'statuses': {key: saved(job)['status'] for key, job in jobs.items()},
              'controls': controls,
              'media_controls': {name: module.read(parents[name])['control'] for name, module in managed.items()}}
    empty = {'paused': False, 'archived': False, 'suspended': [], 'children': []}
    assert actual == {
        'ticks': [{'dispatch': ['a', 'c', 'f'], 'cursor': 'bob'},
                  {'dispatch': ['b', 'k'], 'cursor': 'bob'},
                  {'dispatch': ['g'], 'cursor': 'alice'}],
        'statuses': {'a': 'completed', 'b': 'completed', 'c': 'completed', 'e': 'running',
                     'f': 'completed', 'g': 'running', 'h': 'queued', 'i': 'completed',
                     'j': 'completed', 'k': 'completed'},
        'controls': {'managed_a': empty, 'managed_b': empty,
                     'legacy_a': empty, 'legacy_b': empty, 'legacy_d': empty,
                     'legacy_c': {'paused': True, 'archived': False, 'suspended': ['e'], 'children': ['e']},
                     'managed_a_child': {'paused': False, 'archived': False, 'suspended': [], 'children': ['g']},
                     'managed_b_child': {'paused': True, 'archived': True, 'suspended': [], 'children': []}},
        'media_controls': {'managed_a': {'paused': False, 'archived': False},
                           'managed_b': {'paused': True, 'archived': True}},
    }
