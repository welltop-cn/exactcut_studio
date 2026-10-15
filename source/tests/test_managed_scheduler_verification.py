"""Managed verification handoffs and scheduler callbacks; remote clients only are stubs."""
import copy
import multiprocessing
import os
import queue
import sys
from unittest.mock import Mock

import pytest
from exactcut import workflow as w, webserver as web, publish, stage_results
from exactcut import material_workers as worker
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace
from test_material_versions import enable_fixture_delivery, token
from test_managed_verification_retry import pending_verification


@pytest.fixture
def verification(workspace, monkeypatch):
    monkeypatch.setattr(web, 'ROOT', w.ROOT)
    monkeypatch.setattr(web, 'JOBS', w.ROOT / '.web-state/jobs')
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    m, parent, child, rid, job_file, approve = pending_verification()
    # The historical uncertain upload never ran in this fixture.
    for file in web.JOBS.glob('*.json'):
        job = read_json(file)
        if job.get('action') != 'verify-delivery':
            job['status'] = 'unverified'; write_json_atomic(file, job)
    feishu, cf = Mock(), Mock()
    feishu.get_record.return_value = {'record_id': 'record-1', 'fields': {
        'OSS_URL': {'link': 'https://fixture.invalid/video'}, 'ADS素材ID': 'material-1', '状态': '已完成', '视频ID': 'video-1'}}
    cf.sync_status.return_value = {'facebook_bm_sync_status': 'ready', 'facebook_bm_video_id': 'video-1'}
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    monkeypatch.setattr(publish, 'CfAdsClient', lambda: cf)
    oss = Mock(side_effect=AssertionError('OSS writes forbidden'))
    monkeypatch.setattr(publish.OssPublisher, 'upload', oss)
    for client, names in ((feishu, ('create_or_reuse', 'update')), (cf, ('ingest', 'resync'))):
        for name in names: getattr(client, name).side_effect = AssertionError('external writes forbidden')
    did = next(iter(m.read(parent)['deliveries']))

    def run(file, failure=False):
        job = read_json(file); job.update(status='running', attempt=1, process_token=token())
        job['input_fingerprint'] = stage_results.fingerprint(job, w.BATCHES, w.ROOT)
        web.save(job)
        feishu.get_record.side_effect = TimeoutError('fixture read timeout') if failure else None
        with monkeypatch.context() as patcher:
            patcher.setitem(sys._xoptions, 'exactcut_process_token', job['process_token'])
            code = stage_results.run_child(w.poll, child.name, rid)
        assert code == (2 if failure else 0)
        assert stage_results.scheduler_verified(job, web.JOBS, w.BATCHES, w.ROOT)
        web.finish_job(job, code, scheduler=True)
        m.recover_outbox()
        return job

    yield m, parent, child, rid, job_file, approve, run, feishu, cf, did
    feishu.create_or_reuse.assert_not_called(); feishu.update.assert_not_called()
    cf.ingest.assert_not_called(); cf.resync.assert_not_called(); oss.assert_not_called()


def retry_to_ready(fixture):
    m, parent, child, rid, job_file, approve, run, feishu, cf, did = fixture
    old = run(job_file, failure=True)
    state = approve(); m.recover_outbox()
    event = [e for e in state['outbox'].values() if e['kind'] == 'verification'][-1]
    new = run(web.JOBS / (event['job_id'] + '.json'))
    assert m.read(parent)['deliveries'][did]['status'] == 'fbbm_ready'
    assert feishu.get_record.call_count == 2
    cf.sync_status.assert_not_called()
    return old, new


def test_late_scheduler_finalizer_cannot_regress_ready(verification):
    old, new = retry_to_ready(verification)
    m, parent, child, rid, job_file, _, _, _, _, did = verification
    publication = read_json(child / 'publications.json')
    parent_state = m.read(parent)
    job_state = read_json(job_file)
    assert stage_results.scheduler_verified(old, web.JOBS, w.BATCHES, w.ROOT)['status'] == 'unverified'
    web.finish_job(copy.deepcopy(old), 2, scheduler=True)
    m.recover_outbox()
    assert read_json(child / 'publications.json') == publication
    assert m.read(parent) == parent_state
    assert read_json(job_file) == job_state


def test_callback_from_replaced_attempt_cannot_overwrite_durable_job(verification):
    m, parent, child, rid, job_file, _, run, _, _, _ = verification
    old = run(job_file)
    replacement = {**read_json(job_file), 'attempt': 2, 'process_token': token(), 'status': 'queued'}
    write_json_atomic(job_file, replacement)
    before = read_json(child / 'publications.json')
    web.finish_job(old, 0, scheduler=True)
    assert read_json(job_file) == replacement
    assert read_json(child / 'publications.json') == before


@pytest.mark.parametrize('window', ['before_publication', 'after_publication', 'after_job'])
def test_managed_verification_handoff_survives_process_exit(verification, window):
    m, parent, child, rid, job_file, approve, run, feishu, cf, did = verification
    run(job_file, failure=True)
    state = approve()
    event = [e for e in state['outbox'].values() if e['kind'] == 'verification'][-1]
    target = web.JOBS / (event['job_id'] + '.json')
    def crash():
        original_queue, original_write = w.queue_verify_delivery, m.durable_write
        def queue_fault(*args, **kwargs):
            if window == 'before_publication': os._exit(97)
            value = original_queue(*args, **kwargs)
            if window == 'after_publication': os._exit(97)
            return value
        def write_fault(file, value):
            original_write(file, value)
            if window == 'after_job' and file == target: os._exit(97)
        w.queue_verify_delivery, m.durable_write = queue_fault, write_fault
        worker.recover_verification(parent, event)
        os._exit(98)
    process = multiprocessing.get_context('fork').Process(target=crash)
    process.start(); process.join(10)
    if process.is_alive(): process.kill(); process.join(); pytest.fail('handoff hung')
    assert process.exitcode == 97
    for _ in range(3):
        m.recover_outbox(); w.recover_verification_queue(list(web.stored_jobs())); web.recover_jobs()
    current = [j for j in web.stored_jobs() if j.get('action') == 'verify-delivery' and j['status'] == 'queued']
    assert [j['id'] for j in current] == [event['job_id']]
    run(target)
    assert m.read(parent)['deliveries'][did]['status'] == 'fbbm_ready'
    assert feishu.get_record.call_count == 2
    cf.sync_status.assert_not_called()


def test_unsaved_admission_failure_still_finishes_current_verification(verification):
    m, parent, child, rid, job_file, _, _, _, _, did = verification
    job = read_json(job_file)
    # worker_tick increments in memory before opening its log and saving launch identity.
    job['attempt'] = job.get('attempt', 0) + 1
    web.finish_job(job, -1, 'fixture log open failed', local_failure=True, scheduler=True)
    m.recover_outbox()
    assert read_json(job_file)['status'] == 'unverified'
    assert read_json(child / 'publications.json')[rid]['status'] == 'unverified'
    assert m.read(parent)['deliveries'][did]['status'] == 'unverified'


def test_local_admission_failure_cannot_bypass_superseded_event(verification):
    m, parent, child, rid, job_file, approve, run, _, _, _ = verification
    run(job_file, failure=True)
    approve()  # Supersede the old event before exercising the admission exception.
    disk = read_json(job_file)
    disk['status'] = 'queued'
    write_json_atomic(job_file, disk)
    callback = {**disk, 'attempt': disk.get('attempt', 0) + 1}
    publication = read_json(child / 'publications.json')
    parent_state = m.read(parent)
    web.finish_job(callback, -1, 'late local launch failure', local_failure=True, scheduler=True)
    assert read_json(child / 'publications.json') == publication
    assert read_json(job_file) == disk
    assert m.read(parent) == parent_state
