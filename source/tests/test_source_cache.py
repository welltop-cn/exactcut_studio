import base64
import fcntl
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from exactcut import source_cache as cache
from exactcut import workflow as w
from exactcut.catalog import refresh
from exactcut.intake import read_excel
from exactcut.models import BatchManifest, CutRequest, TimelineItem
from exactcut.util import write_json_atomic

SOURCE = Path(__file__).resolve().parents[1]
TEMPLATE = SOURCE / 'templates/精确剪辑_FBBM_填写模板.xlsx'
GIB = 1024 ** 3


class _Disk:
    def __init__(self, free):
        self.free = free
        self.total = free
        self.used = 0


def _plant(root, episode, body=b'video'):
    directory = root / '101573'
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / f'{episode}.mp4'
    video.write_bytes(body)
    (directory / f'{episode}.json').write_text(json.dumps({
        'complete': True, 'bytes': len(body), 'fetched_at': '2026-10-08T00:00:00+00:00',
    }))
    return video


def _batch(root, name, payload, control=None):
    path = root / '.web-state' / 'batches' / name
    path.mkdir(parents=True)
    (path / 'confirmed.json').write_text(json.dumps(payload, ensure_ascii=False))
    if control is not None:
        (path / 'control.json').write_text(json.dumps(control))
    return path


def _python(code, env):
    return subprocess.Popen(
        [sys.executable, '-c', code],
        cwd=SOURCE, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _ready(process, token):
    line = process.stdout.readline()
    if token not in line:
        raise AssertionError(process.stderr.read())


def test_ensure_rejects_out_of_range_ids(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(tmp_path / 'sources'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    with pytest.raises(RuntimeError, match='EP999'):
        cache.ensure('101573', ['EP999'])
    with pytest.raises(RuntimeError, match='000000'):
        cache.ensure('000000', ['EP1'])
    with pytest.raises(RuntimeError, match='101049'):
        cache.ensure('101049', ['EP500'])


@pytest.mark.integration
def test_cache_hit_records_reused_local_episodes(tmp_path, monkeypatch):
    source = tmp_path / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(source))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    marker = tmp_path / 'called'
    fetch = tmp_path / 'fetch.py'
    fetch.write_text(f'open({str(marker)!r}, "w").write("called\\n")\n')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {fetch}')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda video: None)
    _plant(source, 'EP1')
    path = tmp_path / 'batch'
    path.mkdir()
    write_json_atomic(path / 'runtime.json', {'status': 'queued', 'events': []})
    project = tmp_path / 'projects' / '101573'
    project.mkdir(parents=True)
    write_json_atomic(project / 'project.json', {'project_id': '101573'})
    manifest = BatchManifest(
        drama_id='101573', drama_title='剧', business_date='20261008', author='张三',
        source_workbook='x.xlsx', requests=[CutRequest(
            request_id='R1', seq=1, output_filename='out.mp4',
            timeline=[TimelineItem(type='episode', episode='EP1', start_ms=0, end_ms=1000)],
        )],
    )
    monkeypatch.setattr('exactcut.media.ffprobe', lambda video: {'format': {'duration': '130'}, 'streams': [{'codec_type': 'video'}]})
    w.download(path, manifest, project)
    assert not marker.exists()
    assert w.runtime(path)['reused_local_episodes'] == ['EP1']
    product = SOURCE / 'exactcut'
    for name in ('workflow.py', 'catalog.py', 'material_download.py'):
        text = (product / name).read_text()
        assert 'Downloads/auto-cut' not in text
        assert '.openclaw' not in text


@pytest.mark.integration
def test_require_space_on_source_root_and_evict_oldest(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(tmp_path / 'sources'))
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    monkeypatch.setattr(w, 'read_excel', read_excel)
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    seen = []
    real = __import__('exactcut.storage', fromlist=['require_space']).require_space

    def spy(path, estimated_bytes=0):
        seen.append(Path(path).resolve())
        return real(path, estimated_bytes)

    monkeypatch.setattr('exactcut.storage.require_space', spy)
    item = w.import_excel({'filename': 'request.xlsx', 'content': base64.b64encode(TEMPLATE.read_bytes()).decode()})
    stored = json.loads((w.folder(item['id']) / 'draft.json').read_text())
    stored['draft']['author'] = '张三'
    write_json_atomic(w.folder(item['id']) / 'draft.json', stored)
    w.start_batch(item['id'], 1)
    assert (tmp_path / 'sources').resolve() in seen

    confirmed = _batch(tmp_path, 'a' * 32, {
        'drama_id': '101573',
        'requests': [{'timeline': [{'type': 'episode', 'episode': 'EP1'}]}],
    })
    confirmed_bytes = (confirmed / 'confirmed.json').read_bytes()
    assert b'sha256' not in confirmed_bytes
    draft = tmp_path / '.web-state' / 'batches' / ('b' * 32)
    draft.mkdir()
    (draft / 'draft.json').write_text(json.dumps({
        'draft': {'drama_id': '101573', 'outputs': [{'plots': [{'episode': 'EP3'}, {'episode': ''}]}]},
    }))
    _batch(tmp_path, 'c' * 32, {
        'drama_id': '101573',
        'requests': [{'timeline': [{'type': 'episode', 'episode': 'EP4'}]}],
    }, {'archived': True})
    _batch(tmp_path, 'd' * 32, {
        'drama_id': '101573',
        'requests': [{'timeline': [{'type': 'episode', 'episode': 'EP5'}]}],
    }, {'paused': True, 'archived': False})
    for episode in ('EP1', 'EP2', 'EP3', 'EP4', 'EP5', 'EP7'):
        _plant(tmp_path / 'sources', episode)
    keep_text = '\n'.join(inspect.getsource(fn) for fn in (
        cache.named_episodes, cache.evict_oldest, cache._add_confirmed, cache._add_timeline_text, cache._add_plots))
    assert 'sha256' not in keep_text

    env = os.environ.copy()
    env.update({
        'EXACTCUT_SOURCE_ROOT': str(tmp_path / 'sources'),
        'EXACTCUT_DATA_ROOT': str(tmp_path),
        'EXACTCUT_ENVIRONMENT': 'test',
        'EXACTCUT_ALLOW_MATERIAL_FETCH': '0',
    })
    holder = _python(
        'from exactcut.source_cache import hold_shared\nimport time\n'
        'hold_shared("101573", "EP7")\nprint("held", flush=True)\ntime.sleep(60)\n',
        env,
    )
    try:
        _ready(holder, 'held')
        monkeypatch.setattr('exactcut.source_cache.shutil.disk_usage', lambda path: _Disk(6 * GIB))
        monkeypatch.setattr('exactcut.storage.shutil.disk_usage', lambda path: _Disk(6 * GIB))
        with pytest.raises(ValueError, match='磁盘空间不足'):
            cache.ensure('101573', ['EP9'])
        root = tmp_path / 'sources' / '101573'
        assert (root / 'EP1.mp4').is_file() and (root / 'EP3.mp4').is_file() and (root / 'EP5.mp4').is_file()
        assert not (root / 'EP2.mp4').exists() and not (root / 'EP2.json').exists()
        assert not (root / 'EP4.mp4').exists() and not (root / 'EP4.json').exists()
        assert (root / 'EP7.mp4').is_file() and (root / 'EP7.json').is_file()
        assert (confirmed / 'confirmed.json').read_bytes() == confirmed_bytes
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)

    cache.evict_oldest()
    assert not (tmp_path / 'sources' / '101573' / 'EP7.mp4').exists()

    sig_root = tmp_path / 'sig-sources'
    script = tmp_path / 'slow_fetch.py'
    script.write_text('import sys, time\nopen(sys.argv[3], "wb").write(b"partial")\ntime.sleep(120)\n')
    sig_env = os.environ.copy()
    sig_env.update({
        'EXACTCUT_SOURCE_ROOT': str(sig_root),
        'EXACTCUT_DATA_ROOT': str(tmp_path / 'sig-data'),
        'EXACTCUT_ENVIRONMENT': 'production',
        'EXACTCUT_ALLOW_MATERIAL_FETCH': '1',
        'EXACTCUT_MATERIAL_FETCH_CMD': f'{sys.executable} {script}',
    })
    fetcher = _python('from exactcut.source_cache import ensure\nensure("101573", ["EP40"])\n', sig_env)
    part = sig_root / '101573' / 'EP40.mp4.part'
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not part.exists():
            if fetcher.poll() is not None:
                raise AssertionError(fetcher.stderr.read())
            time.sleep(0.05)
        assert part.is_file()
        fetcher.send_signal(signal.SIGTERM)
        fetcher.wait(timeout=5)
        assert not part.exists()
    finally:
        if fetcher.poll() is None:
            fetcher.kill()
            fetcher.wait(timeout=5)


def test_episode_lock_releases_on_process_exit(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(tmp_path / 'sources'))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    env = os.environ.copy()
    env.update({
        'EXACTCUT_SOURCE_ROOT': str(tmp_path / 'sources'),
        'EXACTCUT_DATA_ROOT': str(tmp_path),
        'EXACTCUT_ENVIRONMENT': 'test',
    })
    process = _python(
        'from exactcut.source_cache import episode_lock\nimport time\n'
        'with episode_lock("101573", "EP1"):\n'
        '    print("locked", flush=True)\n'
        '    time.sleep(60)\n',
        env,
    )
    try:
        _ready(process, 'locked')
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    lock = tmp_path / 'sources' / '101573' / 'EP1.lock'
    assert lock.exists()
    with lock.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)


@pytest.mark.integration
def test_fingerprint_releases_shared_lock(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    _plant(root, 'EP1')
    project = tmp_path / 'projects' / '101573'
    project.mkdir(parents=True)
    (project / 'project.json').write_text(json.dumps({'project_id': '101573'}))
    from exactcut.renderer import source_fingerprints
    source_fingerprints(project, [{'type': 'episode', 'episode': 'EP1'}])
    assert ('101573', 'EP1') not in cache._HELD
    env = os.environ.copy()
    env.update({
        'EXACTCUT_SOURCE_ROOT': str(root),
        'EXACTCUT_DATA_ROOT': str(tmp_path),
        'EXACTCUT_ENVIRONMENT': 'test',
    })
    lock = root / '101573' / 'EP1.lock'
    process = _python(
        'import fcntl\nfrom pathlib import Path\n'
        f'handle = Path({str(lock)!r}).open("a+")\n'
        'fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
        'print("free", flush=True)\n',
        env,
    )
    try:
        _ready(process, 'free')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_low_space_does_not_start_fetch(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    marker = tmp_path / 'called'
    fetch = tmp_path / 'fetch.py'
    fetch.write_text(f'open({str(marker)!r}, "w").write("called\\n")\n')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {fetch}')
    monkeypatch.setattr('exactcut.source_cache.shutil.disk_usage', lambda path: _Disk(6 * GIB))
    monkeypatch.setattr('exactcut.storage.shutil.disk_usage', lambda path: _Disk(6 * GIB))
    with pytest.raises(ValueError, match='磁盘空间不足'):
        cache.ensure('101573', ['EP1'])
    assert not marker.exists()
    assert not (root / '101573' / 'EP1.mp4.part').exists()


def _fetch_env(monkeypatch, root, tmp_path):
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')


@pytest.mark.integration
def test_growing_oss_temp_file_counts_as_progress(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    _fetch_env(monkeypatch, root, tmp_path)
    monkeypatch.setattr(cache, 'FETCH_IDLE_TIMEOUT', 0.2)
    script = tmp_path / 'grow.py'
    script.write_text(
        'import os, sys, time\n'
        'dest = sys.argv[3]\n'
        'tmp = dest + ".tmp-abcdefghijkl"\n'
        'for _ in range(6):\n'
        '    with open(tmp, "ab") as handle:\n'
        '        handle.write(b"x" * 1000)\n'
        '        handle.flush()\n'
        '        os.fsync(handle.fileno())\n'
        '    time.sleep(0.05)\n'
        'os.replace(tmp, dest)\n'
    )
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {script}')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda video: None)
    seen = []
    found = cache.ensure('101573', ['EP1'], on_progress=lambda episode, received, total: seen.append((episode, received, total)))
    assert found['EP1'].read_bytes() == b'x' * 6000
    assert any(episode == 'EP1' and received >= 1000 for episode, received, _ in seen)
    assert not list((root / '101573').glob('EP1.mp4.part.tmp-*'))


def test_download_progress_uses_completed_bytes_from_sidecar(tmp_path):
    part = tmp_path / 'EP1.mp4.part'
    temporary = tmp_path / 'EP1.mp4.part.tmp-abcdefghijkl'
    with temporary.open('wb') as handle:
        handle.seek(100_000_000)
        handle.write(b'x')
    progress = tmp_path / 'EP1.mp4.part.progress.json'
    progress.write_text(json.dumps({'received': 2_097_152, 'total': 129_000_000}))
    _, files, received, total = cache._incoming_download(part)
    assert files == 1
    assert received == 2_097_152
    assert total == 129_000_000


def test_slow_but_progressing_episode_can_finish_after_thirty_minutes(monkeypatch, tmp_path):
    part = tmp_path / 'EP1.mp4.part'
    ticks = iter(range(0, 2200, 2))
    seen = iter(range(1000))

    class Fetch:
        returncode = 0

        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls <= 1000 else 0

    monkeypatch.setattr(cache, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(cache, '_incoming_download', lambda _: ((next(seen),), 1, 1, 2))
    process = Fetch()
    assert cache._wait_fetch(process, part, 'EP1') == 0
    assert process.polls == 1001


def test_stalled_oss_temp_file_times_out_and_keeps_partial(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    _fetch_env(monkeypatch, root, tmp_path)
    monkeypatch.setattr(cache, 'FETCH_IDLE_TIMEOUT', 0.05)
    script = tmp_path / 'stall.py'
    script.write_text(
        'import sys, time\n'
        'open(sys.argv[3] + ".tmp-abcdefghijkl", "wb").write(b"partial")\n'
        'time.sleep(30)\n'
    )
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {script}')
    with pytest.raises(TimeoutError, match='无文件进展'):
        cache.ensure('101573', ['EP1'])
    assert not (root / '101573' / 'EP1.mp4.part').exists()
    assert (root / '101573' / 'EP1.mp4.part.tmp-abcdefghijkl').read_bytes() == b'partial'


@pytest.mark.integration
def test_hung_fetch_clears_part_and_next_episode_continues(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    normal_idle_timeout = cache.FETCH_IDLE_TIMEOUT
    monkeypatch.setattr(cache, 'FETCH_IDLE_TIMEOUT', 0.01)
    hung = tmp_path / 'hung.py'
    hung.write_text('import sys, time\nopen(sys.argv[3], "wb").write(b"partial")\ntime.sleep(60)\n')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {hung}')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda video: None)
    with pytest.raises(TimeoutError, match='无文件进展'):
        cache.ensure('101573', ['EP1'])
    assert not (root / '101573' / 'EP1.mp4.part').exists()
    # Only the hung fetch needs an accelerated timeout; normal process startup
    # must not be judged against the 10 ms failure-injection threshold.
    monkeypatch.setattr(cache, 'FETCH_IDLE_TIMEOUT', normal_idle_timeout)
    fast = tmp_path / 'fast.py'
    fast.write_text('import sys\nopen(sys.argv[3], "wb").write(b"video")\n')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {fast}')
    found = cache.ensure('101573', ['EP2'])
    assert found['EP2'].read_bytes() == b'video'


def test_catalog_refresh_stops_before_any_command(monkeypatch):
    monkeypatch.delenv('EXACTCUT_ALLOW_MATERIAL_FETCH', raising=False)
    text = (SOURCE / 'exactcut' / 'catalog.py').read_text()
    assert 'importlib' not in text
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_MATERIAL_FETCH'):
        refresh()
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} -c "raise SystemExit(99)"')
    with pytest.raises(RuntimeError, match='catalog.json'):
        refresh()
