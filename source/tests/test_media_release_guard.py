"""Code downgrade must never hand managed media data to a legacy writer."""
import pytest

from test_linux_release import Engine, initialize, ops, release


@pytest.mark.parametrize('marker', [
    'data/.web-state/media-registry/asset.json',
    'data/.web-state/batches/project/media-state.json',
])
def test_managed_or_partial_enrollment_blocks_old_image_before_stop(ops, tmp_path, marker):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'legacy')
    file = tmp_path / marker
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('{incomplete enrollment')
    engine = Engine()
    report = ops.check_switch(tmp_path, 'legacy', engine=engine)
    assert report['allowed'] is False
    assert '素材版本' in report['guard_error']
    with pytest.raises(RuntimeError, match='素材版本'):
        ops.transition(tmp_path, 'legacy', engine=engine, timeout=0)
    assert engine.calls == []
    assert file.read_text() == '{incomplete enrollment'


def test_compatible_image_can_recover_managed_data_without_restoring_it(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'compatible')
    marker = tmp_path / 'data/.web-state/media-registry/asset.json'
    ops.write(marker, {'status': 'active'})
    engine = Engine()
    engine.media_protocol = lambda image: True
    assert ops.check_switch(tmp_path, 'compatible', engine=engine)['allowed'] is True
    ops.transition(tmp_path, 'compatible', engine=engine, timeout=0)
    assert ops.read(marker) == {'status': 'active'}


def test_failed_new_startup_cannot_restore_old_writer_after_enrollment(ops, tmp_path):
    initialize(ops, tmp_path)
    old = release(ops, tmp_path, 'old')
    new = release(ops, tmp_path, 'new')
    new['image'] = 'sha256:' + 'c' * 64
    ops.write(tmp_path / 'releases/new/release.json', new)
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    engine = Engine()
    engine.media_protocol = lambda image: image == new['image']
    original_health = engine.health
    marker = tmp_path / 'data/.web-state/media-registry/asset.json'
    def health(settings, version, timeout):
        if version == 'new':
            ops.write(marker, {'status': 'pending'})
            raise RuntimeError('startup failed after durable enrollment')
        return original_health(settings, version, timeout)
    engine.health = health
    with pytest.raises(RuntimeError, match='恢复失败'):
        ops.transition(tmp_path, 'new', engine=engine, timeout=0)
    assert ('apply', 'old') not in engine.calls
    assert (tmp_path / 'data/.web-state/maintenance.json').exists()
    assert ops.read(marker) == {'status': 'pending'}


def recovery_fixture(ops, root):
    initialize(ops, root)
    release(ops, root, 'old')
    new = release(ops, root, 'compatible')
    new['image'] = 'sha256:' + 'c' * 64
    ops.write(root / 'releases/compatible/release.json', new)
    ops.write(root / 'current.json', {'version': 'old'})
    ops.write(root / 'data/.web-state/media-registry/asset.json', {'status': 'pending'})
    ops.write(root / 'data/.web-state/maintenance.json', {'reason': 'failed release'})
    engine = Engine()
    engine.media_protocol = lambda image: image == new['image']
    return engine


def test_retained_maintenance_can_recover_explicit_compatible_release(ops, tmp_path):
    engine = recovery_fixture(ops, tmp_path)
    result = ops.recover(tmp_path, version='compatible', engine=engine)
    assert result['release'] == 'compatible'
    assert ops.read(tmp_path / 'current.json')['version'] == 'compatible'
    assert not (tmp_path / 'data/.web-state/maintenance.json').exists()
    assert ops.read(tmp_path / 'data/.web-state/media-registry/asset.json') == {'status': 'pending'}
    assert ('apply', 'old') not in engine.calls


def test_failed_explicit_recovery_keeps_maintenance_and_never_applies_old(ops, tmp_path):
    engine = recovery_fixture(ops, tmp_path)
    engine.fail = 'compatible'
    with pytest.raises(RuntimeError, match='health failed'):
        ops.recover(tmp_path, version='compatible', engine=engine)
    assert (tmp_path / 'data/.web-state/maintenance.json').exists()
    assert ('apply', 'old') not in engine.calls
    assert ops.read(tmp_path / 'current.json')['version'] == 'old'


def test_explicit_recovery_requires_maintenance_and_no_active_work(ops, tmp_path):
    engine = recovery_fixture(ops, tmp_path)
    ops.write(tmp_path / 'data/.web-state/jobs/running.json', {'status': 'running'})
    with pytest.raises(RuntimeError, match='任务'):
        ops.recover(tmp_path, version='compatible', engine=engine)
    assert engine.calls == []
    (tmp_path / 'data/.web-state/jobs/running.json').unlink()
    (tmp_path / 'data/.web-state/maintenance.json').unlink()
    with pytest.raises(RuntimeError, match='维护'):
        ops.recover(tmp_path, version='compatible', engine=engine)
    assert engine.calls == []


def test_maintenance_verifier_rechecks_protocol_after_stop(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'old')
    release(ops, tmp_path, 'verifier')
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    ops.write(tmp_path / 'data/.web-state/maintenance.json', {'reason': 'verify'})
    engine = Engine()
    engine.stage_protocol = lambda image: True
    engine.media_protocol = lambda image: False
    original_stop = engine.stop
    def stop(root, release, settings):
        original_stop(root, release, settings)
        ops.write(root / 'data/.web-state/media-registry/late.json', {'status': 'pending'})
    engine.stop = stop
    engine.verify_delivery = lambda *a: engine.calls.append(('verify', 'verifier'))
    with pytest.raises(RuntimeError, match='素材版本'):
        ops.verify_delivery_in_maintenance(tmp_path, 'verifier', 'a' * 32, 'R1', 'c' * 64, engine=engine)
    assert ('verify', 'verifier') not in engine.calls
    assert ('apply', 'old') not in engine.calls
    assert (tmp_path / 'data/.web-state/maintenance.json').exists()


def test_maintenance_verifier_child_can_acquire_its_own_mutation_lock(ops, tmp_path):
    import subprocess
    import sys
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'current')
    ops.write(tmp_path / 'current.json', {'version': 'current'})
    ops.write(tmp_path / 'data/.web-state/maintenance.json', {'reason': 'verify'})
    engine = Engine()
    engine.stage_protocol = lambda image: True
    def verify(root, *args):
        script = 'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("acquired")'
        child = subprocess.run([sys.executable, '-c', script, str(root / 'data/.web-state/mutation.lock')],
                               capture_output=True, text=True, timeout=2, check=True)
        assert child.stdout.strip() == 'acquired'
        return {'status': 'queried'}
    engine.verify_delivery = verify
    result = ops.verify_delivery_in_maintenance(tmp_path, 'current', 'a' * 32, 'R1', 'c' * 64, engine=engine)
    assert result['status'] == 'queried'


def test_busy_maintenance_barrier_does_not_restart_current_service(ops, tmp_path):
    import fcntl
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'current')
    ops.write(tmp_path / 'current.json', {'version': 'current'})
    ops.write(tmp_path / 'data/.web-state/maintenance.json', {'reason': 'verify'})
    engine = Engine()
    engine.stage_protocol = lambda image: True
    with (tmp_path / 'data/.web-state/mutation.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match='请求'):
            ops.verify_delivery_in_maintenance(tmp_path, 'current', 'a' * 32, 'R1', 'c' * 64, engine=engine)
    assert engine.calls == []
