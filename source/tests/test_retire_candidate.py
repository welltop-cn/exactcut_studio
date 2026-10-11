"""Explicit recovery never admits an executor or changes authoritative media."""
import copy
import pytest
from exactcut import workflow as w, webserver as web
from exactcut.util import read_json
from test_workflow import workspace
from test_round1_integration import chain
from test_material_versions import changed_candidate, token


@pytest.fixture
def stale_render(chain, monkeypatch):
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '0')
    from exactcut.runtime import maintenance_file
    maintenance_file().write_text('{}')
    m, parent, state, _ = changed_candidate(); m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'candidate')
    child = w.BATCHES / event['child_id']
    assert w.fetch(child.name) == 0
    fetch = read_json(web.JOBS / (event['job_id'] + '.json'))
    fetch.update(status='completed', attempt=1); web.save(fetch); web.advance_stage(fetch)
    job = next(j for j in web.stored_jobs() if j.get('parent_id') == fetch['id'])
    m.control(parent, 'delete', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
    return m, parent, child, job


def test_explicit_recovery_dry_run_apply_and_replay(stale_render):
    from exactcut.retire_candidate import retire
    m, parent, child, job = stale_render
    before = m.read(parent); runtime = (child / 'runtime.json').read_bytes()
    ids = parent.name, child.name, job['id']
    assert retire(*ids)['status'] == 'eligible'
    assert read_json(web.JOBS / (job['id'] + '.json')) == job
    assert (child / 'runtime.json').read_bytes() == runtime
    assert retire(*ids, apply=True)['status'] == 'retired'
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'interrupted'
    assert retire(*ids, apply=True)['status'] == 'already_retired'
    assert m.read(parent) == before
    assert read_json(child / 'runtime.json')['status'] == 'interrupted'
    # Existing scheduler recovery, without the new pre-tick cancellation helper,
    # terminates runtime and retains the deterministic successor as history.
    web.recover_jobs(enqueue=False)
    assert read_json(child / 'runtime.json')['status'] not in w.ACTIVE
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'interrupted'
    assert m.read(parent) == before


@pytest.mark.parametrize('change', [
    {'status': 'running'}, {'status': 'waiting_resource'}, {'attempt': 1},
    {'pid': 123}, {'process_token': 'a' * 32}, {'phase': 'upload'},
    {'instance': '--instance=other'}, {'parent_id': 'b' * 32},
])
def test_explicit_recovery_rejects_execution_or_other_identity(stale_render, change):
    from exactcut.retire_candidate import retire
    _, parent, child, job = stale_render
    job.update(change); web.save(job)
    before = copy.deepcopy(job)
    with pytest.raises(ValueError): retire(parent.name, child.name, job['id'], apply=True)
    assert read_json(web.JOBS / (job['id'] + '.json')) == before


def test_explicit_recovery_rejects_healthy_candidate(chain):
    from exactcut.retire_candidate import retire
    m, parent, state, _ = changed_candidate(); m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'candidate')
    with pytest.raises(ValueError): retire(parent.name, event['child_id'], event['job_id'], apply=True)


def test_online_apply_is_rejected(stale_render, monkeypatch):
    from exactcut.retire_candidate import retire
    _, parent, child, job = stale_render
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
    with pytest.raises(ValueError, match='维护'):
        retire(parent.name, child.name, job['id'], apply=True)
    assert read_json(web.JOBS / (job['id'] + '.json')) == job
