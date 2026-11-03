"""Guarded writes never replay ambiguous authentication responses."""
from unittest.mock import Mock, patch
import pytest
from exactcut import publish, workflow as w, webserver as web, stage_results
from exactcut.util import read_json
from test_workflow import workspace
from test_round1_delivery import api, uncertain


def client():
    cf = publish.CfAdsClient.__new__(publish.CfAdsClient)
    cf.token = 'expired'; cf.base = 'https://fixture.invalid'; cf.timeout = 1
    return cf


@pytest.mark.parametrize('code', [100006, 100009])
@pytest.mark.parametrize('method,path', [('POST', '/api/v1/material/ingest_and_sync'), ('POST', '/api/v1/material/sync/facebook'), ('PUT', '/update'), ('DELETE', '/delete')])
def test_guarded_write_auth_response_never_replays(code, method, path):
    cf = client()
    with patch.object(cf, 'login') as login, patch.object(publish.requests, 'request', return_value=Mock(json=lambda: {'code': code})) as request:
        with publish.guarded_delivery():
            assert cf.request(method, path)['code'] == code
        assert request.call_count == 1
        login.assert_not_called()


@pytest.mark.parametrize('code', [100006, 100009])
def test_guarded_material_read_relogs_once(code):
    cf = client()
    with patch.object(cf, 'login', side_effect=lambda: setattr(cf, 'token', 'fresh')) as login, patch.object(publish.requests, 'request', side_effect=[Mock(json=lambda: {'code': code}), Mock(json=lambda: {'code': 200, 'data': {'material': {'id': '123'}}})]) as request:
        with publish.guarded_delivery(): assert cf.sync_status('123') == {'id': '123'}
        assert login.call_count == 1 and request.call_count == 2


@pytest.mark.integration
def test_poll_never_calls_ingest_even_without_material_id(api, workspace):
    batch, path, rid, video, record, entry = uncertain(workspace)
    entry.update(status='fbbm_queued', intent={'status': 'verified'})
    w.store_publication(path, rid, entry)
    cf = client(); cf.app_usage = Mock(return_value={'is_available': True})
    feishu = Mock(); feishu.get_record.return_value = {'record_id': 'saved-record', 'fields': {'OSS_URL': {'link': entry['publication']['oss_url']}}}
    creations = []
    def commit_but_lose_response(*args, **kwargs):
        creations.append(kwargs.get('json'))
        return Mock(json=lambda: {'code': 100006})
    job = {'id': 'a' * 32, 'batch_id': batch['id'], 'request_id': rid, 'phase': 'poll', 'attempt': 1, 'status': 'running', 'command': ['fixture']}
    job['input_fingerprint'] = stage_results.fingerprint(job, w.BATCHES, w.ROOT)
    web.save(job)
    with patch.object(publish, 'CfAdsClient', return_value=cf), patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(cf, 'login') as login, patch.object(publish.requests, 'request', side_effect=commit_but_lose_response):
        code = w.poll(batch['id'], rid)
        stage_results.complete(job, web.JOBS, w.BATCHES, w.ROOT, code)
        web.finish_job(job, code)
    assert len(creations) == 0
    assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_queued'
    assert read_json(path / 'publications.json')[rid]['intent']['status'] == 'verified'
    login.assert_not_called()
