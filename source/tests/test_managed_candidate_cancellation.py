"""Superseded hidden candidates must not retain queued scheduler blockers."""
import pytest
from exactcut import workflow as w, webserver as web, material_workers as worker
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace
from test_round1_integration import chain
from test_linux_release import ops
from test_material_versions import changed_candidate, token

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('status', ['queued', 'waiting_resource'])
def test_deleted_restored_candidate_cancels_derived_render_and_runtime(chain, status, ops, tmp_path):
    m, parent, state, old_current = changed_candidate()
    m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'candidate')
    child = w.BATCHES / event['child_id']
    fetch = read_json(web.JOBS / (event['job_id'] + '.json'))
    # Use the real fetch and successor fanout, stop before render admission.
    assert w.fetch(child.name) == 0
    fetch['status'] = 'completed'; web.save(fetch); web.advance_stage(fetch)
    render = next(j for j in web.stored_jobs() if j['batch_id'] == child.name and j['phase'] == 'render')
    assert not render.get('attempt')
    render['status'] = status; web.save(render)
    state = m.control(parent, 'delete', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
    state = m.restore(parent, None, {'revision': state['revision'], 'mutation_id': token()})
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()})
    next_candidate = state['assets'][event['asset_id']]['candidate_version']
    chain.tick(); chain.tick()
    cancelled = read_json(web.JOBS / (render['id'] + '.json'))
    assert cancelled['status'] == 'interrupted'
    assert cancelled.get('finished')
    assert read_json(child / 'runtime.json')['status'] not in w.ACTIVE
    assert not read_json(child / 'runtime.json').get('executor_running')
    assert m.read(parent)['assets'][event['asset_id']]['current_version'] == old_current
    assert m.read(parent)['assets'][event['asset_id']]['candidate_version'] == next_candidate
    assert not any(j['batch_id'] == child.name for j, _, _ in chain.owned.values())
    assert not chain.render_calls
    ops_root = tmp_path / 'ops-fixture'; ops_root.mkdir()
    (ops_root / 'data').symlink_to(w.ROOT, target_is_directory=True)
    assert not any(render['id'] in path or event['job_id'] in path for path in ops.phase_blockers(ops_root))
    assert not any(child.name in path or render['id'] in path for path in ops.active_records(ops_root))


def test_cancellation_does_not_change_healthy_queued_candidate(chain):
    m, parent, state, _ = changed_candidate(); m.recover_outbox()
    state = m.read(parent)
    before = {f: f.read_bytes() for f in web.JOBS.glob('*.json')}
    worker.cancel_invalid_children(parent)
    assert all(f.read_bytes() == value for f, value in before.items())
    assert m.read(parent) == state


def test_unrecorded_live_launch_is_not_retired(chain):
    from exactcut.runtime import instance_marker
    m, parent, state, _ = changed_candidate(); m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'candidate')
    child = w.BATCHES / event['child_id']
    job = read_json(web.JOBS / (event['job_id'] + '.json'))
    job.update(status='waiting_resource', attempt=1, process_token=token(), instance=instance_marker())
    web.save(job)
    chain.table.rows[22222] = {'pgid': 22222, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + job['process_token']]}
    m.control(parent, 'delete', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
    worker.cancel_invalid_children(parent)
    assert read_json(web.JOBS / (job['id'] + '.json')) == job


def test_cancellation_recovery_repairs_runtime_after_job_write_crash(chain, monkeypatch):
    from exactcut import storage
    m, parent, state, _ = changed_candidate(); m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'candidate')
    child = w.BATCHES / event['child_id']
    m.control(parent, 'delete', {'revision': m.read(parent)['revision'], 'mutation_id': token()})
    with monkeypatch.context() as patcher:
        patcher.setattr(storage, 'release', lambda *args: (_ for _ in ()).throw(OSError('crash after job write')))
        with pytest.raises(OSError): worker.cancel_invalid_children(parent)
    assert read_json(web.JOBS / (event['job_id'] + '.json'))['status'] == 'interrupted'
    worker.cancel_invalid_children(parent)
    assert read_json(child / 'runtime.json')['status'] not in w.ACTIVE


def test_cancellation_does_not_touch_healthy_running_candidate(chain):
    m, parent, state, _ = changed_candidate(); chain.tick()
    job, _, _ = chain.one('fetch')
    before = read_json(web.JOBS / (job['id'] + '.json'))
    worker.cancel_invalid_children(parent)
    assert read_json(web.JOBS / (job['id'] + '.json')) == before
    assert not chain.signals


def test_superseded_candidate_cancellation_preserves_delivery(chain, monkeypatch):
    import copy
    monkeypatch.setattr(w.material_versions, 'delivery_enabled', lambda: True)
    m, parent, state, old = changed_candidate()
    rid = state['draft']['rows'][0]['request_id']
    state = m.queue_delivery(parent, {'revision': state['revision'], 'mutation_id': token(),
        'request_id': rid, 'version_id': old, 'sha256': state['versions'][old]['record']['output_sha256']})
    m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'delivery')
    child = w.BATCHES / event['child_id']; job_file = web.JOBS / (event['job_id'] + '.json')
    job_before, runtime_before = job_file.read_bytes(), (child / 'runtime.json').read_bytes()
    state = m.read(parent); draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] += ' edited again'
    m.save_draft(parent, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    worker.cancel_invalid_children(parent)
    assert job_file.read_bytes() == job_before
    assert (child / 'runtime.json').read_bytes() == runtime_before


def test_historical_invalid_children_scan_job_directory_once(chain, monkeypatch):
    from pathlib import Path
    import copy
    m, parent, state, _ = changed_candidate(); m.recover_outbox()
    first = state['assets'][state['draft']['rows'][0]['request_id']]['candidate_version']
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] += ' version two'
    state = m.save_draft(parent, {'revision': m.read(parent)['revision'], 'mutation_id': token(), 'draft': draft})
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()}); m.recover_outbox()
    second = state['assets'][state['draft']['rows'][0]['request_id']]['candidate_version']
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] += ' version three'
    state = m.save_draft(parent, {'revision': m.read(parent)['revision'], 'mutation_id': token(), 'draft': draft})
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()}); m.recover_outbox()
    current = state['assets'][state['draft']['rows'][0]['request_id']]['candidate_version']
    assert len({first, second, current}) == 3
    worker.cancel_invalid_children(parent)  # First-time runtime cleanup is complete.
    healthy_child = m.read(parent)['versions'][current]['child_id']
    healthy = {f: f.read_bytes() for f in web.JOBS.glob('*.json') if read_json(f)['batch_id'] == healthy_child}
    original = Path.glob
    scans = []
    def glob(path, pattern):
        if path == web.JOBS and pattern == '*.json': scans.append(path)
        return original(path, pattern)
    monkeypatch.setattr(Path, 'glob', glob)
    worker.cancel_invalid_children(parent)
    assert len(scans) == 1
    assert healthy and all(f.read_bytes() == content for f, content in healthy.items())
