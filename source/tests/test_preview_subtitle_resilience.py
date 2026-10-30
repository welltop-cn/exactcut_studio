import json
from types import SimpleNamespace

import pytest

from exactcut import preview_subtitle_model as model, preview_subtitles


CUE = {'start_ms': 100, 'end_ms': 500, 'original_text': 'Hello', 'translation_zh': '你好'}


def setup_model(tmp_path, monkeypatch, prepare, ask):
    from google import genai
    cfg = preview_subtitles.settings()
    cfg.update(max_inline_request_bytes=100000)
    monkeypatch.setattr(genai, 'Client', lambda **_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(model, 'prepare_chunk', prepare)
    monkeypatch.setattr(model, 'ask', ask)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    return source, cfg


def response(cues):
    text = json.dumps({'cues': cues})
    return SimpleNamespace(text=text, model_dump=lambda **_: {'text': text})


def test_oversize_proxy_splits_before_api_call_with_complete_coverage(tmp_path, monkeypatch):
    prepared, requested, progress = [], [], []
    def prepare(source, target, offset, duration, cfg):
        prepared.append((offset, duration))
        target.write_bytes(b'x' * (100000 if duration > 30000 else 10))
    def ask(client, proxy, duration, cfg, deadline):
        assert model.inline_request_bytes(proxy.stat().st_size) <= cfg['max_inline_request_bytes']
        requested.append(duration)
        return response([CUE])
    source, cfg = setup_model(tmp_path, monkeypatch, prepare, ask)
    cues = model.recognize(source, 60000, cfg, 'fixture', lambda **x: progress.append(x), tmp_path)
    assert prepared == [(0, 60000), (0, 30000), (30000, 30000)]
    assert requested == [30000, 30000]
    assert [cue['start_ms'] for cue in cues] == [100, 30100]
    assert progress[-1]['completed_chunks'] == 2
    assert not list(tmp_path.glob('chunk-*.mp4'))


def test_invalid_segment_retries_only_that_segment_once(tmp_path, monkeypatch):
    requested = []
    def ask(client, proxy, duration, cfg, deadline):
        requested.append(duration)
        if len(requested) == 2:
            return response([dict(CUE, start_ms=33500, end_ms=3500)])
        return response([CUE])
    source, cfg = setup_model(tmp_path, monkeypatch, lambda s,t,*_: t.write_bytes(b'proxy'), ask)
    result = model.recognize(source, 120000, cfg, 'fixture', lambda **_: None, tmp_path)
    assert requested == [60000, 60000, 60000]
    assert result == [CUE, dict(CUE, start_ms=60100, end_ms=60500)]


def test_invalid_segment_second_failure_remains_explicit(tmp_path, monkeypatch):
    calls = []
    def ask(*_):
        calls.append(True)
        return response([dict(CUE, start_ms=33500, end_ms=3500)])
    source, cfg = setup_model(tmp_path, monkeypatch, lambda s,t,*_: t.write_bytes(b'proxy'), ask)
    with pytest.raises(model.RecognitionError, match='字幕文字或时间无效'):
        model.recognize(source, 60000, cfg, 'fixture', lambda **_: None, tmp_path)
    assert len(calls) == 2


def test_completed_segments_survive_failure_and_are_reused_on_retry(tmp_path, monkeypatch):
    requested = []
    def ask(client, proxy, duration, cfg, deadline):
        requested.append(duration)
        if len(requested) == 2:
            raise RuntimeError('provider failure')
        return response([CUE])
    source, cfg = setup_model(tmp_path, monkeypatch, lambda s,t,*_: t.write_bytes(b'proxy'), ask)
    cache = tmp_path/'segments'
    with pytest.raises(RuntimeError):
        model.recognize(source, 120000, cfg, 'fixture', lambda **_: None, tmp_path, cache)
    result = model.recognize(source, 120000, cfg, 'fixture', lambda **_: None, tmp_path, cache)
    assert len(requested) == 3  # first valid segment is never requested twice
    assert len(result) == 2
    source.write_bytes(b'replaced')
    model.recognize(source, 120000, cfg, 'fixture', lambda **_: None, tmp_path, cache)
    assert len(requested) == 5  # different video cannot use the previous cache


def test_oversize_minimum_segment_fails_without_paid_request(tmp_path, monkeypatch):
    source, cfg = setup_model(tmp_path, monkeypatch, lambda s,t,*_: t.write_bytes(b'x'*100000),
                              lambda *_: pytest.fail('oversized request must not reach provider'))
    with pytest.raises(model.RecognitionError, match='预算'):
        model.recognize(source, 1000, cfg, 'fixture', lambda **_: None, tmp_path)
