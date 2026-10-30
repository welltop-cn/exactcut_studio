"""Validation locations follow visible sequence numbers, independent of row order."""
from exactcut.intake import validate_draft
from test_composition import workspace
import pytest


def frame_clip(**changes):
    return dict(episode='', time_format='source_frames_v1', start_m='04',
                start_s='00', start_f='000', end_m='01', end_s='42', end_f='016', **changes)


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
