import json
from types import SimpleNamespace

import pytest

from exactcut import storage, source_cache
from exactcut.util import load_config


def runtime_config(tmp_path, monkeypatch, **settings):
    path = tmp_path / 'runtime.json'
    path.write_text(json.dumps({'environment': 'production', 'storage': settings}))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_CONFIG_FILE', str(path))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'data'))
    return path


def test_machine_override_uses_central_config_and_manual_config_takes_precedence(tmp_path, monkeypatch):
    runtime_config(tmp_path, monkeypatch, reserve_gib=10)
    assert load_config()['storage']['reserve_gib'] == 10
    explicit = tmp_path / 'manual.json'
    explicit.write_text(json.dumps({'storage': {'reserve_gib': 12}}))
    assert load_config(explicit)['storage']['reserve_gib'] == 12


def test_runtime_config_cannot_be_loaded_by_another_environment(tmp_path, monkeypatch):
    runtime_config(tmp_path, monkeypatch, reserve_gib=10)
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    with pytest.raises(RuntimeError, match='配置环境'):
        load_config()


def test_configured_reserve_is_enforced_and_explained(tmp_path, monkeypatch):
    runtime_config(tmp_path, monkeypatch, reserve_gib=10)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: SimpleNamespace(free=9 * storage.GIB, total=100 * storage.GIB))
    with pytest.raises(ValueError, match='10 GB 预留'):
        storage.require_space(tmp_path)
    assert storage.report(tmp_path)['reserve_bytes'] == 10 * storage.GIB


@pytest.mark.parametrize('value', [-1, 0, True, '10', float('inf')])
def test_invalid_storage_limit_fails_before_render_or_cleanup(tmp_path, monkeypatch, value):
    runtime_config(tmp_path, monkeypatch, reserve_gib=value)
    with pytest.raises(ValueError, match='storage.reserve_gib'):
        storage.require_space(tmp_path)


@pytest.mark.integration
def test_source_cache_quota_stops_download_when_active_material_cannot_be_evicted(tmp_path, monkeypatch):
    runtime_config(tmp_path, monkeypatch, source_cache_gib=1, episode_budget_gib=0.5)
    root = tmp_path / 'data/sources/101460'
    root.mkdir(parents=True)
    video = root / 'EP1.mp4'
    with video.open('wb') as stream:
        stream.truncate(700 * 1024**2)
    video.with_suffix('.json').write_text(json.dumps({'complete': True}))
    monkeypatch.setattr(source_cache, 'named_episodes', lambda: {('101460', 'EP1')})
    monkeypatch.setattr(source_cache.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100 * storage.GIB))
    with pytest.raises(ValueError, match='正片缓存容量不足'):
        source_cache._reserve_for_fetch()
    assert video.exists()
