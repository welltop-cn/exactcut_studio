"""Full independent output/work peaks on one or several physical devices."""
from types import SimpleNamespace
from exactcut import storage


def test_three_disks_reject_quarter_output_peak(tmp_path, monkeypatch):
    project, output, temporary = [tmp_path / name for name in ('project', 'output', 'tmp')]
    for path in (project, output, temporary): path.mkdir()
    monkeypatch.setenv('TMPDIR', str(temporary))
    monkeypatch.setattr(storage, 'protect_path', lambda path: None)
    original = type(tmp_path).stat
    devices = {project: 101, output: 102, temporary: 103}
    monkeypatch.setattr(type(tmp_path), 'stat', lambda p, **kw: SimpleNamespace(st_dev=devices[p]) if p in devices else original(p, **kw))
    monkeypatch.setattr(storage, 'policy', lambda: {'reserve_bytes': 10})
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: SimpleNamespace(free=260 if str(p) == str(output) else 10000))
    paths = storage.render_allocations(project, output, 1000)
    assert not storage.reserve('first', paths, tmp_path / 'state/jobs')
    assert paths[str(output)] == 1000


def test_same_disk_reserves_full_output_plus_independent_temporary(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'tmp'))
    monkeypatch.setattr(storage, 'protect_path', lambda path: None)
    monkeypatch.setattr(storage, 'policy', lambda: {'reserve_bytes': 10})
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: SimpleNamespace(free=2010))
    paths = storage.render_allocations(tmp_path / 'project', tmp_path / 'output', 1000)
    assert sum(paths.values()) == 2000
    jobs = tmp_path / 'state/jobs'
    assert storage.reserve('first', paths, jobs)
    assert not storage.reserve('second', paths, jobs)
    storage.release('first', jobs)
    assert storage.reserve('second', paths, jobs)
