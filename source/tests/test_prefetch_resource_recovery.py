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


@pytest.fixture
def previous_version_wait(console):
    from exactcut import source_prefetch
    from exactcut.util import json_sha256, write_json_atomic
    assert prefetch(console, draft_with('EP1', 'EP2'))[0] == 200
    job = jobs()[0]
    path = w.folder(job['batch_id'])
    original_runtime = (path / 'runtime.json').read_bytes()
    # Literal schema emitted by main 9002646, before the fingerprint change.
    old_fingerprint = json_sha256({'phase': None, 'request_id': None, 'config': {},
                                  'source-prefetch.json': read_json(path / 'source-prefetch.json')})
    job.update(status='running', attempt=1, process_token='e' * 32,
               input_fingerprint=old_fingerprint)
    web.save(job)
    write_json_atomic(source_prefetch.result_path(path, job['id']),
                      {'status': 'waiting_resource', 'episodes': {}})
    stage_results.complete(job, web.JOBS, w.BATCHES, w.ROOT, 75,
                           status='waiting_resource', not_before=12345678900)
    return job, path, original_runtime


@pytest.mark.parametrize('completion', ['exit', 'recovery'])
def test_recovery_preserves_resource_wait_written_by_previous_version(previous_version_wait, completion):
    job, path, original_runtime = previous_version_wait
    old_fingerprint = job['input_fingerprint']
    assert stage_results.verified(job, web.JOBS, w.BATCHES, w.ROOT)['status'] == 'waiting_resource'
    if completion == 'exit':
        web.finish_job(job, 75, scheduler=True)
    else:
        web.recover_jobs(scheduler=True)
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['status'] == 'queued'
    assert saved['not_before'] == 12345678900
    assert 'input_fingerprint' not in saved
    assert stage_results.scheduler_fingerprint(saved, w.BATCHES, w.ROOT) != old_fingerprint
    assert (path / 'runtime.json').read_bytes() == original_runtime


@pytest.mark.parametrize('changed', [{'project': '101574'}, {'episodes': ['EP9']},
                                    {'episodes': None}, {'episodes': []}])
def test_previous_version_wait_rejects_job_outside_current_plan(previous_version_wait, changed):
    job, _, _ = previous_version_wait
    altered = {**job, **changed}
    assert stage_results.verified(altered, web.JOBS, w.BATCHES, w.ROOT) is None
    assert stage_results.scheduler_verified(altered, web.JOBS, w.BATCHES, w.ROOT) is None


@pytest.mark.parametrize('changed', [
    {'status': 'done', 'exit_code': 0}, {'version': 2},
    {'publication': {}}, {'source_evidence': {'EP1': None}},
    {'evidence_sha256': 'invalid'}, {'evidence_sha256': None},
    {'not_before': None}, {'not_before': float('inf')}, {'not_before': True},
])
def test_previous_version_fallback_rejects_non_wait_or_invalid_evidence(previous_version_wait, changed):
    from exactcut.util import json_sha256, write_json_atomic
    job, _, _ = previous_version_wait
    target = stage_results.result_path(job, web.JOBS)
    result = {**read_json(target), **changed}
    if 'evidence_sha256' not in changed:
        result['evidence_sha256'] = json_sha256({key: value for key, value in result.items() if key != 'evidence_sha256'})
    write_json_atomic(target, result)
    assert stage_results.verified(job, web.JOBS, w.BATCHES, w.ROOT) is None
    assert stage_results.scheduler_verified(job, web.JOBS, w.BATCHES, w.ROOT) is None


def test_previous_version_fallback_rejects_even_intact_artifacts(previous_version_wait):
    from exactcut.util import json_sha256, write_json_atomic
    job, path, _ = previous_version_wait
    target = stage_results.result_path(job, web.JOBS)
    result = read_json(target)
    result['artifacts'] = [stage_results.digest_evidence(path / 'draft.json')]
    result['evidence_sha256'] = json_sha256({key: value for key, value in result.items() if key != 'evidence_sha256'})
    write_json_atomic(target, result)
    assert stage_results.verified(job, web.JOBS, w.BATCHES, w.ROOT) is None
    assert stage_results.scheduler_verified(job, web.JOBS, w.BATCHES, w.ROOT) is None


@pytest.mark.parametrize('desired', [None, [], {'drama_id': '101573', 'episodes': None}])
def test_previous_version_wait_rejects_missing_or_malformed_plan(previous_version_wait, desired):
    from exactcut.util import write_json_atomic
    job, path, _ = previous_version_wait
    plan = path / 'source-prefetch.json'
    if desired is None:
        plan.unlink()
    else:
        write_json_atomic(plan, desired)
    assert stage_results.verified(job, web.JOBS, w.BATCHES, w.ROOT) is None
    assert stage_results.scheduler_verified(job, web.JOBS, w.BATCHES, w.ROOT) is None


@pytest.mark.integration
def test_confirmation_during_prefetch_wait_keeps_production_state(console, monkeypatch):
    from exactcut import source_prefetch, source_cache
    draft = {**draft_with('EP1'), 'business_date': '20261104', 'author': '测试编导'}
    assert prefetch(console, draft)[0] == 200
    job = jobs()[0]
    path = w.folder(job['batch_id'])
    saved_draft = w.save_draft(job['batch_id'], {'revision': 1, 'draft': draft})
    assert saved_draft['errors'] == []
    token = 'f' * 32
    job.update(status='running', attempt=1, process_token=token,
               input_fingerprint=stage_results.fingerprint(job, w.BATCHES, w.ROOT))
    web.save(job)
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', token)

    def resource_wait():
        raise stage_results.ResourceDeferred(12345678900)

    assert stage_results.run_child(resource_wait) == 75
    web.finish_job(job, 75, scheduler=True)
    command = w.start_batch(job['batch_id'], saved_draft['revision'])
    assert 'produce' in command
    assert w.runtime(path)['status'] == 'queued'
    confirmed = (path / 'confirmed.json').read_bytes()
    runtime = (path / 'runtime.json').read_bytes()
    resumed = read_json(web.JOBS / (job['id'] + '.json'))
    resumed.update(status='running', attempt=2, process_token=token,
                   input_fingerprint=stage_results.fingerprint(resumed, w.BATCHES, w.ROOT))
    web.save(resumed)
    monkeypatch.setattr(source_cache, 'ensure', lambda *args, **kwargs: pytest.fail('confirmed production owns source preparation'))
    assert stage_results.run_child(source_prefetch.run, job['batch_id'], job['id']) == 0
    web.finish_job(resumed, 0, scheduler=True)
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'completed'
    assert read_json(source_prefetch.result_path(path, job['id']))['episodes']['EP1']['status'] == 'skipped'
    assert (path / 'confirmed.json').read_bytes() == confirmed
    assert (path / 'runtime.json').read_bytes() == runtime
