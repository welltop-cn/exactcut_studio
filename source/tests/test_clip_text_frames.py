import json

import httpx
import pytest
from pydantic import ValidationError

from exactcut import llm_intake as m
from test_blank_batch_api import app, call
from test_source_frames import make_video


def frame(episode=4, **changes):
    return {'episode': episode, 'start_m': 0, 'start_s': 0, 'start_f': 0,
            'end_m': 0, 'end_s': 50, 'end_f': 19, **changes}


def result(items=None):
    return {'items': items or [{'source_lines': [1], 'clips': [frame()], 'issue': ''}]}


def mock_frames(monkeypatch, clips=None, items=None):
    def interpret(payload, **kw):
        return kw['schema'].model_validate({'items': items if items is not None else [
            {'source_lines': [1], 'clips': clips if clips is not None else [frame()], 'issue': ''}]})
    monkeypatch.setattr(m, 'interpret', interpret)


@pytest.mark.parametrize('text', [
    'EP4 00:49:00-02:16:18',
    'EP4: 00:49:00-02:16:18,, EP5: 00:00:01-01:08:13',
    'EP4：00:49:00-02:16:18，EP5：00:00:01-01:08:13',
])
def test_all_frame_text_reaches_model_and_preserves_ordinals(monkeypatch, text):
    calls = []
    clips = [frame(start_s=49, end_m=2, end_s=16, end_f=18)]
    if 'EP5' in text:
        clips.append(frame(episode=5, start_f=1, end_m=1, end_s=8, end_f=13))
    def interpret(payload, **kw):
        calls.append(json.loads(payload))
        assert kw['schema'] is m.FrameTextInterpretation
        return kw['schema'].model_validate(result([{'source_lines': [1], 'clips': clips, 'issue': ''}]))
    monkeypatch.setattr(m, 'interpret', interpret)
    parsed = m.read_clip_text(text, time_format='source_frames_v1')
    assert calls == [{'lines': [{'line': 1, 'text': text}]}]
    assert parsed['errors'] == []
    assert len(parsed['clips']) == len(clips)
    assert parsed['clips'][0]['start_s'] == '49'
    assert parsed['clips'][0]['end_f'] == '018'
    if len(clips) == 2:
        assert parsed['clips'][1]['episode'] == '5'
        assert parsed['clips'][1]['start_f'] == '001'
        assert parsed['clips'][1]['end_f'] == '013'


@pytest.mark.parametrize('text', ['EP4 00:01.019-00:02.012',
    'EP4 00:01:019-00:02:012 毫秒', 'EP4 00:00:01:19-00:00:02:12',
    'SMPTE EP4 00:01:019-00:02:012'])
def test_unit_questions_are_sent_to_model_and_issues_never_partially_fill(monkeypatch, text):
    calls = []
    def interpret(payload, **kw):
        calls.append(json.loads(payload)['lines'][1]['text'])
        return kw['schema'].model_validate(result([
            {'source_lines': [1], 'clips': [frame()], 'issue': ''},
            {'source_lines': [2], 'clips': [], 'issue': '请明确分秒帧单位'}]))
    monkeypatch.setattr(m, 'interpret', interpret)
    parsed = m.read_clip_text('EP1 00:00-00:01\n' + text, time_format='source_frames_v1')
    assert calls == [text]
    assert parsed['clips'] == [] and '请明确分秒帧单位' in parsed['errors'][0]


@pytest.mark.parametrize('changes', [{'episode': 501}, {'start_s': 60}, {'start_f': 1000},
    {'end_m': 1000}, {'start_f': -1}, {'start_f': '19'}])
def test_model_frame_values_must_satisfy_strict_schema(changes):
    with pytest.raises(ValidationError):
        m.FrameTextInterpretation.model_validate(result([{'source_lines': [1], 'clips': [frame(**changes)], 'issue': ''}]))


def test_frame_order_and_single_frame_validation(monkeypatch):
    mock_frames(monkeypatch, [frame(start_s=50, start_f=20)])
    parsed = m.read_clip_text('EP4 00:50:020-00:50:019', time_format='source_frames_v1')
    assert parsed['clips'] == [] and parsed['errors']
    mock_frames(monkeypatch, [frame(start_s=50, start_f=19)])
    assert m.read_clip_text('EP4 00:50:019-00:50:019', time_format='source_frames_v1')['errors'] == []


def test_frame_clip_limit_and_unknown_modes(monkeypatch):
    mock_frames(monkeypatch, [frame()] * 30)
    assert len(m.read_clip_text('EP4 00:00-00:01', time_format='source_frames_v1')['clips']) == 30
    mock_frames(monkeypatch, [frame()] * 31)
    parsed = m.read_clip_text('EP4 00:00-00:01', time_format='source_frames_v1')
    assert parsed['errors'] and parsed['clips'] == []
    for mode in (None, 0, [], 'frames'):
        with pytest.raises(ValueError, match='格式'):
            m.read_clip_text('EP4 00:00-00:01', time_format=mode)


@pytest.mark.parametrize('lines', [[1], [2, 1], [1, 1, 2]])
def test_complex_frame_result_requires_complete_ordered_source_evidence(monkeypatch, lines):
    def interpret(payload, **kw):
        assert len(json.loads(payload)['lines']) == 2
        return kw['schema'].model_validate(result([{'source_lines': lines, 'clips': [frame()], 'issue': ''}]))
    monkeypatch.setattr(m, 'interpret', interpret)
    parsed = m.read_clip_text('第4集从片头剪到五十秒第十九帧\n第5集另取一段', time_format='source_frames_v1')
    assert parsed['clips'] == [] and parsed['errors']


def test_mixed_explicit_and_complex_lines_send_whole_text_to_frame_model(monkeypatch):
    def interpret(payload, **kw):
        assert json.loads(payload)['lines'] == [{'line': 1, 'text': 'EP4 00:00-00:01'}, {'line': 2, 'text': '不能识别的说明'}]
        return kw['schema'].model_validate(result([
            {'source_lines': [1], 'clips': [frame(end_s=1)], 'issue': ''},
            {'source_lines': [2], 'clips': [], 'issue': '请明确片段'}]))
    monkeypatch.setattr(m, 'interpret', interpret)
    parsed = m.read_clip_text('EP4 00:00-00:01\n不能识别的说明', time_format='source_frames_v1')
    assert parsed['clips'] == [] and parsed['errors']


def test_frame_mode_uses_independent_strict_schema_over_existing_transport(monkeypatch):
    monkeypatch.setattr(m, 'require_ai', lambda: None)
    monkeypatch.setattr(m, 'load_env_file', lambda path: None)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://fixture.invalid/v1')
    client = httpx.Client
    def handle(request):
        body = json.loads(request.content)
        assert body['text']['format']['name'] == 'clip_text_frames'
        schema = body['text']['format']['schema']
        fields = schema['$defs']['FrameTextClip']['properties']
        assert set(fields) == set(frame()) and 'start_ms' not in json.dumps(schema)
        assert fields['end_f']['maximum'] == 999
        assert body['instructions'] == m.FRAME_TEXT_SYSTEM
        assert body['instructions'] != m.TEXT_SYSTEM
        assert '结束帧包含' in body['instructions']
        return httpx.Response(200, json={'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(result())}]}]})
    monkeypatch.setattr(m.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    parsed = m.read_clip_text('第4集从片头剪到五十秒第十九帧', time_format='source_frames_v1')
    assert parsed['clips'][0]['end_f'] == '019'
    with pytest.raises(ValidationError):
        m.FrameTextClip.model_validate(frame(start_f='19'))


def test_http_requires_auth_csrf_and_passes_explicit_format(app, monkeypatch):
    mock_frames(monkeypatch, [frame(start_f=19, end_s=1, end_f=12)])
    data = {'text': 'EP4 00:00:019-00:01:012', 'time_format': 'source_frames_v1'}
    assert call(app, 'POST', '/api/clip-text', data, auth=False)[0] == 401
    assert call(app, 'POST', '/api/clip-text', data, csrf=False)[0] == 403
    status, _, body = call(app, 'POST', '/api/clip-text', data)
    assert status == 200
    assert json.loads(body)['clips'][0]['start_f'] == '019'
    assert call(app, 'POST', '/api/clip-text', {**data, 'time_format': 'unknown'})[0] == 400


@pytest.mark.integration
def test_model_paste_saves_prepares_and_confirms_actual_source_frame_boundaries(app, monkeypatch):
    from copy import deepcopy
    import uuid
    from exactcut import source_frames, workflow as w, material_versions, material_workers
    mock_frames(monkeypatch, [frame(episode=1, start_f=19, end_s=1, end_f=12)])
    monkeypatch.setenv('EXACTCUT_ALLOW_AI', '0')
    make_video(w.ROOT / 'projects/101573/source/videos/EP1.mp4')
    parsed = m.read_clip_text('EP1 00:00:019-00:01:012', time_format='source_frames_v1')
    assert parsed['errors'] == []
    item = w.create_blank(time_format='source_frames_v1')
    path = w.folder(item['id'])
    draft = deepcopy(item['draft'])
    draft.update(drama_id='101573', author='paste-test')
    draft['rows'][0]['clips'] = parsed['clips']
    saved = w.save_draft(path.name, {'revision': item['revision'], 'draft': draft})
    assert saved['draft']['rows'][0]['clips'][0]['start_f'] == '019'
    assert saved['errors']  # Save is allowed; execution still needs the original index.
    index = source_frames.prepare(path, saved['draft'], 'EP1')
    ready = w.detail(path.name)
    assert ready['errors'] == []
    material_versions.enroll(path)
    state = material_versions.read(path)
    confirmed = material_versions.confirm(path, {'revision': state['revision'], 'mutation_id': uuid.uuid4().hex})
    event = next(iter(confirmed['outbox'].values()))
    child = material_workers.materialize(path, event)
    _, cut_request = w.request_for(child, draft['rows'][0]['request_id'])
    segment = cut_request.timeline[0]
    assert (segment.start_ms, segment.end_ms) == (index['times_ms'][19], index['times_ms'][43])
    assert segment.start_ms == 633
    assert 'start_f' not in json.dumps(event['manifest'])


@pytest.mark.integration
@pytest.mark.parametrize('field', ['start_m', 'start_s', 'start_f', 'end_m', 'end_s', 'end_f'])
def test_source_frame_confirmation_requires_every_explicit_time_field(app, monkeypatch, field):
    mock_frames(monkeypatch, [frame(episode=1, start_f=19, end_s=1, end_f=12)])
    from copy import deepcopy
    from exactcut import intake, source_frames, workflow as w
    make_video(w.ROOT / 'projects/101573/source/videos/EP1.mp4')
    item = w.create_blank(time_format='source_frames_v1')
    path = w.folder(item['id'])
    draft = deepcopy(item['draft'])
    draft.update(drama_id='101573', author='paste-test')
    draft['rows'][0]['clips'] = m.read_clip_text('EP1 00:00:019-00:01:012', time_format='source_frames_v1')['clips']
    source_frames.prepare(path, draft, 'EP1')
    draft['rows'][0]['clips'][0][field] = ''
    issues = []
    manifest, errors = intake.validate_draft(draft, str(path / 'source.xlsx'), validation_issues=issues)
    assert manifest is None and errors
    assert any('请填写' in error for error in errors)
    assert any(issue['fields'] == [field] and issue['code'] == 'required' for issue in issues)
