from exactcut import publish


def test_ready_clears_date_with_null_and_reuses_material(monkeypatch):
    patches = []
    class Feishu:
        def get_record(self, record_id):
            return {'fields': {'ADS素材ID': '67437'}}
        def update(self, record_id, fields):
            patches.append(fields)
    class Ads:
        def app_usage(self):
            return {'is_available': True}
        def sync_status(self, material_id):
            assert material_id == '67437'
            return {'facebook_bm_sync_status': 'ready', 'facebook_bm_video_id': '1039953452399914'}
        def ingest(self, *a):
            raise AssertionError('Do not re-upload an existing material')
    monkeypatch.setattr(publish, 'FeishuClient', Feishu)
    monkeypatch.setattr(publish, 'CfAdsClient', Ads)
    result = publish.sync_record_once('existing-record')
    assert result['action'] == 'ready'
    assert patches[-1]['下次执行时间'] is None
    assert isinstance(patches[-1]['完成时间'], int)
    assert patches[-1]['状态'] == '已完成'
