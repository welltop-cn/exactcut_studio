"""Switch guards run before service stop and never strand both releases."""
import pytest
from test_linux_release import ops, initialize, release
from test_round1_release import CapableEngine, setup_target


@pytest.mark.parametrize('phase', ['fetch', 'render'])
def test_completed_review_without_publications_can_switch_old(ops, tmp_path, phase):
    engine = setup_target(ops, tmp_path)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': phase, 'status': 'completed', 'batch_id': 'batch', 'request_id': 'R1'})
    assert ops.check_switch(tmp_path, 'target', engine=engine)['allowed']


@pytest.mark.parametrize('status', ['unverified', 'failed'])
def test_forward_release_refuses_uncertain_delivery_before_stop(ops, tmp_path, status):
    engine = setup_target(ops, tmp_path, capable=True)
    old = release(ops, tmp_path, 'old'); old['image'] = 'sha256:' + 'c' * 64
    ops.write(tmp_path / 'releases/old/release.json', old)
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': status, 'batch_id': 'batch', 'request_id': 'R1'})
    ops.write(tmp_path / 'data/.web-state/batches/batch/publications.json', {'R1': {'status': 'unverified', 'intent': {'status': 'pending'}}})
    assert not ops.check_switch(tmp_path, 'target', engine=engine)['allowed']
    with pytest.raises(RuntimeError): ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert engine.calls == []
    assert ops.read(tmp_path / 'current.json')['version'] == 'old'
    assert not (tmp_path / 'data/.web-state/maintenance.json').exists()


def test_health_failure_with_post_stop_uncertainty_restores_old_under_maintenance(ops, tmp_path):
    engine = setup_target(ops, tmp_path, capable=True)
    old = release(ops, tmp_path, 'old'); old['image'] = 'sha256:' + 'c' * 64
    ops.write(tmp_path / 'releases/old/release.json', old)
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    engine.fail = 'target'
    health = engine.health
    def fail_new(settings, version, timeout):
        if version == 'target':
            ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'poll', 'status': 'unverified'})
        return health(settings, version, timeout)
    engine.health = fail_new
    with pytest.raises(RuntimeError, match='恢复'): ops.transition(tmp_path, 'target', engine=engine, timeout=0)
    assert ('apply', 'old') in engine.calls
    assert ('health', 'old') in engine.calls
    assert ops.read(tmp_path / 'current.json')['version'] == 'old'
    assert (tmp_path / 'data/.web-state/maintenance.json').exists()


@pytest.mark.parametrize('body', ['{', '[]', 'null'])
def test_bad_publication_json_still_blocks(ops, tmp_path, body):
    engine = setup_target(ops, tmp_path, capable=True)
    ops.write(tmp_path / 'data/.web-state/jobs/a.json', {'phase': 'render', 'status': 'completed', 'batch_id': 'batch', 'request_id': 'R1'})
    file = tmp_path / 'data/.web-state/batches/batch/publications.json'
    file.parent.mkdir(parents=True); file.write_text(body)
    assert not ops.check_switch(tmp_path, 'target', engine=engine)['allowed']
