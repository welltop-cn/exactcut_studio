import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from exactcut import hook_emphasis as semantic
from exactcut.errors import ValidationError
from exactcut.hook_visual import AUTO_STYLE_IDS, _accent_runs, choose_hook_visual, render_personalized_hook_png

TEXT = 'They abandoned the true heiress, but she returned as the Zombie Queen!'
RESPONSE = {'style': 'cold_mystery', 'highlights': [
    {'phrase': 'true heiress', 'occurrence': 1, 'role': 'identity'},
    {'phrase': 'Zombie Queen', 'occurrence': 1, 'role': 'reversal'},
], 'reason': '强调受害者的真实身份与归来的反转身份'}


@pytest.fixture(autouse=True)
def independent_ai_model_budget(monkeypatch):
    # Model-contract tests mock transport; the real bucket is tested in test_step3_resources.
    monkeypatch.setattr(semantic.ai_rate, 'acquire', lambda: None)


def permit_mock(monkeypatch):
    monkeypatch.setattr(semantic, 'require_ai', lambda: None)
    monkeypatch.setattr(semantic, 'load_env_file', lambda path: None)
    monkeypatch.setenv('EXACTCUT_HOOK_MODEL', 'mock-semantic-model')


def test_cached_semantic_decision_reused_and_changed_text_reanalyzed(tmp_path, monkeypatch):
    permit_mock(monkeypatch)
    calls = []
    def analyze(text, model):
        calls.append((text, model))
        return RESPONSE
    monkeypatch.setattr(semantic, '_request_analysis', analyze)
    first = semantic.resolve_emphasis(TEXT, tmp_path)
    reused = semantic.resolve_emphasis(TEXT, tmp_path)
    changed = semantic.resolve_emphasis(TEXT + ' She won.', tmp_path)
    assert len(calls) == 2
    assert not first['cache_reused'] and reused['cache_reused']
    assert first['highlights'] == reused['highlights']
    assert changed['cache_key'] != first['cache_key']
    for span in first['highlights']:
        assert first['text'][span['start']:span['end']] == span['phrase']
    monkeypatch.setenv('EXACTCUT_HOOK_MODEL', 'another-model')
    assert semantic.resolve_emphasis(TEXT, tmp_path)['cache_key'] != first['cache_key']


def test_concurrent_calls_share_one_durable_decision(tmp_path, monkeypatch):
    permit_mock(monkeypatch)
    calls = []
    monkeypatch.setattr(semantic, '_request_analysis', lambda *args: calls.append(args) or RESPONSE)
    with ThreadPoolExecutor(max_workers=4) as pool:
        decisions = list(pool.map(lambda _: semantic.resolve_emphasis(TEXT, tmp_path), range(4)))
    assert len(calls) == 1
    assert all(item['highlights'] == decisions[0]['highlights'] for item in decisions)


@pytest.mark.parametrize('highlights', [
    [{'phrase': 'the evil queen', 'occurrence': 1, 'role': 'identity'}],
    [{'phrase': 'True Heiress', 'occurrence': 1, 'role': 'identity'}],
    [{'phrase': 'heir', 'occurrence': 1, 'role': 'identity'}],
    [{'phrase': 'true heiress', 'occurrence': 0, 'role': 'identity'}],
    [RESPONSE['highlights'][0], RESPONSE['highlights'][0]],
    [{'phrase': TEXT, 'occurrence': 1, 'role': 'identity'}],
    [RESPONSE['highlights'][0], RESPONSE['highlights'][1],
     {'phrase': 'abandoned', 'occurrence': 1, 'role': 'conflict'}],
])
def test_hallucinated_partial_word_overlapping_or_excessive_emphasis_rejected(highlights):
    with pytest.raises(ValidationError):
        semantic.grounded_analysis(TEXT, {**RESPONSE, 'highlights': highlights})


def test_repeated_phrase_targets_only_the_selected_occurrence():
    text = 'She kept her secret, while he exposed her secret.'
    result = semantic.grounded_analysis(text, {'style': 'cold_mystery', 'highlights': [
        {'phrase': 'her secret', 'occurrence': 2, 'role': 'conflict'}], 'reason': '强调被揭露的秘密'})
    assert result['highlights'][0]['start'] == text.rfind('her secret')


def test_manual_marks_win_without_any_external_call(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, 'require_ai', lambda: pytest.fail('Manual text needs no AI'))
    text = 'She kept her secret, but **her secret** made her **Queen**.'
    result = semantic.resolve_emphasis(text, tmp_path)
    assert result['source'] == 'manual'
    assert [span['phrase'] for span in result['highlights']] == ['her secret', 'Queen']
    assert result['highlights'][0]['start'] == result['text'].rfind('her secret')


@pytest.mark.parametrize('text, starts', [('**a** aa**a**', [0, 4]), ('foo** bar**', [4]),
                                        ('**her** **secret**', [0, 4])])
def test_manual_marks_use_exact_positions_even_next_to_repeated_text(text, starts):
    result = semantic.manual_emphasis(text)
    assert [span['start'] for span in result['highlights']] == starts


def test_isolated_environment_cannot_call_service_or_reuse_ai_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(semantic, '_request_analysis', lambda *args: pytest.fail('Network must be blocked'))
    with pytest.raises(RuntimeError, match='禁用'):
        semantic.resolve_emphasis(TEXT, tmp_path)


def test_service_failure_does_not_save_or_fall_back_to_keywords(tmp_path, monkeypatch):
    permit_mock(monkeypatch)
    monkeypatch.setattr(semantic, '_request_analysis', lambda *args: (_ for _ in ()).throw(ValidationError('AI 失败')))
    with pytest.raises(ValidationError, match='AI 失败'):
        semantic.resolve_emphasis(TEXT, tmp_path)
    assert not list(tmp_path.glob('*.json'))


def test_responses_request_uses_strict_schema_only_hook_text_and_bounded_timeout(monkeypatch):
    permit_mock(monkeypatch)
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://mock.invalid/v1')
    real_client = httpx.Client
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == '/v1/responses'
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(RESPONSE)}]}]})
    def client(**kwargs):
        assert kwargs['timeout'].read == 45 and kwargs['timeout'].connect == 10
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(semantic.httpx, 'Client', client)
    assert semantic._request_analysis(TEXT, 'mock-model') == RESPONSE
    assert seen[0]['store'] is False and seen[0]['text']['format']['strict'] is True
    supplied=json.loads(seen[0]['input'][0]['content'])
    assert supplied['text']==TEXT and supplied['highlight_limits']['max_phrases']==2
    assert supplied['highlight_limits']['max_characters']==int(len(TEXT)*.4)


def test_highlight_survives_wrapping_across_two_lines():
    spans = [{'start': 7, 'end': 17}]
    assert _accent_runs('She is her', 0, spans) == [('She is ', False), ('her', True)]
    assert _accent_runs('secret keeper', 11, spans) == [('secret', True), (' keeper', False)]


@pytest.mark.parametrize('style', sorted(AUTO_STYLE_IDS | {'default'}))
def test_every_style_draws_local_highlights_including_neon_and_diary(tmp_path, style):
    result = semantic.grounded_analysis(TEXT, RESPONSE)
    decision = choose_hook_visual({'hook_text': TEXT}, {}, forced_style=style, emphasis=result)
    assert decision['selection_source'] == 'ai_semantic'
    output = render_personalized_hook_png(TEXT, tmp_path, decision)
    assert ' '.join(decision['layout']['lines']) == TEXT
    accent = decision['palette']['accent']
    rgb = tuple(int(accent[i:i+2], 16) for i in (1, 3, 5))
    with Image.open(output) as image:
        assert sum(count for count, color in image.getcolors(image.width * image.height) if color[:3] == rgb) > 20
    left, top, right, bottom = decision['layout']['text_bbox']
    assert left >= 75 and right <= 1005 and bottom - top <= int(1920 * 0.18)


def test_semantic_style_overrides_substring_keyword_rules():
    text = 'Their love gave her hope again.'
    result = semantic.grounded_analysis(text, {'style': 'emotional_diary', 'highlights': [
        {'phrase': 'hope', 'occurrence': 1, 'role': 'consequence'}], 'reason': '希望是情感结果'})
    decision = choose_hook_visual({'hook_text': text}, {}, emphasis=result)
    assert decision['style_id'] == 'emotional_diary'
    assert decision['matched_signals'] == []


def test_multiline_manual_phrase_is_preserved_and_highlighted(tmp_path):
    text = 'She knows **her\nsecret** and keeps it hidden.'
    result = semantic.manual_emphasis(text)
    decision = choose_hook_visual({'hook_text': text}, {}, emphasis=result)
    render_personalized_hook_png(text, tmp_path, decision)
    assert ' '.join(decision['layout']['lines']) == semantic.plain_hook(text)
    assert result['highlights'][0]['phrase'] == 'her secret'


def test_production_analysis_failure_stops_before_media_processing(tmp_path, monkeypatch):
    from exactcut import renderer
    from exactcut.util import load_config
    monkeypatch.setattr(renderer, 'environment', lambda: 'production')
    monkeypatch.setattr(renderer, 'resolve_emphasis', lambda *args: (_ for _ in ()).throw(ValidationError('AI 失败')))
    monkeypatch.setattr(renderer, '_render_episode_unit', lambda *args: pytest.fail('Must not cut before AI succeeds'))
    with pytest.raises(ValidationError, match='AI 失败'):
        renderer.render_request(tmp_path, SimpleNamespace(hook_text=TEXT), [], tmp_path/'output.mp4', load_config())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')
def test_production_render_uses_semantic_choice_and_preserves_video_duration(tmp_path, monkeypatch):
    from exactcut import renderer
    from exactcut.media import ffprobe
    from exactcut.util import load_config
    result = semantic.grounded_analysis(TEXT, RESPONSE)
    monkeypatch.setattr(renderer, 'environment', lambda: 'production')
    monkeypatch.setattr(renderer, 'resolve_emphasis', lambda text, cache: result)
    config = load_config()
    config['render']['preset'] = 'ultrafast'
    request = SimpleNamespace(hook_text=TEXT, request_id='R001')
    output = tmp_path/'semantic-hook.mp4'
    plan = renderer.render_request(tmp_path, request,
        [{'type': 'title_card', 'text': 'Semantic Hook acceptance', 'duration_ms': 1500}], output, config)
    assert plan['hook_visual']['mode'] == 'semantic_frame_auto'
    assert plan['hook_visual']['emphasis']['highlights'] == result['highlights']
    assert ' '.join(plan['hook_visual']['layout']['lines']) == TEXT
    info = ffprobe(output)
    assert abs(float(info['format']['duration']) - 1.5) < 0.1
    assert any(stream['codec_type'] == 'audio' for stream in info['streams'])


def test_retry_only_for_transient_failure(monkeypatch):
    monkeypatch.setattr(semantic.time,'sleep',lambda _:None)
    calls=[]
    def operation():
        calls.append(1)
        if len(calls)==1:
            raise semantic.TransientHookAIError('busy')
        return 'okay'
    assert semantic.retry_ai(operation)=='okay' and len(calls)==2
    calls.clear()
    def permanent():
        calls.append(1)
        raise ValidationError('bad configuration')
    with pytest.raises(ValidationError):
        semantic.retry_ai(permanent)
    assert len(calls)==1
    calls.clear()
    def always_busy():
        calls.append(1)
        raise semantic.TransientHookAIError('busy')
    with pytest.raises(semantic.TransientHookAIError):
        semantic.retry_ai(always_busy)
    assert len(calls)==2


@pytest.mark.parametrize('status',[429,503,401])
def test_http_retry_distinguishes_temporary_service_failure_from_credentials(monkeypatch,status,tmp_path):
    import httpx
    monkeypatch.setattr(semantic,'require_ai',lambda:None)
    monkeypatch.setattr(semantic,'load_env_file',lambda _:None)
    monkeypatch.setattr(semantic.time,'sleep',lambda _:None)
    monkeypatch.setenv('OPENAI_API_KEY','independent-unit-test')
    calls=[]
    real_client=httpx.Client
    def handle(req):
        calls.append(1)
        if len(calls)==1:
            return httpx.Response(status,json={'error':'fixture'})
        return httpx.Response(200,json={'output':[{'type':'message','content':[{'type':'output_text','text':'{"test":true}'}]}]})
    monkeypatch.setattr(semantic.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw))
    operation=lambda:semantic.responses_json('test','text',{},'test','test-model')
    if status==401:
        with pytest.raises(ValidationError,match='配置'):
            semantic.retry_ai(operation)
        assert len(calls)==1
    else:
        assert semantic.retry_ai(operation)=={'test':True}
        assert len(calls)==2


def test_invalid_emphasis_gets_one_grounded_correction_without_rewriting_copy(monkeypatch):
    calls=[]
    bad={'style':'default','highlights':[{'phrase':TEXT,'occurrence':1,'role':'conflict'}],'reason':'too much'}
    def analyse(clean,model,correction=None):
        calls.append((clean,correction))
        return bad if len(calls)==1 else RESPONSE
    monkeypatch.setattr(semantic,'_request_analysis',analyse)
    response,result=semantic.analyze_grounded(TEXT,'test')
    assert len(calls)==2 and all(c[0]==TEXT for c in calls)
    assert calls[1][1] and result['text']==TEXT and response==RESPONSE


def test_transient_then_invalid_response_never_exceeds_two_service_calls(monkeypatch):
    calls=[]
    monkeypatch.setattr(semantic.time,'sleep',lambda _:None)
    def analyse(*args):
        calls.append(1)
        if len(calls)==1:
            raise semantic.TransientHookAIError('temporary')
        return {'style':'default','highlights':[{'phrase':'not in source','occurrence':1,'role':'conflict'}],'reason':''}
    monkeypatch.setattr(semantic,'_request_analysis',analyse)
    with pytest.raises(ValidationError,match='手工'):
        semantic.analyze_grounded(TEXT,'test')
    assert len(calls)==2
