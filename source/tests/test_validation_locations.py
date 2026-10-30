"""Validation locations follow visible sequence numbers, independent of row order."""
from exactcut.intake import validate_draft
from test_composition import workspace
import pytest


def frame_clip(**changes):
    return {'episode': '', 'time_format': 'source_frames_v1', 'start_m': '04',
            'start_s': '00', 'start_f': '000', 'end_m': '01', 'end_s': '42', 'end_f': '016', **changes}


def test_frame_errors_identify_visible_sequence_and_each_invalid_field():
    draft = {'drama_id': '101682', 'author': 'test', 'business_date': '20261030',
             'rows': [{'request_id': f'R{i}', 'seq': i, 'timeline_text': 'EP1 00:00-00:01'}
                      for i in range(1, 6)] + [{'request_id': 'R8', 'seq': 8, 'clips': [frame_clip()]}]}
    issues = []
    manifest, errors = validate_draft(draft, validation_issues=issues)
    assert manifest is None
    assert errors == ['素材 8 · 片段 1：请填写集号', '素材 8 · 片段 1：结束时间不能早于开始时间']
    assert [(i['row_index'], i['clip_index'], i['fields']) for i in issues] == [
        (5, 0, ['episode']), (5, 0, ['start_m', 'start_s', 'start_f', 'end_m', 'end_s', 'end_f'])]


def test_generic_row_error_uses_visible_sequence():
    _, errors = validate_draft({'drama_id': '101682', 'author': 'test', 'business_date': '20261030',
                               'rows': [{'request_id': 'R8', 'seq': 8, 'timeline_text': ''}]})
    assert errors == ['素材 8：请填写至少一个有效片段']


def test_legacy_zero_length_is_invalid_but_single_frame_is_valid():
    from exactcut.intake import clip_field_issues
    frame = frame_clip()
    frame.update(episode='1', start_m='00', end_m='00', end_s='00', end_f='000')
    assert clip_field_issues({'clips': [frame]}) == []
    legacy = {k: v for k, v in frame.items() if k not in ('time_format', 'start_f', 'end_f')}
    legacy.update(start_ms='000', end_ms='000')
    assert clip_field_issues({'clips': [legacy]})[0]['code'] == 'time_order'


@pytest.mark.parametrize('managed', [False, True])
def test_detail_exposes_field_locations_for_both_project_types(workspace, managed):
    from exactcut import workflow as w, material_versions
    item = w.create_blank()
    data = item['draft']
    data.update(drama_id='101682', author='test')
    data['rows'][0].update(seq=8, clips=[frame_clip()])
    saved = w.save_draft(item['id'], {'revision': item['revision'], 'draft': data})
    if managed:
        material_versions.enroll(w.folder(item['id']))
    result = w.detail(item['id'])
    assert result['errors'] == saved['errors']
    assert result['validation_issues'][0]['row_index'] == 0
    assert result['validation_issues'][0]['text'] == '素材 8 · 片段 1：请填写集号'
    assert result['validation_issues'][0]['fields'] == ['episode']


@pytest.mark.parametrize('field,value,code', [
    ('episode', '', 'episode'), ('episode', '0', 'episode'),
    ('episode', '501', 'episode'), ('episode', '²', 'episode'),
    ('start_m', '1000', 'number'), ('start_s', '60', 'number'),
    ('start_f', '-1', 'number'), ('end_m', 'abc', 'number'),
    ('end_s', '60', 'number'), ('end_f', '1.5', 'number'),
    ('start_m', '', 'required'), ('start_s', '', 'required'),
    ('start_f', '', 'required'), ('end_m', '', 'required'),
    ('end_s', '', 'required'), ('end_f', '', 'required'),
])
def test_invalid_clip_fields_have_precise_locations_without_source_indexes(field, value, code):
    from copy import deepcopy
    clip = frame_clip(episode='4', start_m='00', end_m='01')
    clip[field] = value
    draft = {'drama_id': '101682', 'author': 'test', 'business_date': '20261030',
             'rows': [{'request_id': 'R42', 'seq': 42, 'clips': [clip]}]}
    before = deepcopy(draft)
    issues = []
    manifest, errors = validate_draft(draft, validation_issues=issues)
    assert manifest is None and errors
    assert any(issue['fields'] == [field] and issue['code'] == code for issue in issues)
    assert all(issue['row_index'] == 0 and issue['clip_index'] == 0 for issue in issues)
    assert all(error.startswith('素材 42 · 片段 1：') for error in errors)
    assert draft == before
    if code == 'required':
        assert all(issue['code'] != 'time_order' for issue in issues)


def test_multiple_materials_and_clips_collect_all_field_errors():
    clips = [frame_clip(episode='', start_m='00'), frame_clip(episode='501', start_m='00')]
    draft = {'drama_id': '101682', 'author': 'test', 'business_date': '20261030',
             'rows': [{'request_id': f'R{seq}', 'seq': seq, 'clips': clips} for seq in [8, 42]]}
    issues = []
    _, errors = validate_draft(draft, validation_issues=issues)
    assert len(errors) == 4
    assert [(i['row_index'], i['clip_index']) for i in issues] == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_missing_frame_does_not_also_report_an_unproven_time_order_error():
    from exactcut.intake import clip_field_issues
    clip = frame_clip(episode='1', start_m='00', start_f='019', end_m='00', end_s='00', end_f='')
    issues = clip_field_issues({'clips': [clip]})
    assert [(i['code'], i['fields']) for i in issues] == [('required', ['end_f'])]


def test_managed_clone_recomputes_locations_without_persisting_validation_projection(workspace):
    import json
    from exactcut import material_versions, workflow as w
    item = w.create_blank()
    draft = item['draft']
    draft.update(drama_id='101682', author='clone-test')
    draft['rows'][0].update(seq=8, clips=[frame_clip()])
    w.save_draft(item['id'], {'revision': item['revision'], 'draft': draft})
    material_versions.enroll(w.folder(item['id']))
    cloned = w.clone_batch(item['id'])
    stored = json.loads((w.folder(cloned['id']) / 'draft.json').read_text())
    assert 'validation_issues' not in stored
    assert cloned['validation_issues'][0]['row_index'] == 0
    assert cloned['validation_issues'][0]['text'] == f"素材 {cloned['draft']['rows'][0]['seq']} · 片段 1：请填写集号"
    assert cloned['draft']['rows'][0]['request_id'] != draft['rows'][0]['request_id']
