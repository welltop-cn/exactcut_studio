import json

import pytest

from exactcut import preflight


def test_preflight_refuses_development_before_network(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    monkeypatch.setattr(preflight, 'check_oss', lambda *args: pytest.fail('network called'))
    with pytest.raises(RuntimeError, match='production'):
        preflight.run_checks('101460', 1)


def test_preflight_missing_config_does_not_call_services(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    for keys in preflight.REQUIRED.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    for name in ('check_oss', 'check_ai', 'check_feishu'):
        monkeypatch.setattr(preflight, name, lambda *args: pytest.fail('network called'))
    report = preflight.run_checks('101460', 1)
    assert report['status'] == 'failed'
    assert all(report['checks'][name]['status'] == 'missing_config' for name in ('oss', 'ai', 'feishu'))
    assert report['checks']['cf_ads']['ingestion_and_sync'] == 'not_verified'
    assert 'OPENAI_API_KEY' in report['checks']['ai']['missing']


def test_preflight_reports_every_failure_without_secret_or_response_body(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    for keys in preflight.REQUIRED.values():
        for key in keys:
            monkeypatch.setenv(key, 'fixture-secret')
    def fail(*args):
        raise RuntimeError('fixture-secret private-server-response')
    for name in ('check_oss', 'check_ai', 'check_feishu', 'check_cf_ads'):
        monkeypatch.setattr(preflight, name, fail)
    report = preflight.run_checks('101460', 1)
    assert all(item['status'] == 'failed' for item in report['checks'].values())
    assert 'fixture-secret' not in json.dumps(report)
    assert 'private-server-response' not in json.dumps(report)


def test_feishu_preflight_reads_schema_without_fixing_it(monkeypatch):
    from exactcut.publish import FEISHU_FIELD_DEFS, STATUS_FIELD
    class Client:
        def fields(self):
            return {field['field_name']: dict(field) for field in [STATUS_FIELD, *FEISHU_FIELD_DEFS]}
    monkeypatch.setattr(preflight, 'FeishuClient', Client)
    assert preflight.check_feishu()['status'] == 'ok'
    monkeypatch.setattr(Client, 'fields', lambda self: {'状态': {'type': 1}})
    result = preflight.check_feishu()
    assert result['status'] == 'failed'
    assert result['wrong_types'] == ['状态']
    assert 'OSS_URL' in result['missing_fields']


def test_oss_probe_only_reads_bounded_ranges_without_copy_or_upload(monkeypatch):
    from types import SimpleNamespace
    calls = []
    class Stream:
        def __init__(self):
            self.remaining = 1024**2
        def read(self, size):
            chunk = b'x' * min(size, self.remaining)
            self.remaining -= len(chunk)
            return chunk
        def close(self):
            calls.append('closed')
    class Bucket:
        def __init__(self, auth, endpoint, name, **kwargs):
            self.bucket_name = name
            self.session = SimpleNamespace(session=SimpleNamespace(trust_env=True))
        def head_object(self, key):
            assert self.session.session.trust_env is False
            calls.append(('HEAD', key))
            return SimpleNamespace(content_length=10 * 1024**2)
        def get_object(self, key, byte_range):
            calls.append(('GET', key, byte_range))
            return Stream()
    for key in preflight.REQUIRED['oss']:
        monkeypatch.setenv(key, 'fixture')
    monkeypatch.delenv('ALIYUN_OSS_SOURCE_BUCKET', raising=False)
    monkeypatch.setattr(preflight.oss2, 'Bucket', Bucket)
    result = preflight.check_oss('101460', 1)
    assert result['status'] == 'ok'
    assert len(result['downloads']) == 2
    assert all(item['sample_bytes'] == 1024**2 for item in result['downloads'])
    assert [item[0] for item in calls if isinstance(item, tuple)] == ['HEAD', 'GET', 'HEAD', 'GET']
    assert calls.count('closed') == 2
    assert all(item[2] == (0, 1024**2 - 1) for item in calls if isinstance(item, tuple) and item[0] == 'GET')


def test_fbbm_preflight_does_not_login_or_require_quota(monkeypatch):
    monkeypatch.setattr(preflight, 'CfAdsClient', lambda: pytest.fail('FBBM belongs to automation'))
    assert preflight.check_cf_ads()['status'] == 'ok'
    assert preflight.REQUIRED['cf_ads'] == ()
