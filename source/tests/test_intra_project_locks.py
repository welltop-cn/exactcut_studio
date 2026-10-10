import fcntl
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from exactcut import renderer, source_cache, storage
from exactcut.hook_options import HookOptions
from exactcut.stage_results import ResourceDeferred
from exactcut.util import load_config


@pytest.fixture
def isolated_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(tmp_path / 'sources'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda _: None)
    directory = tmp_path / 'sources/101573'
    directory.mkdir(parents=True)
    return directory


def test_ready_episode_returns_while_render_holds_shared_lock(isolated_sources):
    video = isolated_sources / 'EP1.mp4'
    video.write_bytes(b'validated episode')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with source_cache.shared_for(video):
            future = pool.submit(source_cache.ensure, '101573', ['EP1'])
            try:
                found = future.result(timeout=1)
            except TimeoutError:
                pytest.fail('ready cache hit blocked behind the rendering reader')
            assert found == {'EP1': video}


def test_episode_evicted_during_unlocked_check_is_downloaded_under_lock(isolated_sources, monkeypatch):
    video = isolated_sources / 'EP1.mp4'
    video.write_bytes(b'old episode')
    original_is_file = Path.is_file
    evicted = []
    downloaded = []

    def is_file(path):
        exists = original_is_file(path)
        if path == video and exists and not evicted:
            video.unlink()
            evicted.append(True)
        return exists

    def fetch(argv, drama, episode, dest, progress):
        with (isolated_sources / 'EP1.lock').open('a+') as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        dest.write_bytes(b'new episode')
        downloaded.append(episode)

    monkeypatch.setattr(Path, 'is_file', is_file)
    monkeypatch.setattr(source_cache, '_reserve_for_fetch', lambda: None)
    monkeypatch.setattr(source_cache, '_fetch_argv', lambda: ['fixture-fetch'])
    monkeypatch.setattr(source_cache, '_run_fetch', fetch)
    found = source_cache.ensure('101573', ['EP1'])
    assert found == {'EP1': video}
    assert video.read_bytes() == b'new episode'
    assert downloaded == ['EP1'] and source_cache.reused_episodes() == []


@pytest.mark.parametrize('held_mode', [fcntl.LOCK_SH, fcntl.LOCK_EX])
def test_cold_episode_defers_without_waiting_for_busy_lock(isolated_sources, monkeypatch, held_mode):
    monkeypatch.setattr(source_cache, '_reserve_for_fetch', lambda: pytest.fail('busy episode reserved storage'))
    with (isolated_sources / 'EP1.lock').open('a+') as holder:
        fcntl.flock(holder, held_mode)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(source_cache.ensure, '101573', ['EP1'])
            try:
                with pytest.raises(ResourceDeferred) as deferred:
                    future.result(timeout=1)
                assert deferred.value.not_before > 0
            except TimeoutError:
                pytest.fail('cold episode waited for a busy lock instead of deferring')
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            pool.shutdown(wait=True)


@pytest.fixture
def base_render(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    project = tmp_path / 'projects/101573'
    project.mkdir(parents=True)
    config = load_config()
    config['render']['hook_emphasis'] = 'none'
    timeline = [{'type': 'card', 'text': 'fixture', 'duration_ms': 1000}]
    cuts = []

    def card(text, duration, output, render, work):
        cuts.append(output)
        output.write_bytes(b'validated base')

    def concat(units, output, render, expected):
        output.write_bytes(units[0].read_bytes())

    def overlay(input_video, hook_text, output, render, work, **kwargs):
        output.write_bytes(input_video.read_bytes())
        return None

    monkeypatch.setattr(storage, 'prepare_render', lambda *args: None)
    monkeypatch.setattr(renderer, '_render_card_unit', card)
    monkeypatch.setattr(renderer, '_concat_units', concat)
    monkeypatch.setattr(renderer, 'duration_ms', lambda _: 1000)
    monkeypatch.setattr(renderer, '_apply_hook', overlay)
    monkeypatch.setattr(renderer, 'approved_preview', lambda *args: None)
    monkeypatch.setattr(renderer, 'hook_duration', lambda *args: 1000)

    def render(name, *, options=None, progress=None):
        request = SimpleNamespace(request_id=name, hook_text=name, hook_options=options)
        return renderer.render_request(project, request, timeline, tmp_path / f'{name}.mp4',
                                       config, progress=progress)

    return SimpleNamespace(project=project, root=tmp_path, render=render, cuts=cuts)


def test_same_cut_different_hook_overlays_overlap_and_generate_base_once(base_render, monkeypatch):
    first_entered, second_entered, release = Event(), Event(), Event()

    def overlay(input_video, hook_text, output, render, work, **kwargs):
        if hook_text == 'first':
            first_entered.set()
            assert release.wait(timeout=5)
        else:
            second_entered.set()
        output.write_bytes(input_video.read_bytes())
        return None

    monkeypatch.setattr(renderer, '_apply_hook', overlay)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(base_render.render, 'first')
        try:
            assert first_entered.wait(timeout=2)
            second = pool.submit(base_render.render, 'second')
            assert second_entered.wait(timeout=1), 'second Hook overlay blocked behind first overlay'
        finally:
            release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]
    assert len(base_render.cuts) == 1
    assert results[0]['base_cache_key'] == results[1]['base_cache_key']
    assert [item['base_cache_reused'] for item in results] == [False, True]


def test_frame_sampling_runs_after_base_lock_is_released(base_render, monkeypatch):
    sampled = []

    def sample(project, timeline, work, visible, **kwargs):
        lock_path, = (project / 'cache/render-base').glob('*.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pytest.fail('frame sampling still holds exclusive base lock')
        sampled.append(True)
        return [], None

    monkeypatch.setattr(renderer, 'sample_timeline_frames', sample)
    base_render.render('sampling', options=HookOptions(mode='none'))
    assert sampled == [True]


@pytest.mark.parametrize('cached', [False, True])
def test_cleanup_can_evict_base_without_deleting_active_overlay_input(base_render, monkeypatch, cached):
    if cached:
        base_render.render('warmup')
    inputs = []

    def overlay(input_video, hook_text, output, render, work, **kwargs):
        base, = (base_render.project / 'cache/render-base').glob('*.mp4')
        assert input_video.is_relative_to(work), 'overlay must read its request-owned base copy'
        assert input_video.stat().st_ino != base.stat().st_ino, 'writable hardlinks are forbidden'
        report = storage.report(base_render.root)
        removed = storage.cleanup(base_render.root, token=report['cleanup_token'])
        assert removed['removed'] == 1
        assert not base.exists()
        assert input_video.read_bytes() == b'validated base'
        output.write_bytes(input_video.read_bytes())
        inputs.append(input_video)
        return None

    monkeypatch.setattr(renderer, '_apply_hook', overlay)
    result = base_render.render('cleanup')
    assert result['base_cache_reused'] is cached
    assert len(base_render.cuts) == 1
    assert len(inputs) == 1 and not inputs[0].exists()
