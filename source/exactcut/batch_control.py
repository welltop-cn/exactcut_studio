"""Persistent batch controls with portable, per-launch process identities."""
from __future__ import annotations
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from .util import read_json, write_json_atomic, now_iso
from .runtime import instance_marker
from . import diagnostics as diag
from . import lock_timing, tick_snapshot

LOCK = lock_timing.TimedLock(threading.RLock(), 'batch_control')
ACTIVE = {'queued', 'downloading', 'indexing', 'producing', 'publishing'}


def read(path, *, fresh=False):
    file = path / 'control.json'
    value = read_json(file) if file.exists() else {'paused': False, 'archived': False, 'suspended': []}
    from . import material_versions as m
    identity = m.internal(path) if fresh else tick_snapshot.internal(path)
    if identity:
        parent = path.parent / identity['parent_batch_id']
        state = m.read(parent) if fresh else tick_snapshot.parent_state(parent)
        stale = state['deleted_at'] or state['assets'][identity['asset_id']]['deleted_at']
        if identity['kind'] == 'candidate':
            version = state['versions'][identity['version_id']]
            # A failed candidate must finish unwinding and release its resident
            # slot. Admission/commit fences already reject another attempt.
            stale = stale or version['child_id'] != path.name or version['status'] == 'superseded'
        value['paused'] = bool(state['control']['paused'] or stale)
        value['archived'] = bool(state['control']['archived'])
    return value


def write(path, control, message=None):
    control['updated_at'] = now_iso()
    if message:
        control.setdefault('events', []).append({'at': control['updated_at'], 'message': message})
    write_json_atomic(path / 'control.json', control)
    if message:
        diag.event('batch.control_changed', batch_id=path.name, paused=control.get('paused'),
                   archived=control.get('archived'), reason=message)


class PsProcessTable:
    def inspect(self, pid):
        if not isinstance(pid, int) or pid <= 0:
            return None
        try:
            result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'pgid=',
                                     '-o', 'stat=', '-o', 'command='],
                                    capture_output=True, text=True, timeout=5,
                                    env={**os.environ, 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('无法核验进程身份；请检查 ps 是否可用') from exc
        if result.returncode == 1 and not result.stdout.strip():
            return None
        if result.returncode:
            raise RuntimeError('无法核验进程身份：ps 查询失败')
        try:
            group, state, command = result.stdout.strip().split(None, 2)
            return {'pgid': int(group), 'state': state[0], 'arguments': command.split()}
        except (ValueError, IndexError) as exc:
            raise RuntimeError('无法核验进程身份：ps 返回格式无效') from exc

    def find(self, token):
        """Recover the launch window before the parent persisted the child's PID."""
        if not valid_token(token):
            return None
        try:
            result = subprocess.run(['ps', '-ww', '-A', '-o', 'pid=', '-o', 'pgid=',
                                     '-o', 'stat=', '-o', 'command='],
                                    capture_output=True, text=True, timeout=5,
                                    env={**os.environ, 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('无法核验进程身份；请检查 ps 是否可用') from exc
        if result.returncode:
            raise RuntimeError('无法核验进程身份：ps 查询失败')
        for line in result.stdout.splitlines():
            try:
                pid, group, state, command = line.strip().split(None, 3)
                arguments = command.split()
                if int(pid) == int(group) and state[0] != 'Z' and has_token(arguments, token):
                    return {'pid': int(pid), 'pgid': int(group)}
            except (ValueError, IndexError):
                continue
        return None


class FakeProcessTable:
    def __init__(self, rows):
        self.rows = rows

    def inspect(self, pid):
        return self.rows.get(pid)

    def find(self, token):
        for pid, row in self.rows.items():
            if row and row['pgid'] == pid and row['state'] != 'Z' and has_token(row['arguments'], token):
                return {'pid': pid, 'pgid': pid}
        return None


PROCESSES = PsProcessTable()


def valid_token(token):
    return isinstance(token, str) and re.fullmatch(r'[a-f0-9]{32}', token) is not None


def has_token(arguments, token):
    marker = 'exactcut_process_token=' + token
    return any(arguments[i:i+2] == ['-X', marker] for i in range(len(arguments)-1))


def belongs(job):
    return job.get('instance') == instance_marker()


def identify(process, *, process_token=None, lane=None, request_id=None):
    pid = getattr(process, 'pid', None)
    if not isinstance(pid, int) or pid <= 0 or not valid_token(process_token):
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if pgid != pid:
        return None
    return {'pid': pid, 'pgid': pgid, 'process_token': process_token,
            'lane': lane, 'request_id': request_id, 'instance': instance_marker()}


def remember_child(path, identity):
    if not path.is_dir() or not valid_token(identity.get('process_token')):
        return
    control = read(path, fresh=True)
    children = [item for item in control.get('children', []) if item.get('pid') != identity['pid']]
    stored = {key: identity.get(key) for key in ('pid', 'pgid', 'process_token', 'lane', 'request_id')}
    stored['instance'] = identity.get('instance') or instance_marker()
    children.append(stored)
    control['children'] = children
    write(path, control)


def signal_identity(identity, sig, table=None):
    """Signal one recorded process group. A reused PID or another data root is left alone."""
    table = PROCESSES if table is None else table
    if child_state(identity, table) in {None, 'Z'}:
        return False
    try:
        os.killpg(identity.get('pgid') or identity['pid'], sig)
        diag.event('process.signalled', child_pid=identity['pid'], signal=int(sig),
                   request_id=identity.get('request_id'), lane=identity.get('lane'))
        return True
    except ProcessLookupError:
        return False


def _signal(identity, sig, table=None):
    return signal_identity(identity, sig, table)


def recorded_children(path):
    control = read(path)
    return [item for item in control.get('children', []) if valid_token(item.get('process_token'))]


def own_live_children(path, table=None):
    table = PROCESSES if table is None else table
    return [
        child for child in recorded_children(path)
        if belongs(child) and child_state(child, table) not in {None, 'Z'}
    ]


def suspended_seconds(path):
    """Elapsed SIGSTOP time for this executor, including a queued resume."""
    file = path / 'control.json'
    control = read_json(file) if file.exists() else {}
    token = sys._xoptions.get('exactcut_process_token')
    for child in control.get('children', []):
        if child.get('pid') == os.getpid() and child.get('process_token') == token:
            started = child.get('suspended_at')
            return child.get('suspended_seconds', 0.0) + (max(0.0, time.monotonic()-started) if started is not None else 0.0)
    return 0.0


def _signal_controlled(path, control, child, sig, table=None):
    """Persist the pause clock before waking an executor that reads it."""
    state = child_state(child, table)
    if state in {None, 'Z'}:
        return False
    stored = next(item for item in control.get('children', [])
                  if (item.get('pid'), item.get('process_token')) == (child['pid'], child['process_token']))
    if sig == signal.SIGSTOP:
        if not signal_identity(child, sig, table): return False
        stored.setdefault('suspended_at', time.monotonic())
        write(path, control)
        return True
    if sig == signal.SIGCONT:
        started = stored.pop('suspended_at', None)
        if started is not None:
            stored['suspended_seconds'] = stored.get('suspended_seconds', 0.0) + max(0.0, time.monotonic()-started)
        write(path, control)
    return signal_identity(child, sig, table)


def signal_children(path, sig, table=None):
    control = read(path, fresh=True)
    signaled = []
    for child in control.get('children', []):
        if valid_token(child.get('process_token')) and _signal_controlled(path, control, child, sig, table):
            signaled.append(child)
    if sig == signal.SIGSTOP:
        control = read(path, fresh=True)
        control['suspended'] = signaled
        write(path, control)
    elif sig == signal.SIGCONT:
        control = read(path, fresh=True)
        control['suspended'] = []
        write(path, control, '已继续执行')
    return signaled


def child_state(child, table=None):
    table = PROCESSES if table is None else table
    pid, token = child.get('pid'), child.get('process_token')
    if not belongs(child) or not isinstance(pid, int) or pid <= 0 or not valid_token(token):
        return None
    if child.get('pgid') != pid:
        return None
    live = table.inspect(pid)
    if not live or live['pgid'] != pid:
        return None
    arguments = live['arguments']
    if not has_token(arguments, token):
        return None
    return live['state']


def resident_identity(job, process=None):
    """Count physical roots separately, resolving the launch window before PID save."""
    token = job.get('process_token') or job['id']
    pid = job.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        pid = getattr(process, 'pid', None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        found = PROCESSES.find(token) if valid_token(token) else None
        pid = found['pid'] if found else None
    return pid, token


PAUSE_MESSAGE = '已请求暂停：制作进度保留'
PAUSE_CUTTING_ERROR = '剪切/打码进行中，完成后再暂停'
ARCHIVE_MESSAGE = '已归档：制作任务暂停并保留进度；剪切/打码任务已取消，不保留进度'


def cutting_message(bid, *, has_preview=False):
    """Summarize active cut/mosaic work for display without changing project status."""
    from . import mosaic_queue as queue
    statuses = {queue.report(item)['status'] for item in queue.load()['tasks']
                if item['batch_id'] == bid}
    if statuses & {'running', 'cancelling'}:
        message = '剪切/打码处理中'
    elif 'queued' in statuses:
        message = '剪切/打码排队中'
    else:
        return ''
    return message + ('；已有成片仍可播放' if has_preview else '')


def cutting_active(bid):
    """Queued/running cut or mosaic tasks; pause waits for them, archive cancels them."""
    return bool(cutting_message(bid))


def change(path, action):
    diag.event('batch.control_requested', batch_id=path.name, action=action)
    from .mosaic_render import cancel_batch
    # Legacy production holds the media gate throughout a stage. Controls must
    # still reach it; completed edit results do not freeze project controls.
    with LOCK:
        control = read(path, fresh=True)
        state = read_json(path / 'runtime.json')
        if action == 'pause':
            if cutting_active(path.name):
                raise ValueError(PAUSE_CUTTING_ERROR)
            if state['status'] not in ACTIVE and not recorded_children(path):
                raise ValueError('此批次当前没有正在执行或排队的任务')
            control['paused'] = True
            write(path, control, PAUSE_MESSAGE)
            signal_children(path, signal.SIGSTOP)
        elif action == 'resume':
            if control.get('archived'):
                raise ValueError('请先取消归档，再继续执行')
            if not control.get('paused'):
                raise ValueError('此批次当前未暂停')
            control['paused'] = False
            write(path, control, '已请求继续，等待执行位置')
        elif action == 'archive':
            cancel_batch(path.name)
            control['archived'] = True
            control['archived_at'] = now_iso()
            if state['status'] in ACTIVE or recorded_children(path):
                control['paused'] = True
            write(path, control, ARCHIVE_MESSAGE)
            signal_children(path, signal.SIGSTOP)
        elif action == 'unarchive':
            control['archived'] = False
            write(path, control, '已取消归档，暂停的任务需点击继续执行')
        else:
            raise ValueError('未知批次操作')
        return read(path, fresh=True)


def ensure_editable(path, *, execution=False):
    control = read(path, fresh=True)
    if control.get('archived'):
        raise ValueError('批次已归档，请先取消归档')
    if execution and (control.get('paused') or control.get('suspended')):
        raise ValueError('批次已暂停，请使用继续执行，避免重复创建任务')


def present(path, state):
    control = read(path)
    data = dict(state)
    data['archived'] = bool(control.get('archived'))
    data['paused'] = bool(control.get('paused'))
    data['underlying_status'] = state['status']
    if control.get('paused') and state['status'] in ACTIVE:
        data['status'] = 'paused'
        data['message'] = '已暂停，当前进度已保留。点击继续执行可恢复。'
    elif control.get('suspended') and state['status'] in ACTIVE:
        data['status'] = 'resume_queued'
        data['message'] = '等待继续执行；前面的运行任务完成或暂停后自动恢复。'
    if data['archived']:
        data['message'] = '已归档。' + data.get('message', '')
    data['events'] = sorted([*state.get('events', []), *control.get('events', [])], key=lambda e: e['at'])
    data['updated_at'] = max(state.get('updated_at', ''), control.get('updated_at', ''))
    return data


def reconcile(batch_root, table=None, *, external_active=None, cursor_file=None):
    """Resume stopped executors only within their lane and batch limits."""
    from .concurrency import lane, settings, owner_round_robin, scope, scope_blocked
    from .accounts import owner
    table = PROCESSES if table is None else table
    limits = settings()
    cursor_file = cursor_file or batch_root.parent / 'scheduler.json'
    with LOCK:
        counts = {key: (external_active or {}).get(key, 0) for key in ('fetch', 'render', 'upload', 'poll')}
        active_batches = set()
        waiting = []
        resident_identities = set()
        for path in sorted(batch_root.glob('*')):
            if not path.is_dir() or not (path / 'runtime.json').exists():
                continue
            control = read(path)
            stale = set()
            for child in recorded_children(path):
                if not belongs(child):
                    continue
                state = child_state(child, table)
                if state in {None, 'Z'}:
                    stale.add((child['pid'], child['process_token']))
                    continue
                if (control.get('paused') or control.get('archived')) and state != 'T':
                    if _signal_controlled(path, control, child, signal.SIGSTOP, table):
                        state = 'T'
                identity = (child['pid'], child['process_token'])
                if identity not in resident_identities:
                    counts[lane(child)] += 1
                    resident_identities.add(identity)
                if state != 'T':
                    active_batches.add(scope(child, path.name))
                elif not control.get('paused') and not control.get('archived'):
                    waiting.append((path, child))
            if stale:
                for key in ('children', 'suspended'):
                    control[key] = [child for child in control.get(key, [])
                                    if (child.get('pid'), child.get('process_token')) not in stale]
                write(path, control)
        for batch_owner, (path, child) in owner_round_robin(waiting, lambda entry: owner(entry[0]), cursor_file):
            resource = lane(child)
            candidate = scope(child, path.name)
            if scope_blocked(candidate, active_batches) or counts[resource] > limits[resource]:
                continue
            total = limits['max_active_children'] if limits['enabled'] else 1
            if sum(counts.values()) > total:
                continue
            control = read(path, fresh=True)
            if control.get('paused') or control.get('archived'):
                continue
            if _signal_controlled(path, control, child, signal.SIGCONT, table):
                active_batches.add(candidate)
                control = read(path, fresh=True)
                control['suspended'] = [item for item in control.get('suspended', [])
                                        if item.get('process_token') != child['process_token']]
                write(path, control, '已继续执行')
                write_json_atomic(cursor_file, {'owner': batch_owner})
        return bool(sum(counts.values()))
