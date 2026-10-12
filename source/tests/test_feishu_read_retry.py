from unittest.mock import Mock, patch

import pytest
from exactcut import publish


def response(code=0, status=200):
    payload = {'code': code, 'msg': 'InternalError' if code else 'success',
               'data': {'record': {'record_id': 'saved-record', 'fields': {}}}}
    return Mock(status_code=status, ok=status < 400, json=lambda: payload)


def client():
    value = publish.FeishuClient.__new__(publish.FeishuClient)
    value.token = "fixture"; value._url = lambda record=None: 'https://fixture.invalid/' + str(record)
    return value


def test_internal_error_retries_only_same_record_get():
    with patch.object(publish.requests, 'get', side_effect=[response(1255001), response()]) as get, \
         patch.object(publish.time, 'sleep') as sleep:
        with publish.guarded_delivery():
            assert client().get_record('saved-record')['record_id'] == 'saved-record'
        assert get.call_count == 2
        assert get.call_args_list[0] == get.call_args_list[1]
        sleep.assert_called_once_with(1)


def test_persistent_internal_error_is_bounded_and_preserved():
    with patch.object(publish.requests, 'get', return_value=response(1255001)) as get, \
         patch.object(publish.time, 'sleep'):
        with pytest.raises(publish.PublishError, match='1255001'):
            client().get_record('saved-record')
        assert get.call_count == 3


def test_other_rejection_is_not_retried():
    with patch.object(publish.requests, 'get', return_value=response(999999)) as get, \
         patch.object(publish.time, 'sleep') as sleep:
        with pytest.raises(publish.PublishError, match='999999'):
            client().get_record('saved-record')
        assert get.call_count == 1
        sleep.assert_not_called()

from exactcut import workflow as w
from exactcut.util import read_json
from test_workflow import workspace
from test_round1_delivery import uncertain, readonly_clients


def polling_entry(workspace):
    batch, path, rid, _, _, entry = uncertain(workspace)
    entry['status'] = 'fbbm_queued'
    entry['intent']['status'] = 'verified'
    entry['fbbm'] = {'record_id': 'saved-record', 'material_id': '123', 'action': 'syncing'}
    w.store_publication(path, rid, entry)
    return batch, path, rid, entry


def test_transient_poll_switches_to_readonly_and_recovers_without_submission(workspace):
    batch, path, rid, entry = polling_entry(workspace)
    with patch.object(publish, 'sync_record_once', side_effect=publish.TransientDeliveryReadError('读取飞书记录失败：1255001')) as sync:
        assert w.poll(batch['id'], rid) == 0
        saved = read_json(path / 'publications.json')[rid]
        assert saved['status'] == 'fbbm_queued'
        assert saved['verification_only']['material_id'] == '123'
        assert '自动重试' in saved['error']
        assert not w.delivery_blocked(saved)
        feishu, ads = readonly_clients()
        with patch.object(publish, 'FeishuClient', return_value=feishu), patch.object(publish, 'CfAdsClient', return_value=ads):
            assert w.poll(batch['id'], rid) == 0
        assert sync.call_count == 1
        ads.ingest.assert_not_called(); ads.resync.assert_not_called(); feishu.update.assert_not_called()
    saved = read_json(path / 'publications.json')[rid]
    assert saved['status'] == 'fbbm_ready' and not saved.get('error')


@pytest.mark.parametrize('missing', [True, False])
def test_read_retry_never_bypasses_missing_or_conflicting_binding(workspace, missing):
    batch, path, rid, entry = polling_entry(workspace)
    if missing:
        entry.pop('fbbm'); entry['intent'].pop('receipt')
    else:
        entry['verification_only'] = {**w.verification_binding(entry), 'material_id': 'other'}
    w.store_publication(path, rid, entry)
    with patch.object(publish, 'sync_record_once', side_effect=publish.TransientDeliveryReadError('temporary')):
        assert w.poll(batch['id'], rid) == 2
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'


def test_temporary_query_stops_at_original_deadline(workspace):
    batch, path, rid, entry = polling_entry(workspace)
    entry.update(verification_only=w.verification_binding(entry), poll_started=1)
    w.store_publication(path, rid, entry)
    with patch.object(publish, 'verify_record_once') as verify:
        assert w.poll(batch['id'], rid) == 2
        verify.assert_not_called()
    assert read_json(path / 'publications.json')[rid]['status'] == 'unverified'


@pytest.mark.parametrize('failure', [publish.requests.Timeout('timeout'), response(status=503), response(status=429)])
def test_temporary_read_failures_retry(failure):
    with patch.object(publish.requests, 'get', side_effect=[failure, response()]) as get, patch.object(publish.time, 'sleep'):
        assert client().get_record('saved-record')['record_id'] == 'saved-record'
        assert get.call_count == 2


@pytest.mark.parametrize('method,path', [('GET', '/api/v1/material/123/sync_status'), ('POST', '/api/v1/material/ingest_and_sync')])
def test_fbbm_timeout_classification_does_not_replay_write(method, path):
    ads = publish.CfAdsClient.__new__(publish.CfAdsClient)
    ads.base = 'https://fixture.invalid'; ads.token = 'fixture'; ads.timeout = 1
    with patch.object(publish.requests, 'request', side_effect=publish.requests.Timeout('lost')) as request:
        with publish.guarded_delivery():
            expected = publish.TransientDeliveryReadError if method == 'GET' else publish.requests.Timeout
            with pytest.raises(expected): ads.request(method, path)
        assert request.call_count == 1


def test_repeated_read_failures_preserve_deadline_and_clear_notice_when_syncing(workspace):
    batch, path, rid, entry = polling_entry(workspace)
    with patch.object(publish, 'sync_record_once', side_effect=publish.TransientDeliveryReadError('temporary')):
        assert w.poll(batch['id'], rid) == 0
    first = read_json(path / 'publications.json')[rid]
    with patch.object(publish, 'verify_record_once', side_effect=publish.TransientDeliveryReadError('temporary')):
        assert w.poll(batch['id'], rid) == 0
    second = read_json(path / 'publications.json')[rid]
    assert second['poll_started'] == first['poll_started']
    assert second['verification_history'] == first['verification_history']
    with patch.object(publish, 'verify_record_once', return_value={'record_id': 'saved-record', 'material_id': '123', 'action': 'syncing'}):
        assert w.poll(batch['id'], rid) == 0
    saved = read_json(path / 'publications.json')[rid]
    assert saved['status'] == 'fbbm_queued' and not saved.get('error')


def test_read_retry_guidance_does_not_request_reupload():
    from exactcut.error_guidance import guidance
    result = guidance('状态查询暂时失败，将自动重试：1255001', 'fbbm')
    assert result['code'] == 'delivery_read_retry'
    assert '无需重新上传' in result['action']
