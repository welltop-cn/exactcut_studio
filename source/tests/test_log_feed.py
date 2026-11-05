"""日志复制器的离线回归；临时目录运行，不需要 root，不执行 chown。"""
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[2] / f'monitoring/{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load('log_feed')
monitor = load('prod_monitor')


def line(level='error', at='2026-11-05T03:40:00Z', event='render.failed'):
    return (json.dumps({'level': level, 'at': at, 'event': event}, ensure_ascii=False) + '\n').encode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    source, dest, state = [tmp_path / name for name in ('source', 'feed', 'state')]
    source.mkdir()
    # 即使以后误加入属主切换，测试也立即失败，而非实际 chown。
    def forbidden(*args, **kwargs):
        pytest.fail('测试不得执行属主切换')
    monkeypatch.setattr(m.os, 'fchown', forbidden)
    monkeypatch.setattr(m.os, 'chown', forbidden)
    return source, dest, state


def run(env, *extra):
    source, dest, state = env
    return m.main(['--source', str(source), '--dest', str(dest), '--state-dir', str(state),
                   '--owner', '', *extra])


def output(env):
    return b''.join(path.read_bytes() for path in sorted(env[1].glob('events-????????-*.jsonl')))


def cursor(env):
    return json.loads((env[2] / 'state.json').read_text())['cursors']


def test_bootstrap_and_increment(env):
    source, dest, state = env
    path = source / 'events-20261105-app.jsonl'
    path.write_bytes(line())
    assert run(env) == 0
    assert output(env) == b''
    data = line('info') + '坏的 JSON\n'.encode()
    data += b'[]\n{"level":null}\n' + line('error') + line('critical')
    with path.open('ab') as handle:
        handle.write(data)
    assert run(env) == 0
    assert output(env) == line('error') + line('critical')
    assert run(env) == 0
    assert output(env) == line('error') + line('critical')
    with path.open('ab') as handle:
        handle.write(line())
    assert run(env, '--bootstrap') == 0
    assert run(env) == 0
    assert output(env) == line('error') + line('critical')
    assert stat.S_IMODE(dest.stat().st_mode) == 0o750
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / 'state.json').stat().st_mode) == 0o600
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o640 for p in dest.glob('*.jsonl'))


def test_partial_and_partial_baseline(env):
    path = env[0] / 'events-20261105-app.jsonl'
    data = line()
    path.write_bytes(data[:12])
    assert run(env) == 0
    with path.open('ab') as handle:
        handle.write(data[12:] + data[:20])
    assert run(env) == 0
    assert output(env) == b''
    old_offset = cursor(env)[path.name]['offset']
    assert run(env) == 0
    assert cursor(env)[path.name]['offset'] == old_offset
    with path.open('ab') as handle:
        handle.write(data[20:])
    assert run(env) == 0
    assert output(env) == data


def test_rotation_inode_truncation_removal(env):
    assert run(env) == 0
    path = env[0] / 'events-20261105-app.jsonl'
    path.write_bytes(line() * 2)
    assert run(env) == 0
    assert output(env) == line() * 2
    # 新文件从头处理。
    new = env[0] / 'events-20261106-app.jsonl'
    new.write_bytes(line())
    assert run(env) == 0
    # 相同文件名替换 inode 从头处理。
    path.rename(env[0] / 'old')
    path.write_bytes(line() * 2)
    assert run(env) == 0
    path.write_bytes(line())
    assert run(env) == 0
    assert output(env) == line() * 6
    path.unlink()
    assert run(env) == 0
    assert path.name not in cursor(env)
    assert new.name in cursor(env)


def test_dates_and_monitor_integration(env):
    assert run(env) == 0
    events = [line(at='2026-11-05T00:10:00+08:00'), line(at='2026-11-05T03:40:00Z'),
              line(at='无效日期')]
    (env[0] / 'events-20261105-app.jsonl').write_bytes(b''.join(events))
    assert run(env) == 0
    assert (env[1] / 'events-20261104-feed.jsonl').read_bytes() == events[0]
    assert events[1] in (env[1] / 'events-20261105-feed.jsonl').read_bytes()
    today = m.datetime.now(m.timezone.utc).strftime('%Y%m%d')
    assert events[2] in (env[1] / f'events-{today}-feed.jsonl').read_bytes()
    state = {'scan_initialized': True}
    groups = monitor.scan_events(dict(monitor.DEFAULTS, log_dir=str(env[1])), state)
    assert sum(group['count'] for group in groups.values()) == 3
    assert not monitor.scan_events(dict(monitor.DEFAULTS, log_dir=str(env[1])), state)


def test_retention(env):
    assert run(env) == 0
    old = env[1] / 'events-20200101-feed.jsonl'
    boundary = env[1] / 'events-20200102-feed.jsonl'
    other = env[1] / '不要删除.jsonl'
    for path in (old, boundary, other):
        path.write_bytes(b'')
    now = 1800000000
    os.utime(old, (now - 7 * 86400 - 1,) * 2)
    os.utime(boundary, (now - 7 * 86400,) * 2)
    os.utime(other, (0, 0))
    fd = os.open(env[1], os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert m.cleanup(fd, 7, now) == 1
    finally:
        os.close(fd)
    assert not old.exists()
    assert boundary.exists() and other.exists()


def test_round_limit(env):
    assert run(env) == 0
    path = env[0] / 'events-20261105-app.jsonl'
    path.write_bytes(line() * (m.MAX_LINES + 2))
    later = env[0] / 'events-20261106-app.jsonl'
    later.write_bytes(line())
    assert run(env) == 0
    assert output(env).count(b'\n') == m.MAX_LINES
    assert cursor(env)[path.name]['offset'] == len(line()) * m.MAX_LINES
    assert cursor(env)[later.name]['offset'] == 0
    assert run(env) == 0
    assert output(env).count(b'\n') == m.MAX_LINES + 3


def test_source_unchanged_and_symlink_skipped(env):
    assert run(env) == 0
    path = env[0] / 'events-20261105-app.jsonl'
    path.write_bytes(line())
    path.chmod(0o600)
    env[0].chmod(0o700)
    (env[0] / 'events-20261105-link.jsonl').symlink_to(path)
    before = path.stat()
    assert run(env) == 0
    assert output(env) == line()
    assert path.read_bytes() == line()
    assert path.stat().st_mode == before.st_mode
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(env[0].stat().st_mode) == 0o700
    assert 'events-20261105-link.jsonl' not in cursor(env)


def test_dest_symlink_rejected(env, tmp_path, capsys):
    target = tmp_path / 'target'
    target.mkdir()
    target.chmod(0o700)
    env[1].symlink_to(target, target_is_directory=True)
    assert run(env) == 1
    assert '日志复制失败' in capsys.readouterr().err
    assert not list(target.iterdir())
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_output_symlink_rejected_and_cursor_not_committed(env, tmp_path):
    assert run(env) == 0
    before = (env[2] / 'state.json').read_bytes()
    victim = tmp_path / 'victim'
    victim.write_text('保留')
    (env[1] / 'events-20261105-feed.jsonl').symlink_to(victim)
    (env[0] / 'events-20261105-app.jsonl').write_bytes(line())
    assert run(env) == 1
    assert victim.read_text() == '保留'
    assert (env[2] / 'state.json').read_bytes() == before


def test_dry_run_and_corrupt_state(env):
    assert run(env) == 0
    before = (env[2] / 'state.json').read_bytes()
    (env[0] / 'events-20261105-app.jsonl').write_bytes(line())
    assert run(env, '--dry-run') == 0
    assert output(env) == b''
    assert (env[2] / 'state.json').read_bytes() == before
    assert run(env) == 0
    assert output(env) == line()
    (env[2] / 'state.json').write_text('{}')
    assert run(env) == 1


def test_fsync_failure_keeps_cursor(env, monkeypatch):
    assert run(env) == 0
    (env[0] / 'events-20261105-app.jsonl').write_bytes(line())
    before = (env[2] / 'state.json').read_bytes()
    def fail(fd):
        raise OSError('模拟同步失败')
    monkeypatch.setattr(m.os, 'fsync', fail)
    assert run(env) == 1
    assert (env[2] / 'state.json').read_bytes() == before


def test_lock_prevents_overlap(env):
    import fcntl
    assert run(env) == 0
    with (env[2] / 'lock').open('rb') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(env) == 1


def test_raw_bytes_and_fixed_width_date(env):
    assert run(env) == 0
    raw = ' { "level": "ERROR", "at": "0001-01-01T00:00:00Z", "message": "原样保留" }\r\n'.encode()
    (env[0] / 'events-20261105-app.jsonl').write_bytes(raw)
    assert run(env) == 0
    path = env[1] / 'events-00010101-feed.jsonl'
    assert path.read_bytes() == raw
    assert path in list(env[1].glob(m.PATTERN))
