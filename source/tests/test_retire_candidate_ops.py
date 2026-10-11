import pytest
from test_linux_release import ops, initialize, release, Engine

PARENT, CHILD, JOB = 'a' * 32, 'c' * 32, 'd' * 32


class RecoveryEngine(Engine):
    stage_protocol = media_protocol = lambda self, image: True

    def retire_candidate(self, root, target, settings, parent, child, job, *, apply=False):
        self.calls.append(('retire', apply))
        if apply:
            assert ('stop', 'old') in self.calls
            assert (root / 'data/.web-state/maintenance.json').exists()
            if self.fail == 'retire': raise RuntimeError('retire failed')
            file = root / 'data/.web-state/jobs' / (job + '.json')
            self.ops.write(file, {**self.ops.read(file), 'status': 'interrupted'})
            self.ops.write(root / 'data/.web-state/batches' / child / 'runtime.json', {'status': 'interrupted'})
        return {'status': 'retired' if apply else 'eligible', 'job_id': job}


@pytest.fixture
def setup(ops, tmp_path):
    initialize(ops, tmp_path)
    release(ops, tmp_path, 'old'); release(ops, tmp_path, 'new')
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    ops.write(tmp_path / 'data/.web-state/jobs' / (JOB + '.json'),
              {'id': JOB, 'batch_id': CHILD, 'phase': 'render', 'status': 'queued'})
    ops.write(tmp_path / 'data/.web-state/batches' / CHILD / 'runtime.json', {'status': 'producing'})
    engine = RecoveryEngine(); engine.ops = ops
    return ops, tmp_path, engine


def test_recovery_stops_worker_restores_same_release_and_keeps_current(setup):
    ops, root, engine = setup
    result = ops.retire_candidate_in_maintenance(root, 'new', PARENT, CHILD, JOB, engine=engine)
    assert result['status'] == 'retired'
    assert engine.calls.index(('stop', 'old')) < engine.calls.index(('retire', True)) < engine.calls.index(('apply', 'old'))
    assert ops.read(root / 'current.json')['version'] == 'old'
    assert not (root / 'data/.web-state/maintenance.json').exists()


def test_other_active_work_refuses_recovery_before_stop(setup):
    ops, root, engine = setup
    ops.write(root / 'data/.web-state/jobs/other.json', {'status': 'running'})
    with pytest.raises(RuntimeError):
        ops.retire_candidate_in_maintenance(root, 'new', PARENT, CHILD, JOB, engine=engine)
    assert ('stop', 'old') not in engine.calls
    assert not (root / 'data/.web-state/maintenance.json').exists()


def test_recovery_failure_restores_old_worker_and_retains_maintenance(setup):
    ops, root, engine = setup; engine.fail = 'retire'
    with pytest.raises(RuntimeError):
        ops.retire_candidate_in_maintenance(root, 'new', PARENT, CHILD, JOB, engine=engine)
    assert ('apply', 'old') in engine.calls
    assert (root / 'data/.web-state/maintenance.json').exists()
    assert ops.read(root / 'current.json')['version'] == 'old'


def test_legacy_uncertain_job_also_refuses_recovery(setup):
    ops, root, engine = setup
    ops.write(root / 'data/.web-state/jobs/legacy.json', {'status': 'unverified'})
    with pytest.raises(RuntimeError):
        ops.retire_candidate_in_maintenance(root, 'new', PARENT, CHILD, JOB, engine=engine)
    assert ('stop', 'old') not in engine.calls


def test_job_only_recovery_cannot_clear_maintenance(setup):
    ops, root, engine = setup
    original = engine.retire_candidate
    def leave_runtime(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('apply'):
            ops.write(root / 'data/.web-state/batches' / CHILD / 'runtime.json', {'status': 'producing'})
        return result
    engine.retire_candidate = leave_runtime
    with pytest.raises(RuntimeError):
        ops.retire_candidate_in_maintenance(root, 'new', PARENT, CHILD, JOB, engine=engine)
    assert ('apply', 'old') in engine.calls
    assert (root / 'data/.web-state/maintenance.json').exists()
