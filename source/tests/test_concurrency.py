import queue
import signal
import sys
from unittest.mock import Mock, patch
import pytest
from exactcut import batch_control as control, workflow as workflow, webserver as web
from exactcut.runtime import instance_marker
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace, imported, complete_asset

@pytest.fixture
def runner(tmp_path, monkeypatch):
    from exactcut import concurrency
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'max_active_children': 4}})
    monkeypatch.setattr(workflow, 'BATCHES', tmp_path / 'batches'); workflow.BATCHES.mkdir()
    monkeypatch.setattr(web, 'JOBS', tmp_path / 'jobs'); web.JOBS.mkdir()
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setattr(control, 'PROCESSES', control.FakeProcessTable({}))
    monkeypatch.setattr(web.subprocess, 'Popen', Mock(side_effect=lambda *a, **k: Mock(poll=lambda: None)))
    return tmp_path


def queued(key, lane, batch=None, owner='admin'):
    job = dict(id=key * 32, lane=lane, action=lane, status='queued', created=ord(key),
               command=[sys.executable, '-c', 'pass'], owner=owner, instance=instance_marker())
    if batch: job['batch_id'] = batch
    web.save(job); web.WORK.put((job, job['command']))
    return job


def close_logs(owned):
    for _, _, log in owned.values(): log.close()


def test_full_fetch_lane_does_not_block_render(runner):
    queued('a', 'fetch'); queued('b', 'fetch'); queued('c', 'render')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert set(owned) == {'a' * 32, 'c' * 32}
        assert [j['id'] for j, _ in pending] == ['b' * 32]
    finally: close_logs(owned)


def test_capacity_two_renders_and_owner_fairness(runner, monkeypatch):
    from exactcut import concurrency
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': 2, 'max_active_children': 2}})
    queued('a', 'render', owner='alice'); queued('b', 'render', owner='alice'); queued('c', 'render', owner='bob')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert set(owned) == {'a' * 32, 'c' * 32}
    finally: close_logs(owned)


def test_poll_not_before_releases_all_execution_slots(runner):
    waiting = queued('a', 'poll'); waiting['not_before'] = web.time.time() + 60; web.save(waiting)
    queued('b', 'render'); queued('c', 'upload')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert set(owned) == {'b' * 32, 'c' * 32}
    finally: close_logs(owned)


def test_same_batch_serial_including_stopped_executor(runner):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'producing'})
    child = dict(pid=123, pgid=123, process_token='a' * 32, lane='render', instance=instance_marker())
    write_json_atomic(path / 'control.json', {'paused': True, 'children': [child]})
    control.PROCESSES.rows[123] = dict(pgid=123, state='T', arguments=['python', '-X', 'exactcut_process_token=' + 'a' * 32])
    queued('b', 'upload', batch=batch); queued('c', 'fetch')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert set(owned) == {'c' * 32}
    finally: close_logs(owned)


def test_reconcile_resumes_multiple_lanes_with_running_render(runner):
    for key, pid, lane, state in [('a', 123, 'render', 'S'), ('b', 456, 'fetch', 'T'), ('c', 789, 'upload', 'T')]:
        path = workflow.BATCHES / (key * 32); path.mkdir()
        write_json_atomic(path / 'runtime.json', {'status': 'producing'})
        child = dict(pid=pid, pgid=pid, process_token=key * 32, lane=lane, instance=instance_marker())
        write_json_atomic(path / 'control.json', {'children': [child], 'suspended': [child] if state == 'T' else []})
        control.PROCESSES.rows[pid] = dict(pgid=pid, state=state, arguments=['python', '-X', 'exactcut_process_token=' + key * 32])
    with patch.object(control.os, 'killpg') as kill:
        assert control.reconcile(workflow.BATCHES)
        assert kill.call_args_list == [((456, signal.SIGCONT),), ((789, signal.SIGCONT),)]


def test_upload_exits_without_polling_and_poll_queries_once(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    with patch('exactcut.publish.publish_to_queue', return_value={'feishu_record_id': 'fixture-record', 'sha256': record['output_sha256'], 'oss_key': __import__('exactcut.publish', fromlist=['OssPublisher']).OssPublisher().key_for(video)[0]}) as upload, \
         patch('exactcut.publish.sync_record_once', return_value={'record_id': 'fixture-record', 'action': 'syncing'}) as sync, \
         patch.object(workflow.time, 'sleep', side_effect=AssertionError('poll must not sleep')):
        assert workflow.upload(batch['id'], rid) == 0; sync.assert_not_called()
        assert workflow.poll(batch['id'], rid) == 0; sync.assert_called_once_with('fixture-record', expected_url=None, expected_material_id=None); upload.assert_called_once()
    assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_queued'
    with patch('exactcut.publish.sync_record_once', return_value={'record_id': 'fixture-record', 'action': 'ready', 'video_id': 'fixture-video'}):
        assert workflow.poll(batch['id'], rid) == 0
    assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_ready'


def test_render_never_downloads_missing_source(workspace):
    batch = imported(); workflow.start_batch(batch['id'], batch['revision'])
    with patch.object(workflow, 'download') as download, patch('exactcut.orchestrator.execute_batch') as render:
        assert workflow.render(batch['id']) == 2; download.assert_not_called(); render.assert_not_called()


def test_fetch_completion_queues_one_render_and_poll_reschedules(runner):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'downloading', 'events': []})
    fetch = queued('a', 'fetch', batch=batch); fetch.update(action='start', phase='fetch')
    web.finish_job(fetch, 0); web.finish_job(fetch, 0)
    assert len([j for j in web.stored_jobs() if j.get('phase') == 'render']) == 1
    job = queued('b', 'poll', batch=batch); job.update(action='publish', phase='poll', request_id='R1')
    write_json_atomic(path / 'publications.json', {'R1': {'status': 'fbbm_queued'}})
    before = web.time.time(); web.finish_job(job, 0)
    saved = read_json(web.JOBS / (job['id'] + '.json'))
    assert saved['status'] == 'queued' and saved['not_before'] > before


def test_render_uses_fetch_cache_without_project_video_links(workspace, monkeypatch):
    batch = imported(); workflow.start_batch(batch['id'], batch['revision'])
    cache = workspace / 'source-cache'; monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(cache))
    video = cache / batch['draft']['drama_id'] / 'EP1.mp4'; video.parent.mkdir(parents=True); video.write_bytes(b'fixture-video')
    with patch.object(workflow, 'validate_downloaded_video'), patch('exactcut.media.validate_manifest_sources'), \
         patch('exactcut.orchestrator.execute_batch', return_value={'requests': {'R1': {'status': 'qc_passed'}}}) as execute, \
         patch.object(workflow, 'download') as download:
        assert workflow.render(batch['id']) == 0
        execute.assert_called_once(); download.assert_not_called()


def test_fair_cursor_gives_next_owner_priority_across_ticks(runner):
    first = queued('a', 'render', owner='alice'); queued('b', 'render', owner='alice'); queued('c', 'render', owner='bob')
    pending, owned = [], {}
    try:
        web.worker_tick(pending, owned)
        assert set(owned) == {first['id']}
        owned[first['id']][1].poll = lambda: 0
        web.worker_tick(pending, owned)
        assert set(owned) == {'c' * 32}
    finally: close_logs(owned)


@pytest.mark.parametrize('value', [0, -1, True, '2', None])
def test_invalid_lane_capacity_fails_closed(monkeypatch, value):
    from exactcut import concurrency
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'render': value}})
    with pytest.raises(ValueError): concurrency.settings()


def test_default_limits_and_rollback_switch(monkeypatch):
    from exactcut import concurrency
    monkeypatch.setattr(concurrency, 'load_config', lambda: {})
    limits = concurrency.settings()
    assert all(limits[lane] == 1 for lane in concurrency.LANES)
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'enabled': 'false'}})
    with pytest.raises(ValueError): concurrency.settings()


def test_delayed_poll_survives_recovery_without_interruption(runner):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'publishing', 'events': []})
    write_json_atomic(path / 'publications.json', {'R1': {'status': 'fbbm_queued'}})
    job = queued('a', 'poll', batch=batch); job.update(action='publish', phase='poll', not_before=web.time.time() + 60); web.save(job)
    web.recover_jobs()
    assert workflow.runtime(path)['status'] == 'publishing'
    assert read_json(path / 'publications.json')['R1']['status'] == 'fbbm_queued'


def test_staged_dead_fetch_recovery_does_not_interrupt_successor(runner):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'queued', 'download_complete': True, 'events': []})
    job = queued('a', 'fetch', batch=batch); job.update(action='start', phase='fetch', status='running'); web.save(job)
    web.recover_jobs()
    assert workflow.runtime(path)['status'] == 'queued'
    assert len([j for j in web.stored_jobs() if j.get('phase') == 'render' and j['status'] == 'queued']) == 1


def test_retry_poll_persists_deadline_and_times_out(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    write_json_atomic(path / 'publications.json', {rid: {'status': 'fbbm_queued', 'publication': {'feishu_record_id': 'fixture-record'}}})
    with patch.object(workflow.time, 'time', return_value=1000), patch('exactcut.publish.sync_record_once', return_value={'record_id': 'fixture-record', 'action': 'syncing'}):
        assert workflow.poll(batch['id'], rid) == 0
    assert read_json(path / 'publications.json')[rid]['poll_started'] == 1000
    with patch.object(workflow.time, 'time', return_value=1_000_000), patch('exactcut.publish.sync_record_once') as sync:
        assert workflow.poll(batch['id'], rid) == 2
        sync.assert_not_called()
    assert read_json(path / 'publications.json')[rid]['status'] == 'publish_failed'


def test_nonbatch_render_blocks_stopped_batch_resume(runner):
    batch = 'd' * 32; path = workflow.BATCHES / batch; path.mkdir()
    write_json_atomic(path / 'draft.json', {}); write_json_atomic(path / 'runtime.json', {'status': 'producing'})
    child = dict(pid=123, pgid=123, process_token='a' * 32, lane='render', instance=instance_marker())
    write_json_atomic(path / 'control.json', {'paused': False, 'children': [child], 'suspended': [child]})
    control.PROCESSES.rows[123] = dict(pgid=123, state='T', arguments=['python', '-X', 'exactcut_process_token=' + 'a' * 32])
    external = dict(id='b' * 32, status='running', lane='render', action='execute', instance=instance_marker(), process_token='b' * 32)
    owned = {external['id']: (external, Mock(poll=lambda: None), Mock())}
    with patch.object(control.os, 'killpg') as kill:
        web.worker_tick([], owned)
        kill.assert_not_called()
    assert control.read(path)['suspended']


@pytest.mark.parametrize('phase,status', [('poll', 'fbbm_queued'), ('upload', 'uploading')])
def test_failed_staged_delivery_can_retry_original_record(workspace, monkeypatch, phase, status):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    entry = read_json(path / 'publications.json')[rid]
    entry.update(status=status, publication={'feishu_record_id': 'fixture-record'})
    workflow.store_publication(path, rid, entry)
    monkeypatch.setattr(web, 'JOBS', workspace / 'jobs'); web.JOBS.mkdir()
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    job = dict(id='a' * 32, batch_id=batch['id'], action='publish', phase=phase, lane=phase, request_id=rid, status='running')
    web.finish_job(job, -9)
    assert read_json(path / 'publications.json')[rid]['status'] == 'publish_failed'
    command = workflow.queue_publish(batch['id'], rid, record['output_sha256'])
    assert command
    assert read_json(path / 'publications.json')[rid]['publication']['feishu_record_id'] == 'fixture-record'
