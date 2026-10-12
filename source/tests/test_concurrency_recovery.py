"""Crash windows and resource accounting use fixtures only."""
from unittest.mock import Mock, patch
import pytest
from exactcut import workflow, webserver as web, storage
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued, close_logs
from test_workflow import workspace, imported, complete_asset


def test_dead_stage_requeues_without_result(runner):
    job = queued('a', 'render'); job.update(phase='render', status='running', attempt=1); web.save(job)
    web.recover_jobs()
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'queued'


def test_completed_result_recovers_successor_gap(runner):
    from exactcut import stage_results
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'queued', 'events': []})
    job = queued('a', 'fetch', batch=batch); job.update(action='start', phase='fetch', status='completed', attempt=1)
    stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0); web.save(job)
    web.recover_jobs(); web.recover_jobs()
    assert len([j for j in web.stored_jobs() if j.get('phase') == 'render']) == 1


def test_result_rejects_changed_inputs_and_artifacts(runner):
    from exactcut import stage_results
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'source-prefetch.json', {'drama_id': '123456', 'episodes': []})
    video = path / 'production/videos/x.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'original')
    job = queued('a', 'render', batch=batch); job.update(phase='render', attempt=1)
    stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0)
    assert stage_results.verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)
    video.write_bytes(b'tampered')
    assert not stage_results.verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)
    video.write_bytes(b'original'); write_json_atomic(path / 'source-prefetch.json', {'drama_id': '123456', 'episodes': ['changed']})
    assert not stage_results.verified(job, web.JOBS, workflow.BATCHES, workflow.ROOT)


def test_recovery_runs_with_owned(runner):
    queued('a', 'render'); pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        with patch.object(web, 'recover_jobs', wraps=web.recover_jobs) as recover:
            web.worker_tick(pending, owned); assert recover.called
        assert len(owned) == 1
    finally: close_logs(owned)


def test_reservations_merge_same_device_and_prevent_oversell(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'policy', lambda: {'reserve_bytes': 10})
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: Mock(free=100))
    jobs = tmp_path / 'jobs'; jobs.mkdir(); a = tmp_path / 'a'; a.mkdir(); b = tmp_path / 'b'; b.mkdir()
    assert storage.reserve('first', {str(a): 35, str(b): 35}, jobs)
    assert not storage.reserve('second', {str(a): 25}, jobs)
    storage.release('first', jobs)
    assert storage.reserve('second', {str(a): 25}, jobs)


def test_pending_intent_blocks_upload_and_manual_replay(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    entry = read_json(path / 'publications.json')[rid]
    entry.update(status='unverified', intent={'status': 'pending', 'phase': 'upload'})
    workflow.store_publication(path, rid, entry)
    with patch('exactcut.publish.publish_to_queue') as remote:
        assert workflow.upload(batch['id'], rid) == 2; remote.assert_not_called()
    with pytest.raises(ValueError, match='核实'): workflow.queue_publish(batch['id'], rid, record['output_sha256'])


@pytest.mark.parametrize('phase', ['upload', 'poll'])
def test_ambiguous_delivery_is_unverified_and_never_replayed(workspace, phase):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    if phase == 'poll':
        entry = read_json(path / 'publications.json')[rid]; entry['publication'] = {'feishu_record_id': 'fixture-record'}
        workflow.store_publication(path, rid, entry)
    operation = 'publish_to_queue' if phase == 'upload' else 'sync_record_once'
    def ambiguous(*args, **kwargs):
        intent = read_json(path / 'publications.json')[rid]['intent']
        assert intent['status'] == 'pending' and intent['approved_sha256'] == record['output_sha256']
        raise TimeoutError('remote accepted; reply lost')
    with patch('exactcut.publish.' + operation, side_effect=ambiguous) as remote:
        assert getattr(workflow, phase)(batch['id'], rid) == 2
        assert getattr(workflow, phase)(batch['id'], rid) == 2
        assert remote.call_count == 1
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'


def test_owned_killed_stage_requeues_last_unfinished_stage(runner):
    job = queued('a', 'render'); job.update(phase='render', attempt=1, status='running'); web.save(job)
    web.finish_job(job, -9)
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'queued'


def test_dead_queued_admission_result_advances_without_reexecution(runner):
    from exactcut import stage_results
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'queued', 'events': []})
    job = queued('a', 'fetch', batch=batch); job.update(action='start', phase='fetch', attempt=1, process_token='b' * 32)
    stage_results.complete(job, web.JOBS, workflow.BATCHES, workflow.ROOT, 0); web.save(job)
    web.recover_jobs()
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'completed'
    assert len([j for j in web.stored_jobs() if j.get('phase') == 'render']) == 1


def test_guarded_cf_requests_are_single_attempt_without_hidden_wait(monkeypatch):
    from exactcut.publish import CfAdsClient, guarded_delivery
    client = CfAdsClient.__new__(CfAdsClient); client.token = 'fixture'; client.base = 'https://fixture.invalid'; client.timeout = 1
    response = Mock(); response.json.return_value = {'code': 500}
    with patch('exactcut.publish.requests.request', return_value=response) as request, patch('exactcut.publish.time.sleep', side_effect=AssertionError('must not wait')):
        with guarded_delivery():
            client.request('POST', '/api/v1/material/ingest_and_sync')
            client.request('GET', '/api/v1/material/x/sync_status')
        assert request.call_count == 2


def test_reservations_rebuild_admission_and_release_launch_failure(runner, monkeypatch):
    job = queued('a', 'render'); job['admission'] = {'allocations': {'123': {'path': str(runner), 'bytes': 100}}}; web.save(job)
    storage.rebuild_reservations(web.JOBS, {job['id']})
    assert read_json(web.JOBS.parent / 'reservations.json')['jobs'][job['id']][str(runner.stat().st_dev)]['bytes'] == 100
    monkeypatch.setattr(web.subprocess, 'Popen', Mock(side_effect=OSError('launch failed')))
    web.worker_tick([], {})
    assert not read_json(web.JOBS.parent / 'reservations.json')['jobs']


def test_pending_intent_after_process_death_becomes_unverified(workspace, monkeypatch):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    entry = read_json(path / 'publications.json')[rid]; entry.update(status='uploading', intent={'phase': 'upload', 'status': 'pending'})
    workflow.store_publication(path, rid, entry)
    monkeypatch.setattr(web, 'JOBS', workspace / 'jobs'); web.JOBS.mkdir()
    job = dict(id='a' * 32, batch_id=batch['id'], action='publish', phase='upload', lane='upload', request_id=rid, status='running')
    web.finish_job(job, -9)
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'unverified'


def test_require_space_does_not_charge_own_reservation_twice(tmp_path, monkeypatch):
    import sys
    from exactcut.runtime import data_root
    monkeypatch.setattr(storage, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(storage, 'policy', lambda: {'reserve_bytes': 10})
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: Mock(free=60))
    jobs = tmp_path / '.web-state/jobs'; jobs.mkdir(parents=True)
    write_json_atomic(jobs / 'first.json', {'id': 'first', 'process_token': 'fixture'})
    assert storage.reserve('first', {str(tmp_path): 40}, jobs)
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', 'fixture')
    storage.require_space(tmp_path, 40)


def test_unverified_upload_keeps_returned_receipt(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    receipt = {'feishu_record_id': 'known-record', 'oss_key': 'wrong-key', 'sha256': record['output_sha256']}
    with patch('exactcut.publish.publish_to_queue', return_value=receipt): assert workflow.upload(batch['id'], rid) == 2
    entry = read_json(path / 'publications.json')[rid]
    assert entry['status'] == 'unverified' and entry['intent']['receipt'] == receipt


def test_post_upload_remote_readback_checks_known_contract_and_keeps_id():
    from types import SimpleNamespace
    from pathlib import Path
    from exactcut import publish
    upload = {'oss_key': 'fixture-key', 'oss_url': 'https://fixture.invalid/video', 'sha256': 'approved'}
    feishu = Mock(); feishu.create_or_reuse.return_value = {'record_id': 'known-record', 'action': 'created'}
    feishu.get_record.return_value = {'record_id': 'known-record', 'fields': {'OSS_URL': {'link': 'https://wrong.invalid/video'}}}
    receipts = []
    with patch.object(publish.OssPublisher, 'upload', return_value=upload), patch.object(publish, 'FeishuClient', return_value=feishu):
        with publish.guarded_delivery(receipt=receipts.append), pytest.raises(publish.PublishError, match='核验'):
            publish.publish_to_queue(Path('fixture.mp4'), SimpleNamespace(author='fixture', drama_id='123456', business_date='20261009'), SimpleNamespace(seq='1'))
    assert receipts[-1]['feishu_record_id'] == 'known-record'


def test_guarded_oss_checks_remote_sha_after_write(tmp_path):
    from exactcut import publish
    video = tmp_path / '123456_fbbm_1_20261009.mp4'; video.write_bytes(b'fixture')
    publisher = publish.OssPublisher(); bucket = Mock(); bucket.object_exists.return_value = False
    bucket.put_object_from_file.return_value.status = 200
    bucket.head_object.return_value.headers = {'x-oss-meta-sha256': 'wrong'}
    with patch.object(publisher, 'connect', return_value=bucket), patch.object(publisher, 'key_for', return_value=('key', {})):
        with publish.guarded_delivery(), pytest.raises(publish.PublishError, match='校验'): publisher.upload(video)
    bucket.put_object_from_file.assert_called_once(); bucket.head_object.assert_called_once_with('key')


def test_legacy_publish_uses_same_delivery_insurance(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    with patch('exactcut.publish.publish_to_queue', side_effect=TimeoutError('lost reply')) as remote:
        assert workflow.publish(batch['id'], rid) == 2
        assert workflow.publish(batch['id'], rid) == 2
        assert remote.call_count == 1


def test_resource_deferred_result_preserves_stage_and_updates_runtime(workspace, monkeypatch):
    import sys
    from exactcut import stage_results
    batch = imported(); workflow.start_batch(batch['id'], batch['revision']); path = workflow.folder(batch['id'])
    jobs = workspace / '.web-state/jobs'; jobs.mkdir(parents=True); monkeypatch.setattr(web, 'JOBS', jobs)
    token = 'b' * 32; monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', token)
    job = {'id': 'a' * 32, 'batch_id': batch['id'], 'action': 'start', 'phase': 'fetch', 'attempt': 1, 'status': 'running', 'process_token': token}
    web.save(job)
    with patch.object(workflow, 'download', side_effect=stage_results.ResourceDeferred(12345678900)):
        assert stage_results.run_child(workflow.fetch, batch['id']) == 75
    web.finish_job(job, 75)
    assert job['status'] == 'queued' and job['not_before'] == 12345678900
    assert workflow.runtime(path)['status'] == 'queued' and not workflow.runtime(path)['executor_running']


def test_alive_process_is_adopted_while_owned_executor_is_alive(runner):
    from exactcut import batch_control as control
    from exactcut.runtime import instance_marker
    active = queued('a', 'render'); active.update(status='running'); web.save(active)
    other = queued('b', 'fetch'); other.update(status='running', pid=123, pgid=123, process_token='b' * 32); web.save(other)
    control.PROCESSES.rows[123] = {'pgid': 123, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'b' * 32]}
    web.recover_jobs(owned={active['id']: (active, Mock(poll=lambda: None), Mock())})
    saved = read_json(web.JOBS / (other['id'] + '.json'))
    assert saved['status'] == 'running' and saved['adopted_after_restart']
    assert read_json(web.JOBS / (active['id'] + '.json'))['status'] == 'running'


def test_legacy_same_project_serializes_with_render_capacity_two(runner, monkeypatch):
    from exactcut import concurrency
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 2, 'max_active_children': 2}})
    for key in ('a', 'b'):
        job = queued(key, 'render'); job.update(project='123456'); web.save(job)
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert len(owned) == 1 and len(pending) == 1
    finally: close_logs(owned)


@pytest.mark.parametrize('phase', ['upload', 'poll'])
@pytest.mark.parametrize('owned', [False, True])
def test_dead_delivery_before_intent_requeues_safely(runner, phase, owned):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'publishing', 'events': []})
    write_json_atomic(path / 'publications.json', {'R1': {'status': 'fbbm_queued', 'publication': {'feishu_record_id': 'original'}, 'intent': {'phase': 'poll', 'status': 'verified'}}})
    job = queued('a', phase, batch=batch); job.update(phase=phase, request_id='R1', attempt=1, status='running'); web.save(job)
    if owned: web.finish_job(job, -9)
    else: web.recover_jobs()
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['status'] == 'queued' and saved['phase'] == phase
    assert read_json(path / 'publications.json')['R1']['publication']['feishu_record_id'] == 'original'


def test_legacy_live_reservation_is_rebuilt_without_prior_ledger(runner, monkeypatch):
    job = queued('a', 'fetch'); job.update(status='running', episodes=['EP1']); web.save(job)
    monkeypatch.setattr(storage, 'job_budget', lambda j, batches, root: {str(runner): 70})
    storage.rebuild_reservations(web.JOBS, {job['id']}, workflow.BATCHES, workflow.ROOT)
    ledger = read_json(web.JOBS.parent / 'reservations.json')['jobs']
    assert sum(allocation['bytes'] for allocation in ledger[job['id']].values()) == 70
    assert read_json(web.JOBS / (job['id'] + '.json'))['admission']['rebuilt']


def test_reservation_rebuild_remaps_changed_devices(runner):
    job = queued('a', 'fetch')
    job['admission'] = {'paths': {str(runner): 40}, 'allocations': {'old-device': {'path': str(runner), 'bytes': 40}}}
    web.save(job); storage.rebuild_reservations(web.JOBS, {job['id']})
    allocations = read_json(web.JOBS.parent / 'reservations.json')['jobs'][job['id']]
    assert 'old-device' not in allocations and allocations[str(runner.stat().st_dev)]['bytes'] == 40


def test_launch_failure_preserves_attempt_sequence(runner, monkeypatch):
    job = queued('a', 'render'); job.update(phase='render', attempt=5); web.save(job)
    monkeypatch.setattr(web.subprocess, 'Popen', Mock(side_effect=OSError('cannot launch')))
    web.worker_tick([], {})
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['attempt'] == 6 and saved['status'] == 'failed'


@pytest.mark.parametrize('phase', ['render', 'upload', 'poll'])
def test_owned_sighup_requeues_safe_unfinished_stage(runner, phase):
    import signal
    job = queued('a', phase); job.update(phase=phase, attempt=1, status='running'); web.save(job)
    web.finish_job(job, -signal.SIGHUP)
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'queued'


def sync_fixture(workspace, monkeypatch, *, missing=(), timeout_after_ingest=False, material_status=None):
    """Exercise real FeishuClient.update filtering against an in-memory record."""
    from copy import deepcopy
    from exactcut import publish
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    entry = read_json(path / 'publications.json')[rid]; entry['publication'] = {'feishu_record_id': 'fixture-record'}
    workflow.store_publication(path, rid, entry)
    remote = {'状态': '待处理', 'OSS_URL': {'link': 'https://fixture.invalid/video'}, '重试次数': 0}
    feishu = publish.FeishuClient.__new__(publish.FeishuClient)
    feishu.app_token = 'fixture'; feishu.table_id = 'fixture'; feishu.token = 'fixture'
    schema = {name: {'field_name': name} for name in ('状态', 'ADS素材ID', '重试次数', '视频ID') if name not in missing}
    feishu.fields = lambda: schema
    feishu.get_record = lambda record_id: {'record_id': record_id, 'fields': deepcopy(remote)}
    cf = Mock(); cf.app_usage.return_value = {'is_available': True}; cf.ingest.side_effect = ['1', '2']
    cf.sync_status.return_value = material_status or {'facebook_bm_sync_status': 'processing'}
    def put(url, **kwargs):
        fields = kwargs['json']['fields']
        if timeout_after_ingest and 'ADS素材ID' in fields: raise TimeoutError('reply lost after ingest')
        remote.update(fields)
        response = Mock(ok=True); response.json.return_value = {'code': 0}
        return response
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    monkeypatch.setattr(publish, 'CfAdsClient', lambda: cf)
    monkeypatch.setattr(publish.requests, 'put', put)
    return batch['id'], rid, path, cf, remote


def test_missing_remote_material_column_cannot_cause_second_ingest(workspace, monkeypatch):
    batch, rid, path, cf, remote = sync_fixture(workspace, monkeypatch, missing=('ADS素材ID',))
    assert workflow.poll(batch, rid) == 2
    assert workflow.poll(batch, rid) == 2
    assert cf.ingest.call_count == 1 and 'ADS素材ID' not in remote
    entry = read_json(path / 'publications.json')[rid]
    assert entry['status'] == 'unverified' and entry['intent']['receipt']['material_id'] == '1'


def test_timeout_after_ingest_preserves_remote_material_receipt(workspace, monkeypatch):
    batch, rid, path, cf, remote = sync_fixture(workspace, monkeypatch, timeout_after_ingest=True)
    assert workflow.poll(batch, rid) == 2
    entry = read_json(path / 'publications.json')[rid]
    assert entry['status'] == 'unverified' and entry['intent']['receipt']['material_id'] == '1'
    assert workflow.poll(batch, rid) == 2 and cf.ingest.call_count == 1


@pytest.mark.parametrize('missing,status', [
    ('重试次数', {'facebook_bm_sync_status': 'failed'}),
    ('视频ID', {'facebook_bm_sync_status': 'ready', 'facebook_bm_video_id': 'video-1'}),
    ('状态', {'facebook_bm_sync_status': 'ready', 'facebook_bm_video_id': 'video-1'}),
])
def test_guarded_sync_requires_remote_retry_video_and_status_readback(workspace, monkeypatch, missing, status):
    batch, rid, path, cf, remote = sync_fixture(workspace, monkeypatch, missing=(missing,), material_status=status)
    assert workflow.poll(batch, rid) == 2
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'
    assert workflow.poll(batch, rid) == 2 and cf.ingest.call_count == 1


@pytest.mark.parametrize('status,expected,retries', [
    ({'facebook_bm_sync_status': 'processing'}, 'fbbm_queued', 0),
    ({'facebook_bm_sync_status': 'failed'}, 'fbbm_queued', 1),
    ({'facebook_bm_sync_status': 'ready', 'facebook_bm_video_id': 'video-1'}, 'fbbm_ready', 0),
])
def test_guarded_sync_accepts_persisted_material_retry_video_and_status(workspace, monkeypatch, status, expected, retries):
    batch, rid, path, cf, remote = sync_fixture(workspace, monkeypatch, material_status=status)
    assert workflow.poll(batch, rid) == 0
    entry = read_json(path / 'publications.json')[rid]
    assert entry['status'] == expected and entry['intent']['status'] == 'verified'
    assert remote['ADS素材ID'] == '1' and remote['重试次数'] == retries
    if expected == 'fbbm_ready': assert remote['视频ID'] == 'video-1' and remote['状态'] == '已完成'


def test_finish_delivery_preserves_specific_error(runner):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {})
    write_json_atomic(path / 'runtime.json', {'status': 'publishing', 'events': []})
    error = '待核实：OSS上传后校验失败，待核实：fixture.mp4'
    write_json_atomic(path / 'publications.json', {'R1': {'status': 'unverified', 'error': error, 'intent': {'status': 'pending'}}})
    job = queued('a', 'upload', batch=batch)
    job.update(phase='upload', request_id='R1')
    web.finish_job(job, 2)
    assert read_json(path / 'publications.json')['R1']['error'] == error


def test_oss_preparation_failure_is_retryable_without_feishu_write(workspace):
    from exactcut import publish
    batch = imported(); path, request, video, record = complete_asset(batch)
    rid = request['request_id']; workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    with patch.object(publish.OssPublisher, 'upload', side_effect=RuntimeError('OSS校验失败')), patch.object(publish, 'FeishuClient') as feishu:
        assert workflow.upload(batch['id'], rid) == 2
        feishu.assert_not_called()
    entry = read_json(path / 'publications.json')[rid]
    assert entry['status'] == 'publish_failed'
    assert entry['intent']['status'] == 'preparation_failed'
    assert not workflow.delivery_blocked(entry)
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
