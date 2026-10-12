import json

import pytest

from exactcut import llm_intake as m
from test_blank_batch_api import app, call


def response(items):
    return {'items': items}


def item(lines, episode=30, start=80000, end=107000, issue=''):
    return {'source_lines': lines, 'clips': [{'episode': episode, 'start_ms': start, 'end_ms': end}], 'issue': issue}


def mock_model(monkeypatch, result):
    def interpret(payload, **kwargs):
        assert json.loads(payload)['lines'][0]['text']
        return kwargs['schema'].model_validate(result)
    monkeypatch.setattr(m, 'interpret', interpret)


def test_text_preserves_order_repeats_and_milliseconds(monkeypatch):
    mock_model(monkeypatch, response([item([1]), item([2], 31, 12, 117001), item([3])]))
    result = m.read_clip_text('30 1:20-1:47\n31 0:00.012-1:57.001\n30 1:20-1:47')
    assert result['errors'] == []
    assert [c['episode'] for c in result['clips']] == ['30', '31', '30']
    assert result['clips'][1]['start_ms'] == '012'
    assert result['clips'][1]['end_ms'] == '001'


@pytest.mark.parametrize('items', [
    [item([1])], [item([2]), item([1])], [item([1]), item([1, 2])],
    [item([1]), item([2], issue='时间单位不明确')],
    [item([1]), item([2], start=1000, end=1000)],
    [item([1]), item([2], episode=501)],
    [item([1]), item([2], end=60000000)],
])
def test_invalid_or_missing_evidence_never_partially_fills(monkeypatch, items):
    mock_model(monkeypatch, response(items))
    result = m.read_clip_text('30 1:20-1:47\n需要核对的原文')
    assert result['errors']
    assert result['clips'] == []


@pytest.mark.parametrize('text', ['', '  ', None, 123, 'x' * 12001])
def test_input_rejected_before_model(monkeypatch, text):
    monkeypatch.setattr(m, 'interpret', lambda *a, **k: pytest.fail('must not call API'))
    with pytest.raises(ValueError):
        m.read_clip_text(text)


def test_failure_releases_slot_and_does_not_fallback(monkeypatch):
    def fail(*a, **k):
        raise ValueError('AI 服务不可用')
    monkeypatch.setattr(m, 'interpret', fail)
    with pytest.raises(ValueError, match='AI 服务不可用'):
        m.read_clip_text('30 1:20-1:47')
    assert m.SLOTS.acquire(blocking=False)
    m.SLOTS.release()


def test_api_auth_csrf_and_parse(app, monkeypatch):
    mock_model(monkeypatch, response([item([1])]))
    assert call(app, 'POST', '/api/clip-text', {'text': '30 1:20-1:47'}, auth=False)[0] == 401
    assert call(app, 'POST', '/api/clip-text', {'text': '30 1:20-1:47'}, csrf=False)[0] == 403
    status, _, body = call(app, 'POST', '/api/clip-text', {'text': '30 1:20-1:47'})
    assert status == 200
    assert json.loads(body)['clips'][0]['episode'] == '30'


def test_text_uses_existing_transport_with_its_own_schema(monkeypatch):
    import httpx
    monkeypatch.setattr(m, 'require_ai', lambda: None)
    monkeypatch.setattr(m, 'load_env_file', lambda path: None)
    monkeypatch.setattr(m.ai_rate, 'acquire', lambda: None)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://fixture.invalid/v1')
    monkeypatch.setenv('EXACTCUT_INTAKE_MODEL', 'fixture-model')
    original_client = httpx.Client
    def handle(request):
        body = json.loads(request.content)
        assert str(request.url) == 'https://fixture.invalid/v1/responses'
        assert body['model'] == 'fixture-model'
        assert body['store'] is False
        assert body['text']['format']['name'] == 'clip_text'
        assert 'items' in body['text']['format']['schema']['properties']
        assert body['instructions'] == m.TEXT_SYSTEM
        return httpx.Response(200, json={'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(response([item([1])]))}]}]})
    monkeypatch.setattr(m.httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    assert m.read_clip_text('第30集 1分20秒到1分47秒')['clips'][0]['episode'] == '30'
