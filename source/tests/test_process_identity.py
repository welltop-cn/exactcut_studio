import os
import signal
import subprocess
from unittest.mock import Mock, patch

import pytest

from exactcut import batch_control as c
from exactcut.runtime import instance_marker
from exactcut.util import write_json_atomic

TOKEN = 'a' * 32


def identity(pid=11, token=TOKEN):
    return {'pid': pid, 'pgid': pid, 'process_token': token, 'instance': instance_marker()}


def row(pid=11, token=TOKEN, state='S'):
    return {'pgid': pid, 'state': state, 'arguments': ['python', '-X', 'exactcut_process_token=' + token]}


def test_pause_stops_each_recorded_child_and_skips_reused_pid(tmp_path):
    path = tmp_path / ('a' * 32)
    path.mkdir()
    write_json_atomic(path / 'runtime.json', {'status': 'producing', 'message': '', 'events': []})
    children = [identity(pid) for pid in (11, 12, 13)]
    write_json_atomic(path / 'control.json', {'paused': False, 'archived': False, 'children': children})
    table = c.FakeProcessTable({11: row(11), 12: row(12), 13: row(13, 'b' * 32)})
    with patch.object(c, 'PROCESSES', table), patch.object(os, 'killpg') as kill:
        c.change(path, 'pause')
        assert sorted(call.args[0] for call in kill.call_args_list) == [11, 12]
        assert all(call.args[1] == signal.SIGSTOP for call in kill.call_args_list)
    assert c.read(path)['paused'] is True


@pytest.mark.parametrize('live', [None, row(token='b' * 32), row(pid=99), row(state='Z')])
def test_unmatched_identity_is_never_signalled(live):
    with patch.object(os, 'killpg') as kill:
        assert not c.signal_identity(identity(), signal.SIGSTOP, c.FakeProcessTable({11: live}))
        kill.assert_not_called()


def test_foreign_instance_and_legacy_record_are_not_signalled():
    table = c.FakeProcessTable({11: row()})
    with patch.object(os, 'killpg') as kill:
        assert not c.signal_identity({**identity(), 'instance': 'foreign'}, signal.SIGSTOP, table)
        assert not c.signal_identity({'pid': 11, 'pgid': 11, 'starttime': 123,
                                     'instance': instance_marker()}, signal.SIGSTOP, table)
        kill.assert_not_called()


def test_ps_reads_explicit_columns_and_does_not_truncate_command(monkeypatch):
    command = 'python -X exactcut_process_token=' + TOKEN + ' -c ' + 'x' * 1200
    run = Mock(return_value=subprocess.CompletedProcess([], 0, ' 11 S+ ' + command + '\n', ''))
    monkeypatch.setattr(c.subprocess, 'run', run)
    actual = c.PsProcessTable().inspect(11)
    assert actual == {'pgid': 11, 'state': 'S', 'arguments': command.split()}
    argv = run.call_args.args[0]
    assert '-ww' in argv and '-p' in argv and '11' in argv
    assert 'pgid=' in argv and 'stat=' in argv and 'command=' in argv
    assert run.call_args.kwargs['env']['LC_ALL'] == 'C'


@pytest.mark.parametrize('failure', [FileNotFoundError(), subprocess.TimeoutExpired('ps', 5)])
def test_ps_failure_does_not_allow_signals(monkeypatch, failure):
    monkeypatch.setattr(c.subprocess, 'run', Mock(side_effect=failure))
    with patch.object(os, 'killpg') as kill, pytest.raises(RuntimeError, match='进程身份'):
        c.signal_identity(identity(), signal.SIGSTOP, c.PsProcessTable())
    kill.assert_not_called()


def test_ps_missing_pid_is_not_an_inspection_error(monkeypatch):
    monkeypatch.setattr(c.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 1, '', '')))
    assert c.PsProcessTable().inspect(11) is None


def test_process_token_must_be_an_interpreter_option():
    table = c.FakeProcessTable({11: {**row(), 'arguments': ['python', '-c', 'exactcut_process_token=' + TOKEN]}})
    assert c.child_state(identity(), table) is None


def test_ps_finds_only_matching_launch_and_process_group(monkeypatch):
    stdout = (' 9 9 S python -X exactcut_process_token=' + 'b'*32 + '\n'
              ' 11 99 S python -X exactcut_process_token=' + TOKEN + '\n'
              ' 12 12 S python -X exactcut_process_token=' + TOKEN + '\n')
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ''))
    monkeypatch.setattr(c.subprocess, 'run', run)
    assert c.PsProcessTable().find(TOKEN) == {'pid': 12, 'pgid': 12}
    assert '-ww' in run.call_args.args[0] and '-A' in run.call_args.args[0]
