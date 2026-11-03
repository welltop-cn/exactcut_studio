"""Read-only managed delivery retries use isolated files and no network calls."""
import pytest
from exactcut import workflow as w, material_workers as worker
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace
from test_material_versions import enrolled, token, enable_fixture_delivery

pytestmark = pytest.mark.integration


def pending_verification():
    m, parent, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']; vid = state['assets'][rid]['current_version']
    sha = state['versions'][vid]['record']['output_sha256']
    state = m.queue_delivery(parent, dict(revision=state['revision'], mutation_id=token(), request_id=rid, version_id=vid, sha256=sha))
    m.recover_outbox(); delivery = next(iter(state['deliveries'].values()))
    child = w.BATCHES / delivery['child_id']
    entry = {'status': 'unverified', 'approved_sha256': sha,
             'publication': {'sha256': sha, 'feishu_record_id': 'record-1', 'oss_url': 'https://fixture.invalid/video', 'oss_key': 'fixture/video.mp4'},
             'intent': {'status': 'pending', 'receipt': {'material_id': 'material-1'}}}
    write_json_atomic(child / 'publications.json', {rid: entry}); m.recover_outbox()
    def queue():
        return m.queue_verification(parent, dict(revision=m.read(parent)['revision'], mutation_id=token(), delivery_id=delivery['delivery_id']))
    queue(); m.recover_outbox()
    job_file = next(f for f in (w.ROOT / '.web-state/jobs').glob('*.json') if read_json(f).get('action') == 'verify-delivery')
    return m, parent, child, rid, job_file, queue


def test_failed_readonly_verification_can_be_explicitly_retried(workspace):
    m, parent, child, rid, job_file, queue = pending_verification()
    old = read_json(job_file); old['status'] = 'unverified'; write_json_atomic(job_file, old)
    entry = read_json(child / 'publications.json')[rid]
    entry.update(status='unverified', poll_started=1)
    entry['intent']['status'] = 'pending'
    write_json_atomic(child / 'publications.json', {rid: entry})
    state = queue(); m.recover_outbox()
    events = [e for e in state['outbox'].values() if e['kind'] == 'verification']
    new = events[-1]
    assert new['job_id'] != old['id']
    publication = read_json(child / 'publications.json')[rid]
    assert publication['verification_only'] == old['verification_only']
    assert publication['intent']['status'] == 'verified'
    assert publication['poll_started'] > 1
    assert publication['verification_queue']['job_id'] == new['job_id']
    assert read_json(w.ROOT / '.web-state/jobs' / (new['job_id'] + '.json'))['phase'] == 'poll'
    with pytest.raises(ValueError, match='核实'):
        worker.stage_enter(old)
    before = m.read(parent)
    worker.stage_finish(old, 2)
    assert m.read(parent) == before
    # Replaying old outbox events cannot recreate the old job or reset the new intent.
    job_file.unlink(); worker.recover_verification(parent, events[0])
    assert not job_file.exists()
    assert read_json(child / 'publications.json')[rid] == publication


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting_resource'])
def test_active_verification_cannot_be_duplicated(workspace, status):
    m, parent, child, rid, job_file, queue = pending_verification()
    job = read_json(job_file); job['status'] = status; write_json_atomic(job_file, job)
    with pytest.raises(ValueError, match='核实'): queue()
    assert len([e for e in m.read(parent)['outbox'].values() if e['kind'] == 'verification']) == 1


def test_missing_job_remains_an_active_outbox_handoff(workspace):
    m, parent, child, rid, job_file, queue = pending_verification()
    job_file.unlink()
    with pytest.raises(ValueError, match='核实'): queue()


def test_terminal_job_with_live_process_cannot_be_replaced(workspace, monkeypatch):
    from exactcut import batch_control
    m, parent, child, rid, job_file, queue = pending_verification()
    job = read_json(job_file); job.update(status='unverified', pid=12345)
    write_json_atomic(job_file, job)
    monkeypatch.setattr(batch_control, 'belongs', lambda job: True)
    monkeypatch.setattr(batch_control, 'child_state', lambda job: 'S')
    with pytest.raises(ValueError, match='核实'): queue()


def test_superseded_finalizer_cannot_merge_after_retry_approval(workspace):
    m, parent, child, rid, job_file, queue = pending_verification()
    old = read_json(job_file); old['status'] = 'failed'; write_json_atomic(job_file, old)
    state = queue()
    delivery_event = next(e for e in state['outbox'].values() if e['kind'] == 'delivery')
    before = m.read(parent)
    worker._merge_delivery(parent, delivery_event, {'status': 'fbbm_ready'}, verification_job_id=old['id'])
    assert m.read(parent) == before


def test_verification_hash_does_not_hold_parent_lock(workspace, monkeypatch):
    import fcntl
    from exactcut import stage_results
    original = stage_results.digest_evidence
    locked = []
    def inspect(video):
        for file in (w.BATCHES.parent / 'media-locks').glob('*.lock'):
            with file.open('a') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    locked.append(file.name)
        return original(video)
    monkeypatch.setattr(stage_results, 'digest_evidence', inspect)
    pending_verification()
    assert not locked
