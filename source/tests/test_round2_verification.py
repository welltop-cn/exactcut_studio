"""Crash-safe handoff from a read-only verification intent to its durable job."""
from unittest.mock import Mock, patch
import pytest
from exactcut import workflow as w, webserver as web, publish
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace
from test_round1_delivery import api, uncertain, post, readonly_clients


@pytest.mark.parametrize('fault', ['save', 'detail'])
def test_publication_handoff_failure_can_retry_same_verification(api, workspace, monkeypatch, fault):
    batch, path, rid, video, record, entry = uncertain(workspace)
    data = {'request_id': rid, 'sha256': record['output_sha256'], 'submission_token': 'same-verification-token'}
    route = '/api/batches/' + batch['id'] + '/verify-delivery'
    def crash(*args, **kwargs):
        assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_queued'
        raise OSError('handoff crash after publication write')
    with monkeypatch.context() as patcher:
        patcher.setattr(web if fault == 'save' else w, fault, crash)
        assert post(api, route, data)[0] == 400
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'
    assert not list(web.stored_jobs())
    assert post(api, route, data)[0] == 202
    with pytest.raises(ValueError): w.queue_publish(batch['id'], rid, record['output_sha256'])
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish.OssPublisher, 'upload') as oss:
        assert w.poll(batch['id'], rid) == 0
    cf.ingest.assert_not_called(); cf.resync.assert_not_called()
    feishu.create_or_reuse.assert_not_called(); feishu.update.assert_not_called(); oss.assert_not_called()


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('prior_recovery', [False, True])
def test_restart_repairs_orphaned_verification_handoff(api, workspace, legacy, prior_recovery):
    batch, path, rid, video, record, entry = uncertain(workspace)
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'])
    if not legacy:
        publications = read_json(path / 'publications.json')
        publications[rid]['verification_queue'] = {'job_id': 'f' * 32, 'previous': entry}
        write_json_atomic(path / 'publications.json', publications)
    if prior_recovery:
        publications = read_json(path / 'publications.json')
        publications[rid]['status'] = 'publish_failed'
        write_json_atomic(path / 'publications.json', publications)
    web.recover_jobs(scheduler=True)
    repaired = read_json(path / 'publications.json')[rid]
    assert repaired['status'] == 'unverified'
    assert repaired['intent']['status'] == 'pending'
    assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', {'request_id': rid, 'sha256': record['output_sha256']})[0] == 202


def test_job_saved_before_reply_failure_survives_handoff_repair(api, workspace, monkeypatch):
    batch, path, rid, video, record, entry = uncertain(workspace)
    data = {'request_id': rid, 'sha256': record['output_sha256'], 'submission_token': 'saved-job-token-123456'}
    save = web.save
    def crash_after_save(job):
        save(job)
        raise OSError('crash after durable job')
    with monkeypatch.context() as patcher:
        patcher.setattr(web, 'save', crash_after_save)
        assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)[0] == 400
    web.recover_jobs(enqueue=True, scheduler=True)
    jobs = list(web.stored_jobs())
    assert len(jobs) == 1 and jobs[0]['status'] == 'queued'
    assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_queued'
    assert post(api, '/api/batches/' + batch['id'] + '/verify-delivery', data)[0] == 200
