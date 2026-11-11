"""A damaged report must not prevent unrelated queued work from running."""
import threading
import os
import subprocess
import sys
from pathlib import Path

import pytest

from exactcut import interactive_queue as queue
from exactcut.util import read_json, write_json_atomic
from test_interactive_queue import state, submit


@pytest.mark.parametrize('location', ['pending', pytest.param('active_after_process_exit', marks=pytest.mark.integration)])
@pytest.mark.parametrize('content', ['{broken', '[]', '{"id":"another-job","status":"queued"}',
                                   '{"id":"00000000000000000000000000000001","status":[]}'])
def test_corrupt_report_does_not_block_other_tasks(state, location, content):
    bad = submit(state, 1)
    if location == 'active_after_process_exit':
        code = "from exactcut import interactive_queue as q;import os;assert q.Dispatcher().claim();os._exit(71)"
        process = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1])}, timeout=10)
        assert process.returncode == 71
        assert [entry['task_id'] for entry in queue.snapshot()['active']] == [bad['task_id']]
    damaged = state / bad['report']
    damaged.write_text(content)
    good = submit(state, 2)
    finished = threading.Event()
    ran = []

    def run(entry, leases):
        ran.append(entry['task_id'])
        path = state / entry['report']
        write_json_atomic(path, {**read_json(path), 'status': 'completed'})
        finished.set()

    dispatcher = queue.Dispatcher(run=run, poll_seconds=.01)
    try:
        assert dispatcher.start()
        assert finished.wait(2), 'one unreadable report blocked an unrelated queued task'
    finally:
        dispatcher.stop(timeout=None)
    assert ran == [good['task_id']]
    assert damaged.read_text() == content
    assert read_json(state / good['report'])['status'] == 'completed'
    saved = queue.snapshot()
    assert [entry['task_id'] for entry in saved['pending'] + saved['active']] == [bad['task_id']]


@pytest.mark.integration
def test_process_death_after_descriptor_commit_recovers_missing_report_once(state):
    (state / 'session-1').mkdir()
    code = '''
import os,sys
from pathlib import Path
from exactcut import interactive_queue as queue
from test_interactive_queue import submit
write=queue.write_json_atomic
def crash(target,value):
    if Path(target).name=='render-'+f'{1:032x}'+'.json':os._exit(72)
    return write(target,value)
queue.write_json_atomic=crash
submit(Path(sys.argv[1]),1)
'''
    source = Path(__file__).parents[1]
    process = subprocess.run([sys.executable, '-c', code, str(state)], env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(source), str(source/'tests')])}, timeout=10)
    assert process.returncode == 72
    descriptor = queue.snapshot()['pending'][0]
    path = state / descriptor['report']
    assert not path.exists()
    finished = threading.Event()
    ran = []

    def run(entry, leases):
        ran.append(entry['task_id'])
        write_json_atomic(path, {**read_json(path), 'status': 'completed'})
        finished.set()

    dispatcher = queue.Dispatcher(run=run, poll_seconds=.01)
    try:
        assert dispatcher.start()
        assert finished.wait(2)
    finally:
        dispatcher.stop(timeout=None)
    assert ran == [descriptor['task_id']]
    assert read_json(path)['status'] == 'completed'
    assert not queue.snapshot()['pending'] and not queue.snapshot()['active']
