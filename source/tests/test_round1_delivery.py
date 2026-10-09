"""Round-one delivery safety: fixtures only, no external requests."""
import copy
from unittest.mock import Mock, patch
import pytest
from exactcut import cli, publish, workflow as w, stage_results, webserver as web
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace, imported, complete_asset


@pytest.mark.parametrize('code', [100006, 100009])
def test_guarded_auth_rejection_relogs_once_and_preserves_headers(code):
    client = publish.CfAdsClient.__new__(publish.CfAdsClient)
    client.token = 'expired'; client.base = 'https://fixture.invalid'; client.timeout = 1
    responses = [Mock(json=lambda: {'code': code}), Mock(json=lambda: {'code': 200})]
    def login(): client.token = 'fresh'
    with patch.object(client, 'login', side_effect=login) as auth, patch.object(publish.requests, 'request', side_effect=responses) as request, patch.object(publish.time, 'sleep', side_effect=AssertionError('no sleep')):
        with publish.guarded_delivery():
            assert client.request('GET', '/api/v1/material/123/sync_status', headers={'X-Fixture': 'yes'}) == {'code': 200}
        assert auth.call_count == 1 and request.call_count == 2
        assert all(call.kwargs['headers']['X-Fixture'] == 'yes' for call in request.call_args_list)
        assert request.call_args.kwargs['headers']['Authorization'] == 'Bearer fresh'


@pytest.mark.parametrize('extra', [[], ['--publish-dry-run']])
def test_legacy_live_execute_rejected_before_manifest_and_external_write(tmp_path, extra, capsys):
    (tmp_path / 'project.json').write_text('{}')
    with patch.object(cli, '_manifest_from_args', return_value=(object(), None)) as manifest, patch.object(cli, 'execute_batch', return_value={}) as execute:
        for _ in range(2):
            assert cli._main(['execute', '--project', str(tmp_path), '--manifest', 'fixture.json', '--publish', *extra]) != 0
        manifest.assert_not_called(); execute.assert_not_called()
    assert '阶段' in capsys.readouterr().err


def uncertain(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch); rid = request['request_id']
    publication = {'feishu_record_id': 'saved-record', 'sha256': record['output_sha256'], 'oss_key': 'fixture-key', 'oss_url': 'https://fixture.invalid/video'}
    entry = {'status': 'unverified', 'approved_sha256': record['output_sha256'], 'publication': publication,
             'intent': {'phase': 'poll', 'status': 'pending', 'approved_sha256': record['output_sha256'], 'oss_key': 'fixture-key', 'feishu_record_id': 'saved-record', 'receipt': {'record_id': 'saved-record', 'material_id': '123'}}}
    w.store_publication(path, rid, entry)
    return batch, path, rid, video, record, entry


def readonly_clients(url='https://fixture.invalid/video', material='123', status='ready'):
    feishu = Mock(); feishu.get_record.return_value = {'record_id': 'saved-record', 'fields': {'OSS_URL': {'link': url}, 'ADS素材ID': material}}
    cf = Mock(); cf.sync_status.return_value = {'facebook_bm_sync_status': status, 'facebook_bm_video_id': 'saved-video' if status == 'ready' else None}
    return feishu, cf


def test_controlled_poll_keeps_receipt_after_timeout_and_never_ingests(workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    original = copy.deepcopy(entry['intent'])
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish.OssPublisher, 'upload') as oss:
        feishu.get_record.side_effect = TimeoutError('read response lost')
        assert w.poll(batch['id'], rid) == 2
        saved = read_json(path / 'publications.json')[rid]
        assert saved['status'] == 'unverified' and saved['verification_only']['material_id'] == '123'
        assert original in saved['verification_history']
        feishu.get_record.side_effect = None
        w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
        assert w.poll(batch['id'], rid) == 0
        assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_ready'
        cf.sync_status.assert_called_once_with('123'); cf.ingest.assert_not_called(); cf.resync.assert_not_called()
        feishu.create_or_reuse.assert_not_called(); feishu.update.assert_not_called(); oss.assert_not_called()


@pytest.mark.parametrize('mutation', ['no_material', 'record_conflict', 'sha_conflict', 'url_conflict'])
def test_controlled_entry_rejects_missing_or_conflicting_receipts(workspace, mutation):
    batch, path, rid, video, record, entry = uncertain(workspace)
    if mutation == 'no_material': entry['intent']['receipt'].pop('material_id')
    elif mutation == 'record_conflict': entry['intent']['receipt']['record_id'] = 'other'
    elif mutation == 'sha_conflict': entry['intent']['approved_sha256'] = 'other'
    else: entry['intent']['receipt']['oss_url'] = 'https://other.invalid/video'
    w.store_publication(path, rid, entry)
    with pytest.raises(ValueError): w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'


def test_controlled_poll_revalidates_remote_url_before_cf_query(workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    feishu, cf = readonly_clients(url='https://other.invalid/video')
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert w.poll(batch['id'], rid) == 2
    cf.sync_status.assert_not_called(); cf.ingest.assert_not_called()
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'


def test_prevalidated_approval_checks_identity_without_second_hash(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch)
    evidence = stage_results.digest_evidence(video)
    with patch.object(w, 'file_sha256', side_effect=AssertionError('hash under lock')):
        w.queue_publish(batch['id'], request['request_id'], record['output_sha256'], _evidence=evidence)


def test_prevalidated_approval_rejects_same_size_restored_mtime_change(workspace):
    import os
    batch = imported(); path, request, video, record = complete_asset(batch)
    evidence = stage_results.digest_evidence(video); old = video.stat()
    video.write_bytes(b'x' * old.st_size); os.utime(video, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(ValueError, match='变化'):
        w.queue_publish(batch['id'], request['request_id'], record['output_sha256'], _evidence=evidence)

@pytest.fixture
def api(workspace, monkeypatch):
    import queue, threading
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(web, 'ROOT', workspace)
    monkeypatch.setattr(web, 'JOBS', workspace / 'jobs'); web.JOBS.mkdir()
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setattr(web, 'require_publish', lambda: None)
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.password = 'fixture-password'
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield server
    server.shutdown(); server.server_close(); thread.join(2)


def post(server, route, data, *, csrf=True, auth=True, session=None):
    import base64, json
    from http.client import HTTPConnection
    headers = {}
    if auth: headers['Authorization'] = 'Basic ' + base64.b64encode(b'admin:fixture-password').decode()
    if session: headers['Cookie'] = 'exactcut_session=' + session
    if csrf: headers['X-Exactcut-Request'] = '1'
    connection = HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request('POST', route, json.dumps(data), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally: connection.close()


def test_verification_http_is_idempotent_staged_poll_with_readonly_queue_position(api, workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    route = '/api/batches/' + batch['id'] + '/verify-delivery'
    data = {'request_id': rid, 'sha256': record['output_sha256'], 'submission_token': 'fixture-token-123456'}
    first, result = post(api, route, data)
    assert first == 202
    assert result['submission']['queue_position'] == 1
    assert post(api, route, data)[0] == 200
    jobs = list(web.stored_jobs()); assert len(jobs) == 1
    assert jobs[0]['phase'] == 'poll' and jobs[0]['verification_only']['material_id'] == '123'
    assert jobs[0]['action'] == 'verify-delivery'


@pytest.mark.parametrize('condition,expected', [('unauth',401), ('csrf',403), ('worker',503), ('maintenance',503), ('nonadmin',403), ('archived',400)])
def test_verification_http_respects_control_boundaries(api, workspace, monkeypatch, condition, expected):
    batch, path, rid, video, record, entry = uncertain(workspace)
    if condition == 'worker': monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '0')
    if condition == 'maintenance':
        barrier = workspace / 'barrier'; barrier.write_text('{}'); monkeypatch.setattr(web, 'maintenance_file', lambda: barrier)
    if condition == 'nonadmin':
        def auth(self): self.user = {'username': 'user', 'role': 'user'}; return True
        monkeypatch.setattr(web.Handler, 'authorized', auth)
    if condition == 'archived': write_json_atomic(path / 'control.json', {'archived': True})
    assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', {'request_id': rid, 'sha256': record['output_sha256']}, csrf=condition != 'csrf', auth=condition != 'unauth')[0] == expected
    assert not list(web.stored_jobs())


def test_http_approval_hash_releases_global_lock_for_other_user(api, workspace, monkeypatch):
    import threading, time
    batch = imported(); path, request, video, record = complete_asset(batch)
    # Sparse >=1GiB fixture; deterministic slow reader exposes blocking without disk IO.
    with video.open('wb') as stream: stream.truncate(1024**3)
    record['output_sha256'] = 'fixture-sha'; write_json_atomic(path / 'production/results.json', {'requests': {request['request_id']: record}})
    other = imported(); entered = threading.Event(); release = threading.Event(); result = []
    # Real accounts and owner checks; sessions avoid repeated password hashing in the timing window.
    api.auth_db = workspace / 'accounts.sqlite'
    web.accounts.initialize(api.auth_db, 'fixture-admin-password')
    sessions = {}
    for username, owned in [('alice', path), ('bob', w.folder(other['id']))]:
        web.accounts.manage(api.auth_db, 'admin', {'username': username, 'name': username, 'password': 'fixture-user-password'})
        user = web.accounts.authenticate(api.auth_db, username, 'fixture-user-password')
        web.accounts.assign(owned, user)
        sessions[username] = web.accounts.login(api.auth_db, user)
    assert web.accounts.owner(path) == 'alice' and web.accounts.owner(w.folder(other['id'])) == 'bob'
    def slow_digest(file):
        entered.set(); assert release.wait(5)
        return {'path': str(file), 'sha256': 'fixture-sha', 'bytes': file.stat().st_size, 'stat': stage_results.file_identity(file)}
    monkeypatch.setattr(stage_results, 'digest_evidence', slow_digest)
    monkeypatch.setattr(w, 'file_sha256', lambda file: (slow_digest(file))['sha256'])
    thread = threading.Thread(target=lambda: result.append(post(api, '/api/batches/' + batch['id'] + '/publish', {'request_id': request['request_id'], 'sha256': 'fixture-sha'}, session=sessions['alice']))); thread.start()
    try:
        assert entered.wait(2)
        start = time.monotonic()
        code, _ = post(api, '/api/batches/' + other['id'] + '/start', {'revision': other['revision']}, session=sessions['bob'])
        assert code == 202 and time.monotonic() - start < 1
    finally: release.set(); thread.join(6)
    assert result[0][0] == 202
    assert {job['actor'] for job in web.stored_jobs()} == {'alice', 'bob'}


def test_verification_fences_ordinary_upload_after_failed_readonly_query(workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    # A poll receipt may be available even when publication was not finalized.
    entry['intent']['receipt'].update(entry.pop('publication'))
    w.store_publication(path, rid, entry)
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    feishu, cf = readonly_clients(status='failed')
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert w.poll(batch['id'], rid) == 2
    with patch.object(publish, 'publish_to_queue') as upload:
        assert w.upload(batch['id'], rid) == 2
        assert w.publish(batch['id'], rid) == 2
        upload.assert_not_called()
    with pytest.raises(ValueError, match='核实'): w.queue_publish(batch['id'], rid, record['output_sha256'])


@pytest.mark.parametrize('field,value', [('material_id','999'), ('record_id','other')])
def test_readonly_mode_cannot_replace_saved_receipt_identity(workspace, field, value):
    batch, path, rid, video, record, entry = uncertain(workspace)
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    entry = read_json(path / 'publications.json')[rid]; entry['verification_only'][field] = value
    w.store_publication(path, rid, entry)
    with patch.object(publish, 'verify_record_once') as query:
        assert w.poll(batch['id'], rid) == 2
        query.assert_not_called()
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'


def test_missing_remote_material_id_stays_unverified_without_ingest(workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    feishu, cf = readonly_clients(material='')
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert w.poll(batch['id'], rid) == 2
    cf.sync_status.assert_not_called(); cf.ingest.assert_not_called(); feishu.update.assert_not_called()


def test_legacy_dryrun_only_remains_safe(tmp_path):
    (tmp_path / 'project.json').write_text('{}')
    with patch.object(cli, '_manifest_from_args', return_value=(object(), None)), patch.object(cli, 'execute_batch', return_value={}) as execute:
        assert cli._main(['execute', '--project', str(tmp_path), '--manifest', 'fixture.json', '--publish-dry-run']) == 0
    assert execute.call_args.kwargs['publish_dry_run'] is True


@pytest.mark.parametrize('condition,code', [('maintenance',503), ('access',404), ('archive',400), ('file',400), ('state',409)])
def test_http_approval_rechecks_after_hash(api, workspace, monkeypatch, condition, code):
    import os
    batch = imported(); path, request, video, record = complete_asset(batch)
    original = stage_results.digest_evidence
    def changed(file):
        evidence = original(file)
        if condition == 'maintenance':
            barrier = workspace / 'barrier'; barrier.write_text('{}'); monkeypatch.setattr(web, 'maintenance_file', lambda: barrier)
        elif condition == 'access': monkeypatch.setattr(web.Handler, 'can_access', lambda *args: False)
        elif condition == 'archive': write_json_atomic(path / 'control.json', {'archived': True})
        elif condition == 'file':
            old = video.stat(); video.write_bytes(b'x' * old.st_size); os.utime(video, ns=(old.st_atime_ns, old.st_mtime_ns))
        else: w.update(path, message='changed during approval')
        return evidence
    monkeypatch.setattr(stage_results, 'digest_evidence', changed)
    assert post(api, '/api/batches/' + batch['id'] + '/publish', {'request_id': request['request_id'], 'sha256': record['output_sha256']})[0] == code
    assert not list(web.stored_jobs())


@pytest.mark.parametrize('failure', ['second_auth', 'login', 'network'])
def test_guarded_auth_retry_stops_after_one_login_or_unknown_response(failure):
    client = publish.CfAdsClient.__new__(publish.CfAdsClient)
    client.token = 'expired'; client.base = 'https://fixture.invalid'; client.timeout = 1
    responses = [Mock(json=lambda: {'code': 100006}), Mock(json=lambda: {'code': 100009})]
    def login():
        if failure == 'login': raise publish.PublishError('login rejected')
        client.token = 'fresh'
    with patch.object(client, 'login', side_effect=login) as auth, patch.object(publish.requests, 'request', side_effect=TimeoutError('response lost') if failure == 'network' else responses) as request:
        with publish.guarded_delivery(), pytest.raises((publish.PublishError, TimeoutError)):
            client.sync_status('123')
        assert auth.call_count == (0 if failure == 'network' else 1)
        assert request.call_count == (2 if failure == 'second_auth' else 1)


def test_readonly_mode_survives_delayed_reentry_and_dead_process(api, workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    data = {'request_id': rid, 'sha256': record['output_sha256']}
    assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)[0] == 202
    job = list(web.stored_jobs())[0]
    feishu, cf = readonly_clients(status='syncing')
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish, 'sync_record_once') as ordinary:
        assert w.poll(batch['id'], rid) == 0
        web.finish_job(job, 0)
        delayed = read_json(web.JOBS / (job['id'] + '.json'))
        assert delayed['status'] == 'queued' and delayed['not_before'] > web.time.time()
        assert delayed['verification_only']['material_id'] == '123'
        # Re-read persisted mode as a restarted worker would.
        assert w.poll(batch['id'], rid) == 0
        ordinary.assert_not_called(); cf.ingest.assert_not_called(); cf.resync.assert_not_called()
    persisted = read_json(path / 'publications.json')[rid]
    persisted['intent']['status'] = 'pending'; w.store_publication(path, rid, persisted)
    job.update(status='running', attempt=1, process_token='a' * 32); web.save(job)
    web.recover_jobs()
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'
    assert read_json(path / 'publications.json')[rid]['verification_only']['material_id'] == '123'


@pytest.mark.parametrize('change', ['arbitrary_ids', 'sha', 'queue_full', 'same_batch', 'token_payload'])
def test_verification_http_rejects_unsafe_or_conflicting_intake(api, workspace, change):
    batch, path, rid, video, record, entry = uncertain(workspace)
    data = {'request_id': rid, 'sha256': record['output_sha256'], 'submission_token': 'fixture-token-123456'}
    if change == 'arbitrary_ids': data['material_id'] = '999'
    elif change == 'sha': data['sha256'] = 'wrong'
    elif change == 'queue_full':
        for index in range(10): web.save({'id': str(index), 'status': 'queued', 'created': index})
    elif change == 'same_batch': web.save({'id': 'a' * 32, 'status': 'queued', 'batch_id': batch['id'], 'action': 'preview'})
    else:
        assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)[0] == 202
        data['sha256'] = 'different'
    code, _ = post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)
    assert code == (429 if change == 'queue_full' else 400)


def test_lost_readonly_marker_never_falls_back_to_ordinary_poll(workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    entry = read_json(path / 'publications.json')[rid]; entry.pop('verification_only')
    w.store_publication(path, rid, entry)
    with patch.object(publish, 'sync_record_once', return_value={'record_id': 'saved-record', 'action': 'syncing'}) as ordinary:
        assert w.poll(batch['id'], rid) == 2
        ordinary.assert_not_called()


def test_verification_admin_action_audited_once_for_lost_response(api, workspace, monkeypatch):
    batch, path, rid, video, record, entry = uncertain(workspace)
    def authorized(self): self.user = {'username': 'admin', 'role': 'admin'}; return True
    monkeypatch.setattr(web.Handler, 'authorized', authorized)
    api.auth_db = object()
    data = {'request_id': rid, 'sha256': record['output_sha256'], 'submission_token': 'fixture-token-123456'}
    with patch.object(web.accounts, 'audit') as audit:
        assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)[0] == 202
        assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)[0] == 200
        audit.assert_called_once_with(api.auth_db, 'admin', 'verify-delivery', batch['id'])


def test_readonly_local_launch_failure_can_retry_only_saved_poll(api, workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    route = '/api/batches/' + batch['id'] + '/verify-delivery'
    data = {'request_id': rid, 'sha256': record['output_sha256'], 'submission_token': 'first-attempt-123456'}
    assert post(api, route, data)[0] == 202
    job = list(web.stored_jobs())[0]
    web.finish_job(job, 2, 'fixture launch failed', local_failure=True)
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'
    data['submission_token'] = 'retry-attempt-123456'
    assert post(api, route, data)[0] == 202
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish.OssPublisher, 'upload') as oss, patch.object(publish, 'publish_to_queue') as upload:
        assert w.poll(batch['id'], rid) == 0
        assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_ready'
        cf.sync_status.assert_called_once_with('123')
        cf.ingest.assert_not_called(); cf.resync.assert_not_called()
        feishu.create_or_reuse.assert_not_called(); feishu.update.assert_not_called()
        oss.assert_not_called(); upload.assert_not_called()
