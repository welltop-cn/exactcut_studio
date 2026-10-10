import importlib.util
import os
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from exactcut import runtime
from exactcut.util import load_env_file


def test_explicit_config_does_not_load_legacy_data_env(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    selected = tmp_path / '.env.development'
    selected.write_text('EXACTCUT_ENVIRONMENT=development\nISOLATION_FIXTURE=development\n')
    legacy = tmp_path / 'data/.env'
    legacy.parent.mkdir()
    legacy.write_text('ISOLATION_FIXTURE=production\n')
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(selected))
    monkeypatch.delenv('ISOLATION_FIXTURE', raising=False)
    load_env_file(runtime.project_env_file())
    assert os.environ['ISOLATION_FIXTURE'] == 'development'


@pytest.mark.parametrize('environment', ['development', 'staging', 'test'])
def test_production_config_rejected_before_any_value_is_loaded(tmp_path, monkeypatch, environment):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', environment)
    path = tmp_path / 'private.env'
    path.write_text('ISOLATION_FIXTURE=must-not-load\nEXACTCUT_ENVIRONMENT=production\n')
    monkeypatch.delenv('ISOLATION_FIXTURE', raising=False)
    with pytest.raises(RuntimeError, match='配置环境'):
        load_env_file(path)
    assert 'ISOLATION_FIXTURE' not in os.environ


def test_production_filename_is_rejected_even_without_owner_key(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    path = tmp_path / '.env.production'
    path.write_text('ISOLATION_FIXTURE=must-not-load\n')
    with pytest.raises(RuntimeError, match='配置环境'):
        load_env_file(path)


def test_data_directory_cannot_be_reused_by_another_environment(tmp_path, monkeypatch):
    root = tmp_path / 'production'
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(root))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    runtime.claim_data_root()
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'staging')
    with pytest.raises(RuntimeError, match='数据目录.*环境'):
        runtime.data_root()


def test_config_cannot_change_environment_after_import(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    path = tmp_path / 'development.env'
    path.write_text('EXACTCUT_ENVIRONMENT=development\nEXACTCUT_DATA_ROOT=/other/data\n')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    load_env_file(path)
    assert runtime.data_root() == tmp_path


@pytest.mark.parametrize('script', ['material_hub.py', 'quality_sync.py'])
def test_standalone_tools_refuse_foreign_config(tmp_path, monkeypatch, script):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    config = tmp_path / 'foreign.env'
    config.write_text('ISOLATION_FIXTURE=must-not-load\nEXACTCUT_ENVIRONMENT=production\n')
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(config))
    monkeypatch.delenv('ISOLATION_FIXTURE', raising=False)
    path = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts' / script
    spec = importlib.util.spec_from_file_location('foreign_config_fixture', path)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match='配置环境'):
        spec.loader.exec_module(module)
    assert 'ISOLATION_FIXTURE' not in os.environ


@pytest.mark.parametrize('environment', ['development', 'staging', 'test'])
def test_marked_production_source_directory_is_rejected(tmp_path, monkeypatch, environment):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'production'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    runtime.claim_data_root()
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'isolated'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', environment)
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(tmp_path / 'production/sources'))
    with pytest.raises(RuntimeError, match='环境'):
        runtime.source_root()


def test_production_cannot_use_development_source_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'development'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    runtime.claim_data_root()
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path / 'production'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(tmp_path / 'development/sources'))
    with pytest.raises(RuntimeError, match='环境'):
        runtime.source_root()


def test_container_startup_rejects_foreign_config_before_password_write(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / 'production.env'
    config.write_text('EXACTCUT_ENVIRONMENT=production\n')
    data = tmp_path / 'data'
    env = dict(os.environ, EXACTCUT_ENVIRONMENT='development', EXACTCUT_DATA_ROOT=str(data),
               EXACTCUT_ENV_FILE=str(config), PYTHONPATH=str(root),
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    result = subprocess.run(['sh', str(root / 'docker/entrypoint.sh')], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert '配置环境' in result.stderr
    assert not (data / '.web-state/password').exists()


def test_container_rejects_invalid_runtime_policy_before_initializing_data(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / 'production.env'
    config.write_text('EXACTCUT_ENVIRONMENT=production\n')
    policy = tmp_path / 'runtime.json'
    policy.write_text(json.dumps({'environment': 'production', 'storage': {'reserve_gib': 0}}))
    data = tmp_path / 'data'
    env = dict(os.environ, EXACTCUT_ENVIRONMENT='production', EXACTCUT_DATA_ROOT=str(data),
               EXACTCUT_CONFIG_FILE=str(policy), EXACTCUT_ENV_FILE=str(config), PYTHONPATH=str(root),
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    result = subprocess.run(['sh', str(root / 'docker/entrypoint.sh')], env=env,
                            capture_output=True, text=True, timeout=2)
    assert result.returncode != 0
    assert 'storage.reserve_gib' in result.stderr
    assert not (data / '.web-state/password').exists()


@pytest.mark.skipif(not shutil.which('docker'), reason='requires Docker Compose CLI, no daemon needed')
def test_compose_profiles_have_distinct_configs_data_names_and_ports():
    root = Path(__file__).resolve().parents[1]
    models = {}
    for profile, filename in [('development', 'docker-compose.yml'),
                               ('production', 'docker-compose.production.yml')]:
        result = subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '-f', str(root / filename),
                                 'config', '--no-env-resolution', '--format', 'json'],
                                env=dict(os.environ, EXACTCUT_IMAGE='exactcut-web:fixture', EXACTCUT_RELEASE_ID='fixture'),
                                capture_output=True, text=True, check=True)
        models[profile] = json.loads(result.stdout)
    names, configs, data, ports = [], [], [], []
    for profile, model in models.items():
        service = model['services']['web']
        names.append(model['name'])
        configs.append(service['env_file'][0]['path'])
        data.append(next(v['source'] for v in service['volumes'] if v['target'] == '/data'))
        ports.append(service['ports'][0]['published'])
        assert service['environment']['EXACTCUT_ENVIRONMENT'] == profile
        assert service['environment']['EXACTCUT_ENV_FILE'].endswith(f'{profile}.env')
        for flag in ('EXACTCUT_ALLOW_MATERIAL_FETCH', 'EXACTCUT_ALLOW_AI', 'EXACTCUT_ALLOW_PUBLISH',
                     'EXACTCUT_ALLOW_EXTERNAL_WRITES'):
            assert service['environment'][flag] == ('1' if profile == 'production' else '0')
        assert service['ports'][0]['host_ip'] == '127.0.0.1'
    for values in (names, configs, data, ports):
        assert len(set(values)) == 2


@pytest.mark.skipif(not shutil.which('docker'), reason='requires Docker Compose CLI')
def test_production_deployment_can_change_machine_directory_and_ports(tmp_path):
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / 'new machine'
    env = dict(os.environ, EXACTCUT_DEPLOY_ROOT=str(target), EXACTCUT_BACKEND_PORT='18888',
               EXACTCUT_INSTANCE='exactcut-second-machine', EXACTCUT_IMAGE='exactcut-web:verified',
               EXACTCUT_RELEASE_ID='verified', EXACTCUT_CPUS='1.5', EXACTCUT_MEMORY='3g')
    result = subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '-f',
                             str(root / 'docker-compose.production.yml'), 'config',
                             '--no-env-resolution', '--format', 'json'], env=env,
                            capture_output=True, text=True, check=True)
    model = json.loads(result.stdout)
    service = model['services']['web']
    assert model['name'] == 'exactcut-second-machine'
    assert service['env_file'][0]['path'] == str(target / 'config/production.env')
    assert next(v['source'] for v in service['volumes'] if v['target'] == '/data') == str(target / 'data')
    assert service['ports'][0]['published'] == '18888'
    assert service['ports'][0]['host_ip'] == '127.0.0.1'
    assert float(service['cpus']) == 1.5
    assert int(service['mem_limit']) == 3 * 1024**3


def test_dev_launcher_ignores_inherited_production_runtime_config(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[1]
    local = tmp_path/'source'
    (local/'bin').mkdir(parents=True)
    (local/'config').mkdir()
    shutil.copy2(root/'bin/dev-web', local/'bin/dev-web')
    shutil.copy2(root/'config/default.json', local/'config/default.json')
    (local/'.env.development').write_text('EXACTCUT_ENVIRONMENT=development\n')
    foreign = tmp_path/'production.json'
    foreign.write_text(json.dumps({'environment': 'production', 'web': {'base_path': '/production'}}))
    probe = tmp_path/'python-probe'
    probe.write_text('#!' + sys.executable + '\n' +
                     'import sys, json\n'
                     'if "-m" in sys.argv:\n'
                     ' from exactcut.util import load_config\n'
                     ' print(json.dumps(load_config()["web"]))\n'
                     'else:\n'
                     ' exec(sys.stdin.read())\n')
    probe.chmod(0o755)
    result = subprocess.run(['bash', str(local/'bin/dev-web')], text=True, capture_output=True,
                            timeout=10, env={**os.environ, 'EXACTCUT_PYTHON': str(probe),
                                             'EXACTCUT_CONFIG_FILE': str(foreign), 'PYTHONPATH': str(root)})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['base_path'] == '/home'
