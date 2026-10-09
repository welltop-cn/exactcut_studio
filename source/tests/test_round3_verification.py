"""Durable verification waits are not orphaned publication handoffs."""
from unittest.mock import patch

import pytest
from exactcut import publish, stage_results, workflow as w, webserver as web
from exactcut.util import read_json
from test_workflow import workspace
from test_round1_delivery import api, uncertain, post, readonly_clients
from test_round1_integration import chain


@pytest.mark.parametrize('status', ['waiting_resource', 'queued', 'running', 'completed', 'future-wait'])
def test_durable_verification_is_not_an_orphan(api, workspace, status):
    batch, path, rid, _, record, _ = uncertain(workspace)
    route = '/api/batches/' + batch['id'] + '/verify-delivery'
    assert post(api, route, {'request_id': rid, 'sha256': record['output_sha256']})[0] == 202
    job = next(web.stored_jobs())
    job.update(status=status, not_before=2000)
    web.save(job)
    before = read_json(path / 'publications.json')
    w.recover_verification_queue(list(web.stored_jobs()))
    assert read_json(path / 'publications.json') == before
    assert w.runtime(path)['status'] != 'interrupted'
    if status not in {'completed'}:
        # Existing queued/running submissions return their original job (200);
        # other durable waits reject a new submission, never creating a job.
        expected = 200 if status in {'queued', 'running'} else 400
        assert post(api, route, {'request_id': rid, 'sha256': record['output_sha256'],
                                 'submission_token': 'second-verification-token'})[0] == expected
        assert len(list(web.stored_jobs())) == 1


def test_verification_exit_75_waits_then_queries_original_receipt_once(chain, monkeypatch):
    batch, path, rid, _, record, _ = uncertain(w.ROOT)
    job = {'id': 'f' * 32, 'batch_id': batch['id'], 'request_id': rid,
           'action': 'verify-delivery', 'status': 'queued', 'created': 0}
    w.queue_verify_delivery(batch['id'], rid, record['output_sha256'], _job_id=job['id'])
    job['verification_only'] = read_json(path / 'publications.json')[rid]['verification_only']
    web.prepare_staged_job(job); web.save(job)
    clock = [1000.]
    monkeypatch.setattr(web.time, 'time', lambda: clock[0])
    chain.tick()
    job, process, _ = chain.one('poll')
    monkeypatch.setitem(__import__('sys')._xoptions, 'exactcut_process_token', job['process_token'])
    def disk_full(*args):
        raise stage_results.ResourceDeferred(1010., 'fixture disk full')
    assert stage_results.run_child(disk_full, batch['id'], rid) == 75
    assert read_json(stage_results.result_path(job, web.JOBS))['status'] == 'waiting_resource'
    process.code = 75; chain.table.rows.pop(process.pid)
    chain.tick()
    assert not chain.owned
    assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_queued'
    # Also accept a persisted job in the wait state (restart/legacy writer).
    job = next(web.stored_jobs()); job['status'] = 'waiting_resource'; web.save(job)
    before = read_json(path / 'publications.json')
    for _ in range(2): chain.tick()
    assert read_json(path / 'publications.json') == before
    assert not chain.owned
    assert next(web.stored_jobs())['not_before'] == 1010.
    clock[0] = 1010.
    chain.tick()
    feishu, cf = readonly_clients()
    with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish.OssPublisher, 'upload') as oss:
        chain.finish('poll')
    for _ in range(2): chain.tick()
    assert not chain.owned
    feishu.get_record.assert_called_once_with('saved-record')
    cf.sync_status.assert_called_once_with('123')
    cf.ingest.assert_not_called(); cf.resync.assert_not_called()
    feishu.create_or_reuse.assert_not_called(); feishu.update.assert_not_called(); oss.assert_not_called()
