"""tick 快照的可见性、异常和调度等价回归。"""
import copy
import json
import os
import queue
import shutil
import threading
from collections import Counter
from contextlib import nullcontext

import pytest
from exactcut import tick_snapshot as snap, material_versions as m, batch_control as control
from exactcut import webserver as web, workflow as w, material_workers
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs
from test_workflow import workspace
from test_material_versions import token
from test_worker_tick_behavior_lock import (scheduler, limits, batch, managed_candidate,
    finish_owned, saved, test_finished_child_releases_slot_and_dispatches_in_same_tick)


def test_scope_lifetime_and_writes(tmp_path):
    file = tmp_path / 'job.json'
    write_json_atomic(file, {'id': 'one', 'nested': [1]})
    calls = []
    def load():
        calls.append(1)
        return read_json(file)
    for _ in range(2): snap.job(file, load)
    assert len(calls) == 2
    with pytest.raises(RuntimeError), snap.scope():
        assert snap.job(file, load) == snap.job(file, load)
        assert len(calls) == 3
        snap.job(file, load)['nested'].append(2)
        assert snap.job(file, load)['nested'] == [1]
        write_json_atomic(file, {'id': 'two'})
        assert not snap._local.cache['entries']
        assert snap.job(file, load)['id'] == 'two'
        thread_calls = []
        def writer():
            for _ in range(2): snap.job(file, lambda: thread_calls.append(1) or read_json(file))
            write_json_atomic(file, {'id': 'three'})
        t = threading.Thread(target=writer); t.start(); t.join()
        assert thread_calls == [1, 1]
        assert not snap._local.cache['entries']
        assert snap.job(file, load)['id'] == 'three'
        replacement = tmp_path / 'replacement'
        replacement.write_text('{"id": "four"}')
        os.replace(replacement, file)
        assert snap.job(file, load)['id'] == 'four'
        raise RuntimeError()
    assert not snap._active
    write_json_atomic(file, {'id': 'next'})
    with snap.scope(): assert snap.job(file, load)['id'] == 'next'


def test_errors_and_job_copies(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(web, 'JOBS', tmp_path)
    file = tmp_path / 'bad.json'
    with snap.scope():
        for _ in range(2):
            with pytest.raises(FileNotFoundError): snap.job(file, lambda: read_json(file))
        file.write_text('{bad')
        for _ in range(2):
            with pytest.raises(json.JSONDecodeError): snap.job(file, lambda: read_json(file))
            assert list(web.stored_jobs()) == []
        assert caplog.text.count('Unreadable job record') == 2
        write_json_atomic(file, {'id': 'ok', 'nested': [1]})
        list(web.stored_jobs())[0]['nested'].append(2)
        assert list(web.stored_jobs()) == [{'id': 'ok', 'nested': [1]}]
        write_json_atomic(tmp_path / 'other.json', {'id': 'other'})
        assert [j['id'] for j in web.stored_jobs()] == [read_json(f)['id'] for f in tmp_path.glob('*.json')]
        file.unlink()
        assert list(web.stored_jobs()) == [{'id': 'other'}]


def test_parent_validation_and_fresh(scheduler, monkeypatch):
    _, parent, child, _ = managed_candidate()
    original = m.read(parent)
    with snap.scope():
        assert not control.read(child)['paused']
        identity = snap.internal(child); identity['parent_batch_id'] = 'wrong'
        assert snap.internal(child)['parent_batch_id'] == parent.name
        m.control(parent, 'pause', {'revision': original['revision'], 'mutation_id': token()})
        assert control.read(child)['paused']
        with monkeypatch.context() as patch:
            patch.setattr(snap, 'internal', lambda p: pytest.fail('cached identity'))
            patch.setattr(snap, 'parent_state', lambda p: pytest.fail('cached parent'))
            assert control.read(child, fresh=True)['paused']
    file = parent / 'media-state.json'
    with snap.scope():
        for content in ['{bad', '{}']:
            file.write_text(content)
            for _ in range(2):
                with pytest.raises(ValueError, match='纳管记录缺失或损坏'): control.read(child)
        file.unlink()
        with pytest.raises(ValueError, match='纳管记录缺失或损坏'): control.read(child)
        write_json_atomic(file, original)
        assert not control.read(child)['paused']


def test_pause_during_dispatch(scheduler, monkeypatch):
    pending, owned = scheduler
    _, parent, child, event = managed_candidate()
    job = material_workers._job(parent, event, child); web.save(job)
    original = web.fair_pending
    def pause(entries):
        assert not control.read(child)['paused']
        m.control(parent, 'pause', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
        yield from original(entries)
    monkeypatch.setattr(web, 'fair_pending', pause)
    calls = []
    read = control.read
    def tracked(path, **kwargs):
        if path == child and kwargs.get('fresh'): calls.append(path)
        return read(path, **kwargs)
    monkeypatch.setattr(control, 'read', tracked)
    web.worker_tick(pending, owned)
    assert calls and not owned and saved(job)['status'] == 'queued'


def test_pause_before_sigcont(scheduler, monkeypatch):
    from exactcut.runtime import instance_marker
    from exactcut import concurrency
    _, parent, child, _ = managed_candidate()
    identity = dict(pid=123, pgid=123, process_token='a'*32, lane='render', instance=instance_marker())
    write_json_atomic(child / 'control.json', {'children': [identity], 'suspended': [identity]})
    control.PROCESSES.rows[123] = dict(pgid=123, state='T', arguments=['python', '-X', 'exactcut_process_token='+'a'*32])
    original = concurrency.owner_round_robin
    def pause(*args, **kwargs):
        m.control(parent, 'pause', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
        yield from original(*args, **kwargs)
    monkeypatch.setattr(concurrency, 'owner_round_robin', pause)
    with snap.scope(): control.reconcile(w.BATCHES)
    assert control.PROCESSES.rows[123]['state'] == 'T'


def test_three_ticks_equivalent_and_parse_bound(scheduler, monkeypatch, tmp_path):
    pending, owned = scheduler
    limits(monkeypatch, render=1, max_active_children=1)
    parents = []
    for i in range(20):
        _, parent, child, _ = managed_candidate(); parents.append(parent)
        identity = m.internal(child)
        for n in range(2):
            sibling = w.BATCHES / f's{i}_{n}'; sibling.mkdir()
            write_json_atomic(sibling / 'draft.json', {'media_internal': {**identity, 'kind': 'delivery'}})
            write_json_atomic(sibling / 'runtime.json', {'status': 'review'})
        for n in range(3): web.save({'id': f'job{i}_{n}', 'batch_id': child.name, 'status': 'completed'})
    legacy = batch('f')
    for key in 'abc': queued(key, 'render', batch=legacy.name)
    m.recover_outbox()
    backup = tmp_path / 'baseline'; shutil.copytree(web.JOBS.parent, backup)
    real_scope, real_read = snap.scope, m.read_json
    counts = Counter()
    parse_totals = {False: [], True: []}
    def counted(file):
        if file.name == 'media-state.json': counts[file.parent.name] += 1
        return real_read(file)
    monkeypatch.setattr(m, 'read_json', counted)
    recover_outbox = m.recover_outbox
    def outbox():
        recover_outbox()
        counts.clear()  # Unchanged read-modify-write outbox reads are outside LOCK.
    monkeypatch.setattr(m, 'recover_outbox', outbox)
    def run(enabled):
        monkeypatch.setattr(snap, 'scope', real_scope if enabled else nullcontext)
        trace = []
        for _ in range(3):
            counts.clear(); web.worker_tick(pending, owned)
            parse_totals[enabled].append(sum(counts[p.name] for p in parents))
            if enabled:
                # Locked tick: one parent snapshot; allow up to three for recovery writes.
                assert all(counts[p.name] <= 3 for p in parents), counts
            trace.append({'dispatch': list(owned), 'jobs': {j['id']: j for j in web.stored_jobs()},
                          'controls': {p.name: control.read(p) for p in w.BATCHES.iterdir()}})
            finish_owned(owned)
        return trace
    disabled = run(False)
    close_logs(owned); owned.clear(); pending.clear()
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    shutil.rmtree(web.JOBS.parent); shutil.copytree(backup, web.JOBS.parent)
    control.PROCESSES.rows.clear()
    enabled = run(True)
    assert all(a > b * 2 for a, b in zip(parse_totals[False], parse_totals[True]))
    def semantic(trace):
        result = copy.deepcopy(trace)
        for tick in result:
            for job in tick['jobs'].values():
                for key in ('started', 'finished', 'pid', 'pgid', 'process_token'):
                    job.pop(key, None)
                command = job.get('command', [])
                job['command'] = [arg for arg in command if not arg.startswith('exactcut_process_token=')]
                if job.get('admission'): job['admission'].pop('at', None)
            for value in tick['controls'].values():
                value.pop('updated_at', None)
                for event in value.get('events', []): event.pop('at', None)
                for child in value.get('children', []):
                    for key in ('pid', 'pgid', 'process_token'): child.pop(key, None)
        return result
    assert semantic(disabled) == semantic(enabled)


def test_batch_response_memo(scheduler, monkeypatch):
    parent, child = batch('p'), batch('c')
    write_json_atomic(child / 'draft.json', {'media_internal': {'parent_batch_id': parent.name}})
    for i in range(4): web.save({'id': str(i), 'batch_id': child.name, 'status': 'completed', 'created': i})
    detail = {'runtime': {'status': 'review'}, 'assets': [{'request_id': 'r'}]}
    monkeypatch.setattr(w, 'detail', lambda bid: copy.deepcopy(detail))
    monkeypatch.setattr(web, 'job_response', lambda job: {'queue_position': 0})
    calls, original = [], m.read_json
    def counted(file):
        if file == child / 'draft.json': calls.append(1)
        return original(file)
    monkeypatch.setattr(m, 'read_json', counted)
    result = web.batch_response(parent.name)
    assert len(calls) == 1
    jobs = [j for j in web.stored_jobs() if material_workers.parent_batch_id(j['batch_id']) == parent.name]
    expected = copy.deepcopy(detail); expected['assets'][0]['timing'] = web.asset_timing('r', jobs)
    job = max(jobs, key=lambda j: j['created'])
    expected['submission'] = {key: job.get(key) for key in ('id', 'action', 'status', 'created', 'request_id')}
    expected['submission']['queue_position'] = 0
    assert result == expected
    calls.clear(); assert web.batch_response(parent.name) == expected
    assert len(calls) == 1


def test_fingerprint_same_size_restored_mtime(tmp_path):
    file = tmp_path / 'job.json'
    file.write_text('{"id":"one"}')
    before = file.stat()
    with snap.scope():
        assert snap.job(file, lambda: read_json(file))['id'] == 'one'
        replacement = tmp_path / 'replacement'
        replacement.write_text('{"id":"two"}')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, file)
        assert file.stat().st_size == before.st_size
        assert file.stat().st_mtime_ns == before.st_mtime_ns
        assert snap.job(file, lambda: read_json(file))['id'] == 'two'
    # Even an unchanged file must be parsed again in the next tick.
    calls = []
    with snap.scope():
        snap.job(file, lambda: calls.append(1) or read_json(file))
    with snap.scope():
        snap.job(file, lambda: calls.append(1) or read_json(file))
    assert calls == [1, 1]


def test_job_parse_once_per_file_per_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'JOBS', tmp_path)
    for i in range(12): write_json_atomic(tmp_path / f'{i}.json', {'id': str(i)})
    loads, calls = web.json.loads, []
    def counted(*args, **kwargs):
        calls.append(1)
        return loads(*args, **kwargs)
    monkeypatch.setattr(web.json, 'loads', counted)
    with snap.scope():
        expected = list(web.stored_jobs())
        for _ in range(4): assert list(web.stored_jobs()) == expected
    assert len(calls) == 12
    with snap.scope(): assert list(web.stored_jobs()) == expected
    assert len(calls) == 24
