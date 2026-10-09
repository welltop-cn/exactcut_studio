from pathlib import Path

from exactcut import material_download as dl


def test_place_file_never_uses_macos_clone(tmp_path, monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError('subprocess.run')

    monkeypatch.setattr(dl.subprocess, 'run', refuse)
    source = tmp_path / 'EP1.mp4'
    source.write_bytes(b'x')
    destination = tmp_path / 'out' / 'EP1.mp4'
    kind = dl.place_file(source, destination)
    assert kind in {'copy', 'reflink'}
    assert destination.read_bytes() == b'x'
    assert destination.stat().st_ino != source.stat().st_ino
    assert calls == []
    text = Path(dl.__file__).read_text()
    assert '/bin/cp' not in text
    assert 'os.link' not in text


def _flag_values(text):
    import re
    values = {}
    for key, value in re.findall(r'(EXACTCUT_[A-Z_]+)\s*[=:]\s*"?([^\s"\n]+)', text):
        values[key] = value.rstrip('\\')
    return values


def test_docker_dev_config_keeps_production_side_effects_disabled():
    root = Path(__file__).resolve().parents[1]
    compose = (root / 'docker-compose.yml').read_text()
    dockerfile = (root / 'Dockerfile').read_text()
    compose_flags = _flag_values(compose)
    assert compose_flags['EXACTCUT_ENVIRONMENT'] == 'development'
    for key in ('EXACTCUT_ALLOW_PUBLISH', 'EXACTCUT_ALLOW_AI', 'EXACTCUT_ALLOW_EXTERNAL_WRITES'):
        assert compose_flags[key] == '0', key
    publish_line = next(line.strip() for line in compose.splitlines() if '8793:8792' in line)
    assert publish_line.startswith('- "127.0.0.1:')
    docker_flags = _flag_values(dockerfile)
    for key in ('EXACTCUT_ALLOW_PUBLISH', 'EXACTCUT_ALLOW_AI',
                'EXACTCUT_ALLOW_MATERIAL_FETCH', 'EXACTCUT_WORKER_ENABLED'):
        assert docker_flags[key] == '0', key


def test_linux_image_contains_the_episode_fetcher_and_portable_tools():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / 'Dockerfile').read_text()
    compose = (root / 'docker-compose.yml').read_text()
    assert 'context: ..' in compose
    assert 'dockerfile: source/Dockerfile' in compose
    assert 'WORKDIR /app/source' in dockerfile
    assert 'COPY integrations/drama-material-hub/scripts /app/integrations/drama-material-hub/scripts' in dockerfile
    assert 'antiword' in dockerfile
    assert 'fonts-dejavu' not in dockerfile  # application owns its fonts


def test_production_compose_has_no_mac_service_or_tunnel_and_is_local_only():
    root = Path(__file__).resolve().parents[1]
    compose = (root / 'docker-compose.production.yml').read_text()
    flags = _flag_values(compose)
    assert flags['EXACTCUT_ENVIRONMENT'] == 'production'
    assert '127.0.0.1:${EXACTCUT_BACKEND_PORT:-18792}:8792' in compose
    assert 'cloudflared' not in compose
    assert 'launchctl' not in compose
    assert 'EXACTCUT_MATERIAL_FETCH_CMD' in compose
    for key in ('EXACTCUT_ALLOW_MATERIAL_FETCH', 'EXACTCUT_ALLOW_PUBLISH', 'EXACTCUT_ALLOW_AI',
                'EXACTCUT_ALLOW_EXTERNAL_WRITES'):
        assert flags[key] == '1'
