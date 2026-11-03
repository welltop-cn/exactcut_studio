"""Runtime vs development dependency locks: production never ships test tools."""
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]
TEST_TOOLS = {'pytest', 'pytest-xdist', 'execnet', 'iniconfig', 'pluggy', 'pygments'}


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, SOURCE / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _names(path):
    return {re.sub(r'[-_.]+', '-', line.split('==')[0]).lower()
            for line in path.read_text().splitlines() if line.strip() and not line.startswith(('#', '-'))}


def test_runtime_lock_has_no_test_tools_and_dev_lock_extends_it():
    runtime = _names(SOURCE / 'requirements.lock.txt')
    dev = _names(SOURCE / 'requirements-dev.lock.txt')
    assert '-r requirements.lock.txt' in (SOURCE / 'requirements-dev.lock.txt').read_text().splitlines()
    assert not runtime & TEST_TOOLS
    assert {'pytest', 'pytest-xdist'} <= dev
    assert not runtime & dev
    for line in (SOURCE / 'requirements.lock.txt').read_text().splitlines():
        assert re.fullmatch(r'[A-Za-z0-9._-]+==[A-Za-z0-9.+!_-]+', line), line


def test_runtime_image_installs_only_the_runtime_lock():
    dockerfile = (SOURCE / 'Dockerfile').read_text()
    assert 'pip install --no-cache-dir -r requirements.lock.txt' in dockerfile
    assert 'requirements-dev' not in dockerfile


def test_application_code_does_not_import_test_tools():
    # devtools/ and tests/ are not shipped in the image and may use pytest.
    pattern = re.compile(r'^\s*(?:import|from)\s+(pytest|_pytest|xdist|execnet|pluggy|iniconfig|pygments)\b', re.M)
    offenders = [str(p.relative_to(SOURCE)) for folder in ('exactcut', 'deploy')
                 for p in (SOURCE / folder).rglob('*.py') if pattern.search(p.read_text(errors='ignore'))]
    assert offenders == []


@pytest.fixture
def mac_ops(tmp_path, monkeypatch):
    module = _load('dependency_lock_ops', 'deploy/ops.py')
    release = tmp_path / 'release'
    release.mkdir()
    (release / 'requirements.lock.txt').write_text('runtime==1.0\nother==2.0\n')
    frozen = []
    monkeypatch.setattr(module, 'run', lambda *args, **kwargs: '\n'.join(frozen) + '\n')
    return module, release, frozen


def test_release_venv_matching_runtime_lock_passes(mac_ops):
    module, release, frozen = mac_ops
    frozen[:] = ['runtime==1.0', 'other==2.0', '-e git+https://example.invalid/app#egg=app']
    module.check_dependencies(release)


def test_locked_test_tools_left_in_release_venv_are_tolerated(mac_ops):
    module, release, frozen = mac_ops
    (release / 'requirements-dev.lock.txt').write_text('# tools\n-r requirements.lock.txt\npytest==9.1.1\n')
    frozen[:] = ['runtime==1.0', 'other==2.0', 'pytest==9.1.1']
    module.check_dependencies(release)


@pytest.mark.parametrize('frozen', [['runtime==1.0', 'other==2.0', 'pytest==8.0.0'],
                                    ['runtime==1.0', 'other==2.0', 'unlisted==1.0'],
                                    ['runtime==1.0', 'pytest==9.1.1'],
                                    ['runtime==1.0', 'other==2.1']])
def test_unlocked_extras_or_missing_runtime_pins_are_rejected(mac_ops, frozen):
    module, release, current = mac_ops
    (release / 'requirements-dev.lock.txt').write_text('-r requirements.lock.txt\npytest==9.1.1\n')
    current[:] = frozen
    with pytest.raises(RuntimeError, match='do not match'):
        module.check_dependencies(release)


def test_without_dev_lock_release_venv_must_equal_runtime_lock(mac_ops):
    module, release, frozen = mac_ops
    frozen[:] = ['runtime==1.0', 'other==2.0', 'pytest==9.1.1']
    with pytest.raises(RuntimeError, match='do not match'):
        module.check_dependencies(release)


def test_linux_validation_image_adds_dev_lock_on_top_of_untouched_runtime_image(tmp_path, monkeypatch):
    ops = _load('dependency_lock_linux_ops', 'deploy/linux_ops.py')
    repo = tmp_path / 'repo'
    for relative, text in {'source/deploy/data-schema.json': '{"version": 1, "readable": [1]}',
                           'source/docker-compose.production.yml': 'services: {}\n',
                           'source/requirements.lock.txt': 'runtime==1.0\n',
                           'source/requirements-dev.lock.txt': '-r requirements.lock.txt\npytest==9.1.1\n'}.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'fixture'], check=True)
    commit = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                            capture_output=True, text=True).stdout.strip()
    docker_calls = []
    real_run = ops.run

    def fake_run(*args, **kwargs):
        if args[0] != 'docker':
            return real_run(*args, **kwargs)
        docker_calls.append((tuple(map(str, args)), kwargs.get('input')))
        if args[1] == 'run':
            kwargs['stdout'].write('3 passed\n')

    class FakeDocker:
        def inspect_image(self, image):
            return 'sha256:' + 'c' * 64, commit

        def stage_protocol(self, image):
            return True

    monkeypatch.setattr(ops, 'run', fake_run)
    monkeypatch.setattr(ops, 'Docker', FakeDocker)
    monkeypatch.setattr(ops, 'verify', lambda *args: None)
    root = tmp_path / 'deployment'
    root.mkdir()
    assert ops.build(root, repo, 'fixture')['tests']['passed'] == 3
    runtime_build, validation_build, test_run = docker_calls
    assert runtime_build[1] is None and any(a.endswith('source/Dockerfile') for a in runtime_build[0])
    args, dockerfile = validation_build
    assert args[:6] == ('docker', 'build', '-t', 'exactcut-validation:fixture', '-f', '-')
    assert Path(args[6]) == root / 'releases/fixture/source'
    assert 'COPY source/requirements.lock.txt source/requirements-dev.lock.txt' in dockerfile
    assert 'pip install --no-cache-dir -r /tmp/exactcut-deps/requirements-dev.lock.txt' in dockerfile
    assert test_run[0][:2] == ('docker', 'run') and 'pytest' in test_run[0]
