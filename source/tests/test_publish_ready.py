from unittest.mock import Mock
from exactcut import publish


def test_completed_automation_result_is_read_without_writes(monkeypatch):
    feishu = Mock()
    feishu.get_record.return_value = {'record_id': 'existing-record', 'fields': {
        'OSS_URL': {'link': 'https://fixture.invalid/video'}, 'ADS素材ID': '67437',
        '状态': '已完成', '视频ID': '1039953452399914'}}
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    ads = Mock(side_effect=AssertionError('Do not log into FBBM'))
    monkeypatch.setattr(publish, 'CfAdsClient', ads)
    result = publish.sync_record_once('existing-record')
    assert result['action'] == 'ready'
    assert result['video_id'] == '1039953452399914'
    feishu.update.assert_not_called()
    ads.assert_not_called()
