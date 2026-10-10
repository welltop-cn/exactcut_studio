import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def ops():
    path = Path(__file__).resolve().parents[1] / 'deploy/linux_ops.py'
    spec = importlib.util.spec_from_file_location('linux_release_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Engine:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.present = False
    def inspect_image(self, image):
        return image, 'a' * 40
    def occupied(self, settings):
        return self.present
    def apply(self, root, release, settings):
        self.calls.append(('apply', release['version']))
        self.present = True
    def stop(self, root, release, settings):
        self.calls.append(('stop', release['version']))
    def remove(self, root, release, settings):
        self.calls.append(('remove', release['version']))
        self.present = False
    def health(self, settings, version, timeout):
        self.calls.append(('health', version))
        if version == self.fail:
            raise RuntimeError('health failed')
        return {'status': 'ok', 'environment': 'production', 'release': version, 'worker_alive': True}


def initialize(ops, root):
    template = Path(__file__).resolve().parents[1] / 'config/production.json'
    ops.initialize(root, template, instance='exactcut-fixture', backend_port=18792,
                   public_port=8792, cpus=1, memory='2g')
    (root / 'config/production.env').write_text('EXACTCUT_ENVIRONMENT=production\n')
    (root / 'config/production.env').chmod(0o600)


def release(ops, root, version):
    folder = root / 'releases' / version
    folder.mkdir(parents=True)
    compose = folder / 'compose.yml'
    compose.write_text('services: {}\n')
    tests = folder / 'tests.log'
    tests.write_text('1 passed\n')
    record = {'version': version, 'commit': 'a' * 40, 'image': 'sha256:' + 'b' * 64,
              'data_schema': {'version': 1, 'readable': [1]}, 'tests': {'exit_code': 0, 'passed': 1},
              'compose_sha256': ops.sha(compose), 'tests_sha256': ops.sha(tests)}
    ops.write(folder / 'release.json', record)
    return record


def test_health_failure_restores_previous_image_without_restoring_database(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'old')
    release(ops, tmp_path, 'new')
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    database = tmp_path / 'data/.web-state/accounts.sqlite3'
    database.parent.mkdir(parents=True)
    database.write_bytes(b'latest business data')
    engine = Engine(fail='new')
    with pytest.raises(RuntimeError, match='恢复'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert ('apply', 'old') in engine.calls
    assert ('stop', 'new') in engine.calls
    assert ops.read(tmp_path / 'current.json')['version'] == 'old'
    assert database.read_bytes() == b'latest business data'
    assert not (tmp_path / 'data/.web-state/maintenance.json').exists()


def test_active_work_aborts_before_stopping_current_service(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'new')
    ops.write(tmp_path / 'data/.web-state/jobs/running.json', {'status': 'running'})
    engine = Engine()
    with pytest.raises(RuntimeError, match='任务'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert engine.calls == []
    assert not (tmp_path / 'data/.web-state/maintenance.json').exists()


def test_publish_records_version_and_rollback_keeps_current_data(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'old')
    release(ops, tmp_path, 'new')
    engine = Engine()
    ops.transition(tmp_path, 'old', engine=engine, timeout=0)
    ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert ops.read(tmp_path / 'current.json')['previous'] == 'old'
    target = ops.read(tmp_path / 'current.json')['previous']
    ops.transition(tmp_path, target, engine=engine, timeout=0)
    assert ops.read(tmp_path / 'current.json')['version'] == 'old'
    assert len(list((tmp_path / 'history').glob('*.json'))) == 3


def test_tampered_compose_and_incompatible_schema_are_rejected_before_switch(ops, tmp_path):
    initialize(ops, tmp_path)
    record = release(ops, tmp_path, 'new')
    engine = Engine()
    (tmp_path / 'releases/new/compose.yml').write_text('tampered')
    with pytest.raises(RuntimeError, match='校验'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    record['compose_sha256'] = ops.sha(tmp_path / 'releases/new/compose.yml')
    record['data_schema']['version'] = 2
    ops.write(tmp_path / 'releases/new/release.json', record)
    with pytest.raises(RuntimeError, match='schema'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert engine.calls == []


def test_unknown_existing_container_is_not_replaced(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'new')
    engine = Engine()
    engine.occupied = lambda settings: True
    with pytest.raises(RuntimeError, match='实例'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert engine.calls == []


def test_failed_recovery_keeps_maintenance_gate_for_operator(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'old')
    release(ops, tmp_path, 'new')
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    engine = Engine()
    engine.health = lambda *args: (_ for _ in ()).throw(RuntimeError('offline'))
    with pytest.raises(RuntimeError, match='恢复失败'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert (tmp_path / 'data/.web-state/maintenance.json').exists()


def test_nginx_site_has_independent_port_range_and_forwarded_protocol(ops):
    text = ops.nginx_config({'public_port': 8792, 'backend_port': 18792})
    assert 'listen 8792;' in text
    assert 'proxy_pass http://127.0.0.1:18792;' in text
    assert 'proxy_set_header X-Forwarded-Proto $scheme;' in text
    assert 'proxy_buffering off;' in text
    assert 'listen 80' not in text
    assert '124.156.228.202' not in text


def test_nginx_path_snippet_preserves_prefix_without_claiming_existing_server(ops):
    text = ops.nginx_config({'public_port': 80, 'backend_port': 18792}, base_path='/home')
    assert 'location = /home {' in text
    assert 'return 301 /home/;' in text
    assert 'location /home/ {' in text
    assert 'proxy_pass http://127.0.0.1:18792;' in text
    assert 'listen ' not in text and 'server_name ' not in text
    assert 'client_max_body_size 15m;' in text
    assert 'proxy_set_header X-Forwarded-Proto $scheme;' in text
    assert 'proxy_buffering off;' in text


def test_first_failed_publish_removes_own_container_and_can_be_retried(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'new')
    engine = Engine(fail='new')
    with pytest.raises(RuntimeError, match='恢复'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert not engine.present
    assert ('remove', 'new') in engine.calls
    engine.fail = None
    assert ops.transition(tmp_path, 'new', engine=engine, timeout=0)['status'] == 'ok'


@pytest.mark.parametrize('name', ['source/.env.production', 'source/sources/EP1.mp4',
                                'source/projects/demo/request.xlsx', 'source/feedback/result.json',
                                'source/outputs/final.mp4', 'source/accounts.sqlite3'])
def test_build_rejects_committed_secrets_and_business_data_before_docker(ops, tmp_path, name):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('private fixture')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    root = tmp_path / 'deployment'
    root.mkdir()
    with pytest.raises(RuntimeError, match='工程归档含'):
        ops.build(root, repo, 'fixture')


def test_build_refuses_dirty_source_before_creating_release(ops, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'dirty').write_text('uncommitted')
    root = tmp_path / 'deployment'
    root.mkdir()
    with pytest.raises(RuntimeError, match='干净'):
        ops.build(root, repo, 'fixture')
    assert not (root / 'releases/fixture').exists()
