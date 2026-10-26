from types import SimpleNamespace

import pytest

from exactcut import dev_fetch


@pytest.mark.parametrize('exists', [True, False])
def test_download_only_existing_objects(monkeypatch, tmp_path, exists):
    calls = []
    store = SimpleNamespace(exists=lambda key: exists,
                            download=lambda key, dest: calls.append((key, dest)))
    hub = SimpleNamespace(OssStore=lambda: store)
    monkeypatch.setattr(dev_fetch, 'environment', lambda: 'development')
    monkeypatch.setattr(dev_fetch, 'require_material_fetch', lambda: None)
    monkeypatch.setattr(dev_fetch, 'load_env_file', lambda path: None)
    monkeypatch.setenv('EXACTCUT_DEV_FETCH_ENV', str(tmp_path / 'credentials'))
    monkeypatch.setattr(dev_fetch.sys, 'argv', ['dev_fetch', '101607', 'EP1', str(tmp_path / 'EP1.mp4')])
    monkeypatch.setattr(dev_fetch.importlib.util, 'spec_from_file_location',
                        lambda *args: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None)))
    monkeypatch.setattr(dev_fetch.importlib.util, 'module_from_spec', lambda spec: hub)
    if exists:
        dev_fetch.main()
        assert calls == [('assets/101607/video/EP1.mp4', tmp_path / 'EP1.mp4')]
    else:
        with pytest.raises(RuntimeError, match='不存在'):
            dev_fetch.main()
        assert calls == []


def test_refuses_non_development(monkeypatch):
    monkeypatch.setattr(dev_fetch, 'environment', lambda: 'production')
    with pytest.raises(RuntimeError, match='本地开发'):
        dev_fetch.main()
