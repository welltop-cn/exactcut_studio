import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch
import pytest
from exactcut import batch_control as c
from exactcut.util import write_json_atomic


def state(tmp_path, status='producing'):
    p = tmp_path / ('a' * 32)
    p.mkdir()
    write_json_atomic(p / 'runtime.json', {'status': status, 'message': 'working', 'events': []})
    return p


def test_archive_preserves_state_and_requires_explicit_resume(tmp_path):
    p = state(tmp_path)
    with patch.object(c, 'roots', return_value={}):
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
    with patch.object(c, 'roots', return_value={}):
        with pytest.raises(ValueError):
            c.change(p, 'pause')
        c.change(p, 'archive')
        assert not c.read(p)['paused']
        c.change(p, 'unarchive')
        c.ensure_editable(p)


def test_pid_reuse_is_never_signalled():
    with patch.object(os, 'kill') as kill:
        assert not c._signal({'pid': 1, 'born': 'old'}, signal.SIGSTOP, {1: {'born': 'new'}})
        kill.assert_not_called()


def test_resume_waits_for_other_running_task(tmp_path):
    p = state(tmp_path)
    stopped = {'pid': 123, 'born': 'test', 'stat': 'T'}
    running = {'pid': 456, 'born': 'test', 'stat': 'S'}
    with patch.object(c, 'roots', return_value={p.name: stopped, 'b'*32: running}), patch.object(c, 'resume_tree') as resume:
        assert c.reconcile(tmp_path)
        resume.assert_not_called()
    with patch.object(c, 'roots', return_value={p.name: stopped}), patch.object(c, 'resume_tree') as resume:
        assert c.reconcile(tmp_path)
        resume.assert_called_once_with(p, stopped)


def test_actual_process_tree_suspends_and_continues(tmp_path):
    p = state(tmp_path)
    counter = tmp_path / 'ticks'
    child = "import time; from pathlib import Path; p=Path(%r);\nwhile True:\n with p.open('a') as f: f.write('x')\n time.sleep(.03)" % str(counter)
    parent = "import subprocess,sys; subprocess.run([sys.executable,'-c',%r])" % child
    proc = subprocess.Popen([sys.executable, '-c', parent], start_new_session=True)
    try:
        deadline = time.monotonic()+5
        while not counter.exists() and time.monotonic()<deadline:
            time.sleep(.03)
        assert counter.exists()
        root = c.processes()[proc.pid]
        c.suspend(p, root)
        before = counter.stat().st_size
        time.sleep(.15)
        assert counter.stat().st_size == before
        assert len(c.read(p)['suspended']) >= 2
        c.resume_tree(p, root)
        time.sleep(.15)
        assert counter.stat().st_size > before
    finally:
        os.killpg(proc.pid, signal.SIGCONT)
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
