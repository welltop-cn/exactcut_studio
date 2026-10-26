import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

from exactcut import llm_intake, publish, runtime, workflow as w
from exactcut.intake import read_excel
from exactcut.util import file_sha256, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'templates/精确剪辑_FBBM_填写模板.xlsx'
VIDEO_NAME = '20260101_typea_zhang_agent_1_#101573#.mp4'


def test_old_switch_does_not_open_new_clients(monkeypatch, tmp_path):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_EXTERNAL_WRITES', '1')
    for name in ('EXACTCUT_ALLOW_PUBLISH', 'EXACTCUT_ALLOW_AI', 'EXACTCUT_ALLOW_MATERIAL_FETCH'):
        monkeypatch.delenv(name, raising=False)
    request = Mock()
    bucket = Mock(side_effect=AssertionError('oss2.Bucket'))
    monkeypatch.setattr(publish.requests, 'post', request)
    import oss2
    monkeypatch.setattr(oss2, 'Bucket', bucket)
    video = tmp_path / VIDEO_NAME
    video.write_bytes(b'preview-bytes')
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_PUBLISH'):
        publish.OssPublisher().upload(video, dry_run=False)
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_PUBLISH'):
        publish.FeishuClient()
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_AI'):
        llm_intake.interpret('{}')
    request.assert_not_called()
    bucket.assert_not_called()
    dry = publish.OssPublisher().upload(video, dry_run=True)
    assert dry['dry_run'] is True
    assert dry['sha256'] == file_sha256(video)
    bucket.assert_not_called()


def test_development_can_fetch_when_the_switch_is_on(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    monkeypatch.delenv('EXACTCUT_ALLOW_MATERIAL_FETCH', raising=False)
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_MATERIAL_FETCH'):
        runtime.require_material_fetch()
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    runtime.require_material_fetch()
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_MATERIAL_FETCH'):
        runtime.require_material_fetch()


def test_feature_flags_follow_only_the_new_switches(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ALLOW_EXTERNAL_WRITES', '1')
    monkeypatch.delenv('EXACTCUT_ALLOW_PUBLISH', raising=False)
    monkeypatch.delenv('EXACTCUT_ALLOW_MATERIAL_FETCH', raising=False)
    monkeypatch.setenv('EXACTCUT_ALLOW_AI', '1')
    assert runtime.feature_flags() == {'publish': False, 'material_fetch': False, 'ai': True}


def test_review_sha_rejected_before_publish_flag_or_publications(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.delenv('EXACTCUT_ALLOW_PUBLISH', raising=False)
    monkeypatch.setattr(w, 'read_excel', read_excel)
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    item = w.import_excel({'filename': 'request.xlsx', 'content': base64.b64encode(TEMPLATE.read_bytes()).decode()})
    path = w.folder(item['id'])
    stored = w.read_json(path / 'draft.json')
    stored['draft']['author'] = '张三'
    write_json_atomic(path / 'draft.json', stored)
    batch = w.detail(item['id'])
    w.start_batch(batch['id'], batch['revision'])
    manifest = w.read_json(path / 'confirmed.json')
    request = manifest['requests'][0]
    video = path / 'production/videos' / request['output_filename']
    video.parent.mkdir(parents=True)
    video.write_bytes(b'pretend-master')
    master = file_sha256(video)
    review = 'a' * 64
    write_json_atomic(path / 'production/results.json', {'requests': {request['request_id']: {
        'status': 'qc_passed', 'output_sha256': master, 'review': {'sha256': review}}}})
    w.update(path, 'review', 'ready')
    with pytest.raises(ValueError, match='不能使用审片文件的校验值上传'):
        w.queue_publish(batch['id'], request['request_id'], review)
    assert not (path / 'publications.json').exists()
