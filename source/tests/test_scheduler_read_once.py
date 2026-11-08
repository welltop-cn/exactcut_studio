"""Outbox recovery shares one job snapshot across all managed parents."""
from collections import Counter

import pytest

from exactcut import material_workers as worker, workflow as w
from exactcut.util import read_json, write_json_atomic
from test_material_versions import candidate_job, enrolled, token
from test_workflow import workspace


@pytest.mark.parametrize('bad_job', [None, 'invalid-json', 'unreadable'])
def test_recover_outbox_reads_jobs_once(workspace, monkeypatch, bad_job):
    job_files = []
    candidates = []
    for _ in range(3):
        m, parent, state = enrolled()
        state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()})
        _, child, event, job = candidate_job(parent, state)
        job.update(status='queued')
        file = w.ROOT / '.web-state/jobs' / (job['id'] + '.json')
        write_json_atomic(file, job)
        job_files.append(file)
        candidates.append((parent, child, event))

    jobs = w.ROOT / '.web-state/jobs'
    broken = jobs / 'broken.json'
    if bad_job:
        broken.write_text('{invalid json' if bad_job == 'invalid-json' else '{}')
        job_files.append(broken)

    reads = Counter()

    def counted_read(file):
        if file.parent == jobs:
            reads[file] += 1
            if file == broken and bad_job == 'unreadable':
                raise OSError('fixture read failure')
        return read_json(file)

    monkeypatch.setattr(worker, 'read_json', counted_read)
    if bad_job == 'unreadable':
        with pytest.raises(OSError, match='fixture read failure'):
            m.recover_outbox()
        assert not (jobs.parent / 'job-quarantine').exists()
        return
    m.recover_outbox()

    assert reads == Counter({file: 1 for file in job_files})
    for file in job_files:
        if file != broken:
            assert read_json(file)['status'] == 'interrupted'
    for parent, child, event in candidates:
        latest = m.read(parent)
        assert latest['versions'][event['version_id']]['child_id'] != child.name


def test_cancel_invalid_children_uses_snapshot_file_list(workspace, monkeypatch):
    from exactcut import batch_control as control

    m, parent, state = enrolled()
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()})
    _, child, _, job = candidate_job(parent, state)
    m.control(parent, 'delete', {'revision': state['revision'], 'mutation_id': token()})
    jobs = w.ROOT / '.web-state/jobs'
    file = jobs / (job['id'] + '.json')
    calls = []
    monkeypatch.setattr(control, 'signal_children', lambda *args: None)
    monkeypatch.setattr(worker, 'cancel_queued_candidate',
                        lambda parent, event, child, *, job_files: calls.append(job_files))

    def no_job_read(file):
        assert file.parent != jobs
        return read_json(file)

    monkeypatch.setattr(worker, 'read_json', no_job_read)
    worker.cancel_invalid_children(parent, snapshot=[(file, job)])
    assert calls == [[file]]


def test_managed_parent_outbox_filters_invalid_structure_before_grouping(workspace):
    m, parent, state = enrolled()
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()})
    _, child, event, job = candidate_job(parent, state)
    jobs = w.ROOT / '.web-state/jobs'
    bad_file = jobs / ('a' * 32 + '.json')
    write_json_atomic(bad_file, {'id': bad_file.stem, 'batch_id': [], 'status': 'queued'})
    m.recover_outbox()
    assert read_json(bad_file)['quarantine']
    assert m.read(parent)['outbox']
