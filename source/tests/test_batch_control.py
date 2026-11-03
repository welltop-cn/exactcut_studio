import os
import signal
import subprocess
import sys
import time
import uuid
from unittest.mock import patch
import pytest
from exactcut import batch_control as c
from exactcut.runtime import instance_marker
from exactcut.util import write_json_atomic


def state(tmp_path, status='producing'):
    p = tmp_path / ('a' * 32)
    p.mkdir()
    write_json_atomic(p / 'runtime.json', {'status': status, 'message': 'working', 'events': []})
    return p


def test_archive_preserves_state_and_requires_explicit_resume(tmp_path):
    p = state(tmp_path)
    c.change(p, 'archive')
    assert c.present(p, {'status': 'producing', 'message': ''})['status'] == 'paused'
    with pytest.raises(ValueError, match='取消归档'):
        c.change(p, 'resume')
    with pytest.raises(ValueError, match='归档'):
        c.ensure_editable(p, execution=True)
    c.change(p, 'unarchive')
    assert c.read(p)['paused']
    c.change(p, 'resume')
    assert not c.read(p)['paused']
    assert (p / 'runtime.json').exists()


def test_draft_archive_and_invalid_pause(tmp_path):
    p = state(tmp_path, 'draft')
    with pytest.raises(ValueError):
        c.change(p, 'pause')
    c.change(p, 'archive')
    assert not c.read(p)['paused']
    c.change(p, 'unarchive')
    c.ensure_editable(p)


def test_pid_reuse_is_never_signalled():
    table = c.FakeProcessTable({1: {'pgid': 1, 'state': 'S',
                                    'arguments': ['python', '-X', 'exactcut_process_token=' + 'b'*32]}})
    with patch.object(os, 'killpg') as kill:
        assert not c.signal_identity({'pid': 1, 'pgid': 1, 'process_token': 'a'*32,
                                      'instance': instance_marker()}, signal.SIGSTOP, table)
        kill.assert_not_called()


def test_resume_waits_for_other_running_task_under_explicit_emergency_total(tmp_path,monkeypatch):
    from exactcut import concurrency
    monkeypatch.setattr(concurrency,'load_config',lambda:{'concurrency':{'max_active_children':1}})
    paused = state(tmp_path)
    other = tmp_path / ('b' * 32)
    other.mkdir()
    write_json_atomic(other / 'runtime.json', {'status': 'producing', 'message': 'working', 'events': []})
    child = {'pid': 123, 'pgid': 123, 'process_token': 'a'*32, 'lane': 'encode', 'request_id': None,
             'instance': instance_marker()}
    write_json_atomic(paused / 'control.json', {'paused': False, 'archived': False, 'children': [child]})
    write_json_atomic(other / 'control.json', {'paused': False, 'archived': False, 'children': [{**child, 'pid': 456, 'pgid': 456}]})
    args = ['python', '-X', 'exactcut_process_token=' + 'a'*32]
    busy = c.FakeProcessTable({123: {'pgid': 123, 'arguments': args, 'state': 'T'},
                               456: {'pgid': 456, 'arguments': args, 'state': 'S'}})
    with patch.object(os, 'killpg') as kill:
        assert c.reconcile(tmp_path, busy)
        kill.assert_not_called()
    alone = c.FakeProcessTable({123: {'pgid': 123, 'arguments': args, 'state': 'T'}})
    with patch.object(os, 'killpg') as kill:
        assert c.reconcile(tmp_path, alone)
        kill.assert_called_once_with(123, signal.SIGCONT)


def test_actual_process_tree_suspends_and_continues(tmp_path):
    p = state(tmp_path)
    counter = tmp_path / 'ticks'
    script = "import time; from pathlib import Path; p=Path(%r)\nwhile True:\n p.open('a').write('x')\n time.sleep(.03)" % str(counter)
    token = uuid.uuid4().hex
    proc = subprocess.Popen([sys.executable, '-X', 'exactcut_process_token=' + token, '-c', script], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not counter.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        assert counter.exists()
        identity = c.identify(proc, lane='encode', process_token=token)
        assert c.child_state(identity) not in {None, 'Z'}
        c.remember_child(p, identity)
        c.signal_children(p, signal.SIGSTOP)
        before = counter.stat().st_size
        time.sleep(.15)
        assert counter.stat().st_size == before
        assert c.read(p)['suspended'][0]['process_token'] == token
        c.signal_children(p, signal.SIGCONT)
        time.sleep(.15)
        assert counter.stat().st_size > before
    finally:
        os.killpg(proc.pid, signal.SIGCONT)
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)


def test_reconcile_prunes_dead_children_but_preserves_foreign_records(tmp_path):
    p = state(tmp_path)
    dead = {'pid': 12, 'pgid': 12, 'process_token': 'a'*32, 'instance': instance_marker()}
    foreign = {**dead, 'pid': 99, 'pgid': 99, 'instance': 'foreign'}
    write_json_atomic(p/'control.json', {'children': [dead, foreign]})
    assert not c.reconcile(tmp_path, c.FakeProcessTable({}))
    assert c.read(p)['children'] == [foreign]


@pytest.mark.parametrize('request_id', [None, 'r1'])
def test_legacy_pause_and_archive_work_while_production_stage_holds_gate(tmp_path, request_id):
    # workflow.execution() holds the media gate for a whole legacy stage
    # (exclusive for batch stages, shared for one request's render/upload/poll).
    from exactcut.cutting_access import gate
    p = state(tmp_path)
    with gate(p, request_id):
        c.change(p, 'pause')
        assert c.read(p)['paused']
        c.change(p, 'resume')
        c.change(p, 'archive')
        assert c.read(p)['archived'] and c.read(p)['paused']
        c.change(p, 'unarchive')
