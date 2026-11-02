"""Friendly messages at shared response boundaries, with diagnostics kept separate."""
import json

import pytest
from pydantic import ValidationError

from exactcut.hook_options import HookOptions
from exactcut import workflow as w
from test_web_base_path import app, login, request


@pytest.mark.parametrize('exc,expected', [
    (KeyError('request_id'), '提交内容不完整，请刷新页面后重试'),
    (FileNotFoundError('/srv/exactcut/private/source.mp4'), '所需文件暂时无法读取，请刷新后重试'),
    (RuntimeError('Command failed (1): ffmpeg -i /tmp/source.mp4'), '操作未完成，请稍后重试；如仍失败，请联系管理员'),
    (ValueError('编导姓名不能为空'), '编导姓名不能为空'),
])
def test_shared_api_error_has_plain_copy_and_separate_diagnostics(app, monkeypatch, exc, expected):
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr(w, 'create_blank', fail)
    status, _, body = request(app, 'POST', '/home/api/batches/blank', {}, login(app))
    assert status == 400
    data = json.loads(body)
    assert data['error'] == expected
    assert data['error_info']['detail']
    assert not any(word in data['error'] for word in ['KeyError', 'ffmpeg', '/srv/', '/tmp/'])


def test_model_exception_outside_draft_validation_gets_same_plain_copy(app, monkeypatch):
    def fail(*args, **kwargs):
        HookOptions(font_size=1)
    monkeypatch.setattr(w, 'create_blank', fail)
    status, _, body = request(app, 'POST', '/home/api/batches/blank', {}, login(app))
    assert status == 400
    data = json.loads(body)
    assert data['error'] == '字幕字号请填写 32～96 的整数'
    assert 'validation error' not in data['error']
    assert 'HookOptions' in data['error_info']['detail']


def test_public_message_preserves_business_guidance_and_hides_exception_dump():
    from exactcut.error_guidance import public_message
    assert public_message('请先保存当前修改，再恢复素材') == '请先保存当前修改，再恢复素材'
    assert public_message('识别失败：ConnectionError GET https://internal.example/path') == '操作未完成，请稍后重试；如仍失败，请联系管理员'
    assert public_message('') == ''
    assert public_message(ConnectionError('connection reset')) == '操作未完成，请稍后重试；如仍失败，请联系管理员'
