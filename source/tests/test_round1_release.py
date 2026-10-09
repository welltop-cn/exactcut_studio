import json
from pathlib import Path
import sys
import subprocess

import pytest
from test_linux_release import ops, Engine, initialize, release


class CapableEngine(Engine):
    def __init__(self):
        super().__init__()
        self.capabilities = {}

    def stage_protocol(self, image):
        return self.capabilities.get(image, False)


def setup_target(ops, root, capable=False):
    initialize(ops, root)
    record = release(ops, root, 'target')
    engine = CapableEngine()
    engine.capabilities[record['image']] = capable
    return engine


@pytest.mark.parametrize('status', ['queued', 'running', 'paused', 'archived', 'unverified', 'future-status', None, []])
def test_old_image_refuses_unfinished_phase_before_service_mutation(ops, tmp_path, status):
    engine = setup_target(ops, tmp_path)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': status, 'not_before': 9999999999})
    with pytest.raises(RuntimeError, match='阶段|phase'):
        ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert engine.calls == []
    assert not (tmp_path / 'data/.web-state/maintenance.json').exists()


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted'])
def test_terminal_phase_history_allows_old_image(ops, tmp_path, status):
    engine = setup_target(ops, tmp_path)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'render', 'status': status})
    assert ops.transition(tmp_path, 'target', engine=engine, timeout=0)['status'] == 'ok'


@pytest.mark.parametrize('entry', [{'status': 'unverified'}, {'status': 'publish_failed', 'intent': {'status': 'pending'}}])
def test_failed_phase_with_uncertain_publication_still_blocks_old_image(ops, tmp_path, entry):
    engine = setup_target(ops, tmp_path)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'upload', 'status': 'failed', 'batch_id': 'batch', 'request_id': 'R1'})
    ops.write(tmp_path / 'data/.web-state/batches/batch/publications.json', {'R1': entry})
    with pytest.raises(RuntimeError, match='阶段|phase'):
        ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert engine.calls == []


@pytest.mark.parametrize('body', ['{', '[]', 'null'])
def test_unreadable_job_fails_closed_before_old_image(ops, tmp_path, body):
    engine = setup_target(ops, tmp_path)
    path = tmp_path / 'data/.web-state/jobs/a.json'
    path.parent.mkdir(parents=True)
    path.write_text(body)
    with pytest.raises(RuntimeError, match='阶段|phase'):
        ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert engine.calls == []


def test_new_capable_image_keeps_drain_rules(ops, tmp_path):
    engine = setup_target(ops, tmp_path, capable=True)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': 'queued', 'not_before': 9999999999})
    with pytest.raises(RuntimeError, match='任务'):
        ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert engine.calls == []


def test_capability_is_bound_to_distinct_immutable_images_not_release_metadata(ops, tmp_path):
    engine = setup_target(ops, tmp_path, capable=True)
    old = release(ops, tmp_path, 'old')
    old['image'] = 'sha256:' + 'c' * 64
    old['stage_protocol'] = True  # Arbitrary metadata cannot upgrade an unsupported image.
    ops.write(tmp_path / 'releases/old/release.json', old)
    engine.capabilities[old['image']] = False
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': 'unverified'})
    assert ops.check_switch(tmp_path, 'target', engine=engine)['target_stage_protocol'] is True
    old_check = ops.check_switch(tmp_path, 'old', engine=engine)
    assert old_check['target_stage_protocol'] is False and old_check['allowed'] is False
    with pytest.raises(RuntimeError, match='阶段'):
        ops.transition(tmp_path, 'old', engine=engine, timeout=0)
    assert engine.calls == []


def test_read_only_check_reports_target_verification_failure_before_mutation(ops, tmp_path):
    engine = setup_target(ops, tmp_path)
    (tmp_path / 'releases/target/compose.yml').write_text('tampered')
    result = ops.check_switch(tmp_path, 'target', engine=engine)
    assert not result['allowed'] and '校验' in result['guard_error']
    assert engine.calls == []


def test_guard_rechecks_admission_after_maintenance_acquisition(ops, tmp_path, monkeypatch):
    engine = setup_target(ops, tmp_path)
    from contextlib import contextmanager
    original = ops.write_barrier
    @contextmanager
    def concurrent_admission(root, timeout):
        ops.write(root / 'data/.web-state/jobs/a.json', {'phase': 'render', 'status': 'paused'})
        with original(root, timeout) as gate:
            yield gate
    monkeypatch.setattr(ops, 'write_barrier', concurrent_admission)
    with pytest.raises(RuntimeError, match='阶段|phase'):
        ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert engine.calls == []


def test_check_switch_is_read_only_and_reports_code_guard(ops, tmp_path, monkeypatch, capsys):
    engine = setup_target(ops, tmp_path)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': 'unverified'})
    monkeypatch.setattr(ops, 'Docker', lambda: engine)
    monkeypatch.setattr(sys, 'argv', ['ops', '--root', str(tmp_path), 'check-switch', 'target'])
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit) as error:
        ops.main()
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['target_stage_protocol'] is False
    assert result['allowed'] is False and result['phase_blockers']
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert engine.calls == []


@pytest.mark.parametrize('operation', ['inspect_image', 'stage_protocol'])
def test_check_switch_docker_failure_returns_safe_json(ops, tmp_path, monkeypatch, capsys, operation):
    engine = setup_target(ops, tmp_path)
    def unavailable(*args):
        raise subprocess.CalledProcessError(1, ['docker', 'image', 'inspect'], output='private-provider-output')
    monkeypatch.setattr(engine, operation, unavailable)
    monkeypatch.setattr(ops, 'Docker', lambda: engine)
    monkeypatch.setattr(sys, 'argv', ['ops', '--root', str(tmp_path), 'check-switch', 'target'])
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit) as error:
        ops.main()
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['allowed'] is False and result['guard_error'] == 'CalledProcessError'
    assert 'private-provider-output' not in json.dumps(result)
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert engine.calls == []


def test_docker_capability_is_inspected_from_image_label(ops, monkeypatch):
    observed = []
    def inspect(*args):
        observed.append(args)
        return json.dumps([{'Id': 'sha256:fixture', 'Config': {'Labels': {'io.exactcut.stage-protocol': '1'}}}])
    monkeypatch.setattr(ops, 'output', inspect)
    assert ops.Docker().stage_protocol('sha256:fixture') is True
    assert observed == [('docker', 'image', 'inspect', 'sha256:fixture')]
    monkeypatch.setattr(ops, 'output', lambda *args: json.dumps([{'Config': {'Labels': None}}]))
    assert ops.Docker().stage_protocol('sha256:old') is False


def test_build_definition_declares_stage_protocol_and_dev_limits():
    source = Path(__file__).resolve().parents[1]
    assert 'LABEL io.exactcut.stage-protocol="1"' in (source / 'Dockerfile').read_text()
    compose = (source / 'docker-compose.yml').read_text()
    assert 'cpus: 1.0' in compose and 'mem_limit: 2g' in compose and 'pids_limit: 256' in compose
    production = (source / 'docker-compose.production.yml').read_text()
    assert 'EXACTCUT_CPUS:-14.0' in production and 'EXACTCUT_MEMORY:-24g' in production and 'pids_limit: 512' in production


def test_compose_uses_approved_deployment_quota_over_parent_environment(ops, tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_CPUS', '1')
    monkeypatch.setenv('EXACTCUT_MEMORY', '2g')
    calls = []
    monkeypatch.setattr(ops, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    ops.Docker().compose(tmp_path, {'version': 'target', 'image': 'sha256:fixture'},
                         {'instance': 'fixture', 'backend_port': 18792, 'cpus': 14.0, 'memory': '24g'}, 'up')
    assert calls[0][1]['env']['EXACTCUT_CPUS'] == '14.0'
    assert calls[0][1]['env']['EXACTCUT_MEMORY'] == '24g'


@pytest.mark.parametrize('quota', [{}, {'cpus': 0, 'memory': '24g'}, {'cpus': True, 'memory': '24g'},
                                  {'cpus': float('nan'), 'memory': '24g'}, {'cpus': 14, 'memory': 'invalid'}])
def test_invalid_or_missing_quota_fails_before_docker(ops, tmp_path, monkeypatch, quota):
    calls = []
    monkeypatch.setattr(ops, 'run', lambda *args, **kwargs: calls.append(args))
    with pytest.raises((RuntimeError, ValueError), match='资源'):
        ops.Docker().compose(tmp_path, {'version': 'target', 'image': 'sha256:fixture'},
                             {'instance': 'fixture', 'backend_port': 18792, **quota}, 'up')
    assert calls == []


def test_guard_rechecks_after_graceful_stop_before_apply(ops, tmp_path):
    engine = setup_target(ops, tmp_path)
    release(ops, tmp_path, 'current')
    ops.write(tmp_path / 'current.json', {'version': 'current'})
    original_stop = engine.stop
    def stop(root, record, settings):
        original_stop(root, record, settings)
        if record['version'] == 'current':
            ops.write(root / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': 'unverified'})
    engine.stop = stop
    with pytest.raises(RuntimeError):
        ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert ('apply', 'target') not in engine.calls
    assert ('apply', 'current') in engine.calls
    assert (tmp_path / 'data/.web-state/maintenance.json').exists()
    assert ops.read(tmp_path / 'current.json')['version'] == 'current'
