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


def test_explicit_lines_parse_locally_preserving_frame_ordinals(monkeypatch):
    monkeypatch.setattr(m, 'interpret', lambda *a, **k: pytest.fail('explicit frame grammar needs no AI'))
    parsed = m.read_clip_text('第4集 00:00:000-00:50:019\n\nEP5 00:00:000 至 01:28:012', time_format='source_frames_v1')
    assert not parsed['errors']
    assert parsed['clips'] == [
        {'episode': '4', 'time_format': 'source_frames_v1', 'start_m': '00', 'start_s': '00', 'start_f': '000', 'end_m': '00', 'end_s': '50', 'end_f': '019'},
        {'episode': '5', 'time_format': 'source_frames_v1', 'start_m': '00', 'start_s': '00', 'start_f': '000', 'end_m': '01', 'end_s': '28', 'end_f': '012'}]
    same = m.read_clip_text('EP4 01:20 至 01:20', time_format='source_frames_v1')
    assert not same['errors'] and same['clips'][0]['end_f'] == '000'


@pytest.mark.parametrize('text', ['EP4 00:60:000-01:00:000', 'EP4 00:01:1000-00:02:000',
    'EP4 1000:01:000-1001:00:000', 'EP4 00:01:020-00:01:019', 'EP501 00:01-00:02',
    'EP4 00:01.019-00:02.012', 'EP4 00:01:019-00:02:012 毫秒',
    'EP4 00:00:01:19-00:00:02:12', 'SMPTE EP4 00:01:019-00:02:012'])
def test_ambiguous_or_invalid_explicit_input_never_partially_fills(monkeypatch, text):
    monkeypatch.setattr(m, 'interpret', lambda *a, **k: pytest.fail('must reject explicit invalid/ambiguous grammar locally'))
    parsed = m.read_clip_text('EP1 00:00-00:01\n' + text, time_format='source_frames_v1')
    assert parsed['errors'] and parsed['clips'] == []


def test_frame_clip_limit_and_unknown_modes(monkeypatch):
    monkeypatch.setattr(m, 'interpret', lambda *a, **k: pytest.fail('must not call AI'))
    assert len(m.read_clip_text('\n'.join(['EP4 00:00-00:01'] * 30), time_format='source_frames_v1')['clips']) == 30
    parsed = m.read_clip_text('\n'.join(['EP4 00:00-00:01'] * 31), time_format='source_frames_v1')
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
    monkeypatch.setattr(m.ai_rate, 'acquire', lambda: None)
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
    monkeypatch.setattr(m, 'interpret', lambda *a, **k: pytest.fail('explicit frames need no AI'))
    data = {'text': 'EP4 00:00:019-00:01:012', 'time_format': 'source_frames_v1'}
    assert call(app, 'POST', '/api/clip-text', data, auth=False)[0] == 401
    assert call(app, 'POST', '/api/clip-text', data, csrf=False)[0] == 403
    status, _, body = call(app, 'POST', '/api/clip-text', data)
    assert status == 200
    assert json.loads(body)['clips'][0]['start_f'] == '019'
    assert call(app, 'POST', '/api/clip-text', {**data, 'time_format': 'unknown'})[0] == 400


def test_local_paste_saves_prepares_and_confirms_actual_source_frame_boundaries(app, monkeypatch):
    from copy import deepcopy
    import uuid
    from exactcut import source_frames, workflow as w, material_versions, material_workers
    monkeypatch.setattr(m, 'interpret', lambda *a, **k: pytest.fail('standard paste must stay local'))
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


@pytest.mark.parametrize('field', ['start_m', 'start_s', 'start_f', 'end_m', 'end_s', 'end_f'])
def test_source_frame_confirmation_requires_every_explicit_time_field(app, field):
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
    manifest, errors = intake.validate_draft(draft, str(path / 'source.xlsx'))
    assert manifest is None and errors
    assert any('必填' in error for error in errors)
