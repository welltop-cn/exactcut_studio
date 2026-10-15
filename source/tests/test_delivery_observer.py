"""The external Feishu automation is the sole FBBM writer."""
from unittest.mock import Mock
import pytest
from exactcut import publish


@pytest.fixture
def clients(monkeypatch):
    feishu = Mock()
    feishu.get_record.return_value = {'record_id': 'record', 'fields': {
        'OSS_URL': {'link': 'https://fixture.invalid/video'}, '状态': '待处理'}}
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    monkeypatch.setattr(publish, 'CfAdsClient', Mock(side_effect=AssertionError('No FBBM login or API')))
    feishu.update.side_effect = AssertionError('Automation owns record updates')
    return feishu


@pytest.mark.parametrize('state,material,video,action', [
    ('待处理', '', '', 'syncing'), ('处理中', '123', '', 'syncing'),
    ('同步中', '123', 'video', 'syncing'), ('已完成', '123', 'video', 'ready'),
    ('已完成', '', 'video', 'syncing'), ('已完成', '123', '', 'syncing'),
    ('失败', '123', '', 'failed'), ('超时', '123', '', 'failed'),
    ('暂停', '', '', 'syncing'), ('unknown', '123', 'video', 'syncing'),
])
def test_poll_only_observes_automation(clients, state, material, video, action):
    clients.get_record.return_value['fields'].update({'状态': state, 'ADS素材ID': material, '视频ID': video})
    result = publish.sync_record_once('record')
    assert result['action'] == action
    assert result['material_id'] == material
    clients.update.assert_not_called()
    clients.create_or_reuse.assert_not_called()


def test_verification_reads_feishu_without_fbbm(clients):
    clients.get_record.return_value['fields'].update({'状态': '已完成', 'ADS素材ID': '123', '视频ID': 'video'})
    result = publish.verify_record_once({'record_id': 'record', 'material_id': '123', 'oss_url': 'https://fixture.invalid/video'})
    assert result['action'] == 'ready'


@pytest.mark.parametrize('field,value', [('OSS_URL', {'link': 'https://wrong.invalid'}), ('ADS素材ID', '456')])
def test_verification_keeps_identity_protection(clients, field, value):
    clients.get_record.return_value['fields'].update({'状态': '已完成', 'ADS素材ID': '123', '视频ID': 'video', field: value})
    with pytest.raises(publish.PublishError, match='不一致'):
        publish.verify_record_once({'record_id': 'record', 'material_id': '123', 'oss_url': 'https://fixture.invalid/video'})

from test_workflow import workspace
from test_round1_delivery import uncertain
from exactcut import workflow as w
from exactcut.util import read_json


def test_query_outage_before_worker_assigns_id_is_retryable(workspace, monkeypatch):
    batch, path, rid, _, _, entry = uncertain(workspace)
    entry['status'] = 'fbbm_queued'
    entry['intent'] = {'status': 'verified'}
    w.store_publication(path, rid, entry)
    monkeypatch.setattr(publish, 'sync_record_once', Mock(side_effect=publish.TransientDeliveryReadError('temporary')))
    assert w.poll(batch['id'], rid) == 0
    saved = read_json(path / 'publications.json')[rid]
    assert saved['status'] == 'fbbm_queued'
    assert saved['intent']['status'] == 'verified'
    assert not w.delivery_blocked(saved)


def test_reuse_preserves_automation_owned_fields(monkeypatch):
    client = publish.FeishuClient.__new__(publish.FeishuClient)
    row = {'record_id': 'record', 'fields': {'状态': '已完成', 'ADS素材ID': '123', '视频ID': 'video'}}
    client.search_by_url = Mock(return_value=row)
    client.fields = Mock(side_effect=AssertionError('No schema/write for existing row'))
    monkeypatch.setattr(publish.requests, 'post', Mock(side_effect=AssertionError('No duplicate create')))
    monkeypatch.setattr(publish.requests, 'put', Mock(side_effect=AssertionError('No reset to pending')))
    result = client.create_or_reuse({'OSS_URL': {'link': 'https://fixture.invalid/video'}, '状态': '待处理'})
    assert result['action'] == 'reused'
    assert result['fields'] == row['fields']
    client.search_by_url.assert_called_once_with('https://fixture.invalid/video')


def test_normal_poll_rejects_changed_url_before_accepting_result(clients):
    clients.get_record.return_value['fields'].update({'状态': '已完成', 'ADS素材ID': '123', '视频ID': 'video'})
    with pytest.raises(publish.PublishError, match='不一致'):
        publish.sync_record_once('record', expected_url='https://other.invalid/video')


@pytest.mark.parametrize('recovery', ['finish', 'restart'])
@pytest.mark.parametrize('material_id', ['', '123'])
def test_observer_process_exit_requeues(workspace, monkeypatch, recovery, material_id):
    import multiprocessing
    import os
    from exactcut import webserver as web
    batch, path, rid, _, _, entry = uncertain(workspace)
    entry.update(status='fbbm_queued', intent={'status': 'verified'})
    if material_id: entry['intent']['receipt'] = {'record_id': 'saved-record', 'material_id': material_id}
    w.store_publication(path, rid, entry)
    feishu = Mock()
    feishu.get_record.side_effect = lambda record: os._exit(71)
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    monkeypatch.setattr(publish, 'CfAdsClient', Mock(side_effect=AssertionError('No FBBM')))
    job = {'id': 'e' * 32, 'batch_id': batch['id'], 'request_id': rid, 'phase': 'poll',
           'action': 'publish', 'status': 'running', 'attempt': 1, 'command': ['fixture']}
    monkeypatch.setattr(web, 'JOBS', w.ROOT / '.web-state/jobs')
    web.save(job)
    child = multiprocessing.get_context('fork').Process(target=w.poll, args=(batch['id'], rid))
    child.start(); child.join(10)
    try:
        assert child.exitcode == 71
        assert not w.delivery_blocked(read_json(path / 'publications.json')[rid])
        if recovery == 'finish': web.finish_job(job, 71)
        else: web.recover_jobs(enqueue=False)
        assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'queued'
        started = read_json(path / 'publications.json')[rid]['poll_started']
        feishu.get_record.side_effect = lambda record: {'record_id': 'saved-record', 'fields': {
            'OSS_URL': {'link': 'https://fixture.invalid/video'}, '状态': '已完成', 'ADS素材ID': '123', '视频ID': 'video'}}
        assert w.poll(batch['id'], rid) == 0
        saved = read_json(path / 'publications.json')[rid]
        assert saved['status'] == 'fbbm_ready' and saved['poll_started'] == started
    finally:
        if child.is_alive(): child.kill(); child.join()


def test_enqueue_writes_only_four_user_requested_fields(monkeypatch, tmp_path):
    from types import SimpleNamespace
    upload = {'oss_url': 'https://fixture.invalid/video', 'oss_key': 'fixture-key', 'sha256': 'sha'}
    monkeypatch.setattr(publish.OssPublisher, 'upload', Mock(return_value=upload))
    monkeypatch.setenv('OPENCLAW_SOURCE_USER', 'fixture-source')
    feishu = Mock()
    feishu.create_or_reuse.return_value = {'record_id': 'record', 'action': 'created'}
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    publish.publish_to_queue(tmp_path/'video.mp4', SimpleNamespace(author='author', drama_id='101628', business_date='20261015'), SimpleNamespace(seq='2'))
    feishu.create_or_reuse.assert_called_once_with({
        '剧ID': '101628', 'OSS_URL': {'link': upload['oss_url'], 'text': upload['oss_url']},
        '状态': '待处理', '来源': 'fixture-source'})
