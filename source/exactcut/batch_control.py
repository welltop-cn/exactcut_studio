"""Persistent batch controls and verified process-tree suspension (macOS/POSIX)."""
from __future__ import annotations
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from .util import read_json, write_json_atomic, now_iso
from .runtime import instance_marker

LOCK = threading.RLock()
ACTIVE = {'queued', 'downloading', 'indexing', 'producing', 'publishing'}


def read(path):
    file = path / 'control.json'
    return read_json(file) if file.exists() else {'paused': False, 'archived': False, 'suspended': []}


def write(path, control, message=None):
    control['updated_at'] = now_iso()
    if message:
        control.setdefault('events', []).append({'at': control['updated_at'], 'message': message})
        control['events'] = control['events'][-50:]
    write_json_atomic(path / 'control.json', control)


def processes():
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,lstart=,stat=,command='], capture_output=True, text=True, check=True, timeout=10)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        rows[pid] = {'pid': pid, 'ppid': ppid, 'born': ' '.join(parts[2:7]), 'stat': parts[7], 'command': parts[8]}
    return rows


def roots(snapshot=None):
    snapshot = processes() if snapshot is None else snapshot
    found = {}
    for row in snapshot.values():
        match = re.search(r'\s-m\s+exactcut\.workflow\s+(produce|publish|preview)\s+([0-9a-f]{32})(?:\s|$)', row['command'])
        executable = Path(row['command'].split()[0]).name.lower()
        if match and executable.startswith('python') and 'Z' not in row['stat'] and instance_marker() in row['command'].split():
            found[match[2]] = row
    return found


def _signal(identity, sig, snapshot=None):
    snapshot = processes() if snapshot is None else snapshot
    live = snapshot.get(identity['pid'])
    if not live or live['born'] != identity['born']:
        return False  # Never signal a recycled PID.
    try:
        os.kill(identity['pid'], sig)
        return True
    except ProcessLookupError:
        return False


def suspend(path, root):
    control = read(path)
    # Stop parent first, then walk its children after it can no longer launch work.
    _signal(root, signal.SIGSTOP)
    identities = {i['pid']: i for i in control.get('suspended', [])}
    identities[root['pid']] = {'pid': root['pid'], 'born': root['born']}
    for _ in range(4):
        snapshot = processes()
        new = [row for row in snapshot.values() if row['ppid'] in identities and row['pid'] not in identities]
        if not new:
            break
        for row in new:
            if _signal(row, signal.SIGSTOP, snapshot):
                identities[row['pid']] = {'pid': row['pid'], 'born': row['born']}
    control['suspended'] = list(identities.values())
    write(path, control)


def resume_tree(path, root):
    control = read(path)
    suspended = control.get('suspended', [])
    # Resume descendants before allowing their parent to advance.
    for item in reversed(suspended):
        _signal(item, signal.SIGCONT)
    _signal(root, signal.SIGCONT)
    control['suspended'] = []
    write(path, control, '已继续执行')


def change(path, action):
    with LOCK:
        control = read(path)
        state = read_json(path / 'runtime.json')
        root = roots().get(path.name)
        if action == 'pause':
            if state['status'] not in ACTIVE and not root:
                raise ValueError('此批次当前没有正在执行或排队的任务')
            control['paused'] = True
            write(path, control, '已请求暂停，保留当前进度')
            if root:
                suspend(path, root)
        elif action == 'resume':
            if control.get('archived'):
                raise ValueError('请先取消归档，再继续执行')
            if not control.get('paused'):
                raise ValueError('此批次当前未暂停')
            control['paused'] = False
            write(path, control, '已请求继续，等待执行位置')
        elif action == 'archive':
            control['archived'] = True
            control['archived_at'] = now_iso()
            if state['status'] in ACTIVE or root:
                control['paused'] = True
            write(path, control, '已归档；正在执行的任务保留进度并暂停')
            if root:
                suspend(path, root)
        elif action == 'unarchive':
            control['archived'] = False
            write(path, control, '已取消归档，暂停的任务需点击继续执行')
        else:
            raise ValueError('未知批次操作')
        return read(path)


def ensure_editable(path, *, execution=False):
    control = read(path)
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
    data['events'] = sorted([*state.get('events', []), *control.get('events', [])], key=lambda e: e['at'])[-200:]
    data['updated_at'] = max(state.get('updated_at', ''), control.get('updated_at', ''))
    return data


def reconcile(batch_root):
    """Pause requested trees; resume at most one tree when the runner is free."""
    with LOCK:
        current = roots()
        for batch_id, root in current.items():
            path = batch_root / batch_id
            if not (path / 'runtime.json').exists():
                continue
            control = read(path)
            if (control.get('paused') or control.get('archived')) and 'T' not in root['stat']:
                suspend(path, root)
        current = roots()
        running = [r for r in current.values() if 'T' not in r['stat']]
        if running:
            return True
        for batch_id, root in current.items():
            path = batch_root / batch_id
            if not (path / 'runtime.json').exists():
                continue
            control = read(path)
            if not control.get('paused') and not control.get('archived'):
                resume_tree(path, root)
                return True
        return False
