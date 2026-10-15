"""First-upgrade health failure can drain via a new-image maintenance CLI."""
import sys
from unittest.mock import patch

import pytest
from exactcut import publish, stage_results, workflow as w, webserver as web
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace
from test_round1_delivery import uncertain, readonly_clients
from test_linux_release import ops, release
from test_round1_release import setup_target


@pytest.fixture
def isolated(workspace, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '0')
    monkeypatch.setattr(web, 'JOBS', workspace / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    write_json_atomic(workspace / '.web-state/maintenance.json', {'reason': 'release'})
    return workspace


def blocked_delivery(workspace):
    batch, path, rid, _, record, entry = uncertain(workspace)
    original = {'id': 'd' * 32, 'batch_id': batch['id'], 'request_id': rid,
                'action': 'publish', 'phase': 'poll', 'status': 'unverified', 'attempt': 1}
    original['input_fingerprint'] = stage_results.fingerprint(original, w.BATCHES, w.ROOT)
    web.save(original)
    stage_results.complete(original, web.JOBS, w.BATCHES, w.ROOT, 2)
    return batch['id'], rid, record['output_sha256'], original


def test_offline_verification_is_readonly_and_resolves_bound_history(isolated):
    from exactcut import maintenance_verify
    bid, rid, sha, original = blocked_delivery(isolated)
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish.OssPublisher, 'upload') as oss:
        result = maintenance_verify.verify_delivery(bid, rid, sha)
    assert result['status'] == 'verified'
    assert read_json(web.JOBS / (original['id'] + '.json'))['resolution'] == 'verified_delivery'
    assert (isolated / '.web-state/maintenance.json').exists()
    cf.sync_status.assert_not_called(); feishu.get_record.assert_called_once_with('saved-record')
    cf.ingest.assert_not_called(); cf.resync.assert_not_called()
    feishu.create_or_reuse.assert_not_called(); feishu.update.assert_not_called(); oss.assert_not_called()
    with pytest.raises(ValueError): w.queue_publish(bid, rid, sha)


@pytest.mark.parametrize('condition', ['no-maintenance', 'worker-enabled', 'bad-sha'])
def test_offline_verification_fail_closed_before_query(isolated, monkeypatch, condition):
    from exactcut import maintenance_verify
    bid, rid, sha, _ = blocked_delivery(isolated)
    if condition == 'no-maintenance': (isolated / '.web-state/maintenance.json').unlink()
    if condition == 'worker-enabled': monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
    if condition == 'bad-sha': sha = '0' * 64
    before = read_json(w.folder(bid) / 'publications.json')
    with patch.object(publish, 'verify_record_once') as query:
        with pytest.raises((ValueError, RuntimeError)): maintenance_verify.verify_delivery(bid, rid, sha)
    query.assert_not_called()
    assert read_json(w.folder(bid) / 'publications.json') == before


@pytest.mark.parametrize('remote', ['syncing', 'mismatch', 'timeout'])
def test_offline_unready_or_unknown_keeps_guard_for_explicit_retry(isolated, remote):
    from exactcut import maintenance_verify
    bid, rid, sha, _ = blocked_delivery(isolated)
    feishu, cf = readonly_clients(status='syncing' if remote == 'syncing' else 'ready')
    if remote == 'mismatch': feishu.get_record.return_value['fields']['ADS素材ID'] = 'different'
    if remote == 'timeout': feishu.get_record.side_effect = TimeoutError('fixture lost read')
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'unverified'
    assert read_json(w.folder(bid) / 'publications.json')[rid]['status'] == 'unverified'
    assert not [j for j in web.stored_jobs() if j['status'] in {'queued', 'running'}]
    assert (isolated / '.web-state/maintenance.json').exists()
    cf.ingest.assert_not_called(); cf.resync.assert_not_called(); feishu.update.assert_not_called()


def test_ready_receipt_before_local_resolution_crash_can_resume(isolated, monkeypatch):
    from exactcut import maintenance_verify
    bid, rid, sha, original = blocked_delivery(isolated)
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        with monkeypatch.context() as fault:
            fault.setattr(web, 'resolve_verified_delivery', lambda *a: (_ for _ in ()).throw(OSError('local resolve crash')))
            with pytest.raises(OSError): maintenance_verify.verify_delivery(bid, rid, sha)
        assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'verified'
    cf.sync_status.assert_not_called()
    assert read_json(web.JOBS / (original['id'] + '.json'))['status'] == 'interrupted'


def test_first_upgrade_failure_recovers_without_old_application_api(ops, isolated, monkeypatch, capsys):
    root = isolated / 'deployment'
    engine = setup_target(ops, root, capable=True)
    (root / 'data').rmdir(); (root / 'data').symlink_to(isolated, target_is_directory=True)
    # Begin the real transition without a pre-existing maintenance flag.
    (isolated / '.web-state/maintenance.json').unlink()
    old = release(ops, root, 'old'); old['image'] = 'sha256:' + 'c' * 64
    ops.write(root / 'releases/old/release.json', old)
    ops.write(root / 'current.json', {'version': 'old'})
    assert not engine.stage_protocol(old['image'])  # Old image has no verification API.
    engine.fail = 'target'
    health = engine.health
    saved = []
    def new_health(settings, version, timeout):
        if version == 'target' and not saved: saved.extend(blocked_delivery(isolated))
        return health(settings, version, timeout)
    engine.health = new_health
    with pytest.raises(RuntimeError): ops.transition(root, 'target', engine=engine, timeout=0)
    assert ('apply', 'old') in engine.calls
    assert (isolated / '.web-state/maintenance.json').exists()
    bid, rid, sha, _ = saved
    def oneoff(root, image, settings, batch, request, digest):
        from exactcut import maintenance_verify
        assert image['version'] == 'target' and engine.stage_protocol(image['image'])
        assert engine.calls[-1] == ('stop', 'old')
        return maintenance_verify.verify_delivery(batch, request, digest)
    engine.verify_delivery = oneoff
    monkeypatch.setattr(ops, 'Docker', lambda: engine)
    monkeypatch.setattr(sys, 'platform', 'linux')
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        monkeypatch.setattr(sys, 'argv', ['linux-ops', '--root', str(root), 'verify-delivery', 'target', bid, rid, sha])
        ops.main()
    assert not ops.phase_blockers(root)
    assert ops.read(root / 'current.json')['version'] == 'old'
    assert (isolated / '.web-state/maintenance.json').exists()
    monkeypatch.setattr(sys, 'argv', ['linux-ops', '--root', str(root), 'recover'])
    ops.main()
    assert not (isolated / '.web-state/maintenance.json').exists()
    engine.fail = None
    assert ops.transition(root, 'target', engine=engine, timeout=0)['status'] == 'ok'
    assert ops.read(root / 'current.json')['version'] == 'target'
    cf.ingest.assert_not_called(); cf.resync.assert_not_called(); feishu.update.assert_not_called()


def test_oneoff_command_uses_fixed_image_without_web_entrypoint(ops, tmp_path, monkeypatch):
    observed = []
    monkeypatch.setattr(ops.Docker, 'compose', lambda *args: observed.append(args))
    result = ops.Docker().verify_delivery(tmp_path, {'version': 'target', 'image': 'sha256:fixed'}, {}, 'a' * 32, 'R1', 'b' * 64)
    args = observed[0][4:]
    assert args[:4] == ('run', '--rm', '--no-deps', '-T')
    assert '--entrypoint' in args and 'EXACTCUT_WORKER_ENABLED=0' in args
    assert args[-6:] == ('-m', 'exactcut.maintenance_verify', 'a' * 32, 'R1', 'b' * 64, '--once')
    assert result['status'] == 'queried'


@pytest.mark.parametrize('fault', ['poll', 'complete'])
def test_oneoff_local_failure_can_retry_without_stranded_queue(isolated, monkeypatch, fault):
    from exactcut import maintenance_verify
    bid, rid, sha, _ = blocked_delivery(isolated)
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        with monkeypatch.context() as patcher:
            module = w if fault == 'poll' else stage_results
            patcher.setattr(module, fault, lambda *a: (_ for _ in ()).throw(OSError('local fault')))
            if fault == 'complete':
                with pytest.raises(OSError): maintenance_verify.verify_delivery(bid, rid, sha)
            else:
                assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'unverified'
        assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'verified'
    assert not [j for j in web.stored_jobs() if j['status'] in {'queued', 'running'}]
    cf.ingest.assert_not_called(); cf.resync.assert_not_called(); feishu.update.assert_not_called()


@pytest.mark.parametrize('condition', ['no-maintenance', 'old-executor', 'active-task', 'query-failed'])
def test_maintenance_manager_guards_and_restores_on_error(ops, tmp_path, condition):
    engine = setup_target(ops, tmp_path, capable=condition != 'old-executor')
    old = release(ops, tmp_path, 'old'); old['image'] = 'sha256:' + 'c' * 64
    ops.write(tmp_path / 'releases/old/release.json', old)
    ops.write(tmp_path / 'current.json', {'version': 'old'})
    gate = tmp_path / 'data/.web-state/maintenance.json'
    if condition != 'no-maintenance': ops.write(gate, {'reason': 'release'})
    if condition == 'active-task': ops.write(tmp_path / 'data/.web-state/jobs/q.json', {'status': 'queued'})
    def fail(*args): raise OSError('provider fixture')
    engine.verify_delivery = fail
    with pytest.raises((RuntimeError, OSError)):
        ops.verify_delivery_in_maintenance(tmp_path, 'target', 'a' * 32, 'R1', 'b' * 64, engine=engine)
    if condition == 'query-failed':
        assert engine.calls == [('stop', 'old'), ('apply', 'old'), ('health', 'old')]
    else: assert engine.calls == []
    assert gate.exists() == (condition != 'no-maintenance')
    assert ops.read(tmp_path / 'current.json')['version'] == 'old'


def test_maintenance_application_cli_requires_once_and_returns_pending_exit(isolated, monkeypatch, capsys):
    from exactcut import maintenance_verify
    bid, rid, sha, _ = blocked_delivery(isolated)
    monkeypatch.setattr(sys, 'argv', ['maintenance-verify', bid, rid, sha])
    with pytest.raises(SystemExit) as error: maintenance_verify.main()
    assert error.value.code == 2
    monkeypatch.setattr(sys, 'argv', ['maintenance-verify', bid, rid, sha, '--once'])
    (isolated / '.env').write_text('')
    feishu, cf = readonly_clients(status='syncing')
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert maintenance_verify.main() == 2
    assert 'unverified' in capsys.readouterr().out


def test_maintenance_disk_refusal_stays_explicitly_retryable(isolated, monkeypatch):
    from exactcut import maintenance_verify, storage
    bid, rid, sha, _ = blocked_delivery(isolated)
    with monkeypatch.context() as patcher:
        patcher.setattr(storage, 'reserve', lambda *a: False)
        with patch.object(publish, 'verify_record_once') as query:
            assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'unverified'
        query.assert_not_called()
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'verified'


@pytest.mark.parametrize('legacy_recovered', [False, True])
def test_offline_verification_repairs_missing_handoff_job(isolated, legacy_recovered):
    from exactcut import maintenance_verify
    bid, rid, sha, original = blocked_delivery(isolated)
    w.queue_verify_delivery(bid, rid, sha, _job_id='f' * 32)
    if legacy_recovered:
        entry = read_json(w.folder(bid) / 'publications.json')[rid]
        entry['status'] = 'publish_failed'
        w.store_publication(w.folder(bid), rid, entry)
        w.update(w.folder(bid), 'interrupted')
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf):
        assert maintenance_verify.verify_delivery(bid, rid, sha)['status'] == 'verified'
    assert read_json(web.JOBS / (original['id'] + '.json'))['status'] == 'interrupted'
    cf.ingest.assert_not_called(); cf.resync.assert_not_called(); feishu.update.assert_not_called()
