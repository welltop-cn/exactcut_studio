"""Prefetch resource waits remain recoverable while the editable plan changes."""
import sys

import pytest

from exactcut import stage_results, webserver as web, workflow as w
from exactcut.util import read_json
from test_source_prefetch import console, draft_with, jobs, prefetch
from test_web_base_path import app


@pytest.mark.parametrize('completion', ['exit', 'recovery'])
@pytest.mark.parametrize('desired', [('EP1', 'EP2', 'EP3', 'EP4', 'EP5', 'EP6'), ('EP2',)])
def test_editing_plan_during_prefetch_resource_wait_requeues_without_changing_draft(console, monkeypatch, desired, completion):
    assert prefetch(console, draft_with('EP1', 'EP2', 'EP3'))[0] == 200
    job = jobs()[0]
    path = w.folder(job['batch_id'])
    original_draft = (path / 'draft.json').read_bytes()
    original_runtime = (path / 'runtime.json').read_bytes()
    token = 'b' * 32
    job.update(status='running', attempt=1, process_token=token,
               input_fingerprint=stage_results.fingerprint(job, w.BATCHES, w.ROOT))
    web.save(job)
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', token)

    def prepare_until_resource_wait():
        # This is a real edit through the sources API during the current child.
        assert prefetch(console, draft_with(*desired))[0] == 200
        raise stage_results.ResourceDeferred(12345678900)

    assert stage_results.run_child(prepare_until_resource_wait) == 75
    if completion == 'exit':
        web.finish_job(job, 75, scheduler=True)
    else:
        web.recover_jobs(scheduler=True)
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['status'] == 'queued'
    assert saved['not_before'] == 12345678900
    assert 'process_token' not in saved
    assert (path / 'draft.json').read_bytes() == original_draft
    assert (path / 'runtime.json').read_bytes() == original_runtime


def test_cancelled_prefetch_wait_retries_as_skipped_without_downloads(console, monkeypatch):
    from exactcut import source_prefetch, source_cache
    assert prefetch(console, draft_with('EP1', 'EP2'))[0] == 200
    job = jobs()[0]
    path = w.folder(job['batch_id'])
    original_runtime = (path / 'runtime.json').read_bytes()
    token = 'c' * 32
    job.update(status='running', attempt=1, process_token=token,
               input_fingerprint=stage_results.fingerprint(job, w.BATCHES, w.ROOT))
    web.save(job)
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', token)

    def cancel_before_resource_wait():
        assert prefetch(console, draft_with('EP1', 'EP2'), cancel=True)[0] == 200
        raise stage_results.ResourceDeferred(12345678900)

    assert stage_results.run_child(cancel_before_resource_wait) == 75
    web.finish_job(job, 75, scheduler=True)
    resumed = read_json(web.JOBS / (job['id'] + '.json'))
    assert resumed['status'] == 'queued'
    resumed.update(status='running', attempt=2, process_token=token,
                   input_fingerprint=stage_results.fingerprint(resumed, w.BATCHES, w.ROOT))
    web.save(resumed)
    monkeypatch.setattr(source_cache, 'ensure', lambda *args, **kwargs: pytest.fail('cancelled episodes must not download'))
    assert stage_results.run_child(source_prefetch.run, job['batch_id'], job['id']) == 0
    web.finish_job(resumed, 0, scheduler=True)
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'completed'
    result = read_json(source_prefetch.result_path(path, job['id']))
    assert [result['episodes'][ep]['status'] for ep in ['EP1', 'EP2']] == ['skipped', 'skipped']
    assert (path / 'runtime.json').read_bytes() == original_runtime


@pytest.mark.parametrize('changed', [{'project': '101574'}, {'episodes': ['EP9']}])
def test_prefetch_evidence_still_rejects_changed_job_arguments(console, changed):
    assert prefetch(console, draft_with('EP1'))[0] == 200
    job = jobs()[0]
    job.update(attempt=1, input_fingerprint=stage_results.fingerprint(job, w.BATCHES, w.ROOT))
    stage_results.complete(job, web.JOBS, w.BATCHES, w.ROOT, 75,
                           status='waiting_resource', not_before=12345678900)
    altered = {**job, **changed}
    assert stage_results.verified(altered, web.JOBS, w.BATCHES, w.ROOT) is None
    assert stage_results.scheduler_verified(altered, web.JOBS, w.BATCHES, w.ROOT) is None
