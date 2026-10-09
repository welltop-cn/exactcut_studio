import importlib.util
import json
from pathlib import Path


def test_episode_download_reports_sdk_progress_and_cleans_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(tmp_path / 'empty.env'))
    script = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'
    spec = importlib.util.spec_from_file_location('material_hub_progress_test', script)
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)

    store = hub.OssStore.__new__(hub.OssStore)
    store.target = object()
    path = tmp_path / 'EP1.mp4.part'
    sidecar = tmp_path / 'EP1.mp4.part.progress.json'

    def fake_download(bucket, key, filename, **kwargs):
        assert bucket is store.target
        assert key == 'example/EP1.mp4'
        assert filename == str(path)
        assert kwargs['num_threads'] == 4
        kwargs['progress_callback'](2_097_152, 10_000_000)
        assert json.loads(sidecar.read_text()) == {'received': 2_097_152, 'total': 10_000_000}
        path.write_bytes(b'complete')

    monkeypatch.setattr(hub.oss2, 'resumable_download', fake_download)
    store.download('example/EP1.mp4', path)
    assert path.read_bytes() == b'complete'
    assert not sidecar.exists()


def test_episode_download_retries_broken_stream_without_losing_resume_path(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(tmp_path / 'empty.env'))
    script = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'
    spec = importlib.util.spec_from_file_location('material_hub_retry_test', script)
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)

    store = hub.OssStore.__new__(hub.OssStore)
    store.target = object()
    path = tmp_path / 'EP1.mp4.part'
    attempts = []

    def flaky_download(bucket, key, filename, **kwargs):
        attempts.append(filename)
        if len(attempts) == 1:
            path.with_name(path.name + '.tmp-abcdefghijkl').write_bytes(b'partial')
            raise hub.requests.exceptions.ChunkedEncodingError('broken stream')
        assert path.with_name(path.name + '.tmp-abcdefghijkl').read_bytes() == b'partial'
        path.write_bytes(b'complete')

    monkeypatch.setattr(hub.oss2, 'resumable_download', flaky_download)
    monkeypatch.setattr(hub.time, 'sleep', lambda _: None)
    store.download('example/EP1.mp4', path)
    assert attempts == [str(path), str(path)]
    assert path.read_bytes() == b'complete'


def test_episode_download_survives_three_separate_connection_drops(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(tmp_path / 'empty.env'))
    script = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'
    spec = importlib.util.spec_from_file_location('material_hub_long_retry_test', script)
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)
    store = hub.OssStore.__new__(hub.OssStore)
    store.target = object()
    path = tmp_path / 'EP1.mp4.part'
    attempts = []

    def flaky_download(bucket, key, filename, **kwargs):
        attempts.append(filename)
        if len(attempts) <= 3:
            raise hub.requests.exceptions.ChunkedEncodingError('broken stream')
        path.write_bytes(b'complete')

    monkeypatch.setattr(hub.oss2, 'resumable_download', flaky_download)
    monkeypatch.setattr(hub.time, 'sleep', lambda _: None)
    store.download('example/EP1.mp4', path)
    assert len(attempts) == 4
    assert path.read_bytes() == b'complete'


def test_oss_buckets_do_not_inherit_host_proxy_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(tmp_path / 'empty.env'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('ALIYUN_OSS_ACCESS_KEY_ID', 'test-id')
    monkeypatch.setenv('ALIYUN_OSS_ACCESS_KEY_SECRET', 'test-secret')
    monkeypatch.setenv('ALIYUN_OSS_ENDPOINT', 'https://oss-us-west-1.aliyuncs.com')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    script = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'
    spec = importlib.util.spec_from_file_location('material_hub_proxy_test', script)
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)

    store = hub.OssStore()
    for bucket in (store.target, store.source):
        session = bucket.session.session
        assert session.proxies == {}
        assert not session.trust_env
        settings = session.merge_environment_settings('https://oss-us-west-1.aliyuncs.com', {}, None, None, None)
        assert settings['proxies'] == {}


def test_development_oss_can_use_local_proxy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(tmp_path / 'empty.env'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    monkeypatch.setenv('ALIYUN_OSS_ACCESS_KEY_ID', 'test-id')
    monkeypatch.setenv('ALIYUN_OSS_ACCESS_KEY_SECRET', 'test-secret')
    monkeypatch.setenv('ALIYUN_OSS_ENDPOINT', 'https://oss-us-west-1.aliyuncs.com')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    script = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'
    spec = importlib.util.spec_from_file_location('material_hub_development_proxy_test', script)
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)

    store = hub.OssStore()
    for bucket in (store.target, store.source):
        session = bucket.session.session
        assert session.trust_env
        settings = session.merge_environment_settings('https://oss-us-west-1.aliyuncs.com', {}, None, None, None)
        assert settings['proxies']['https'] == 'http://127.0.0.1:7890'
