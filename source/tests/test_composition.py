import json

import pytest
from exactcut import workflow as w
from exactcut.intake import validate_draft


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    return tmp_path


def test_web_form_compiles_minute_second_millisecond(workspace, monkeypatch):
    from exactcut import compiler
    monkeypatch.setattr(compiler, 'production_date', lambda: '20261011')
    item = w.create_blank('马涛')
    path = w.folder(item['id'])
    stored = json.loads((path / 'draft.json').read_text())
    draft = stored['draft']
    assert draft['author'] == ''
    assert draft['business_date'] == '20261011'
    assert draft['material_type'] == 'typezxraw'
    row = draft['rows'][0]
    assert 'composition' not in row
    assert row['hook_options'] == {'mode': 'ai', 'style': 'auto', 'position': 'auto', 'duration_ms': None, 'manual_ranges': []}
    assert row['clips'] == [{'episode': '', 'start_m': '00', 'start_s': '00', 'start_ms': '000', 'end_m': '00', 'end_s': '00', 'end_ms': '000'}]
    row['hook_text'] = '今晚别回头'
    row['clips'] = [
        {'episode': '12', 'start_m': '1', 'start_s': '02', 'start_ms': '008', 'end_m': '1', 'end_s': '18', 'end_ms': '0'},
        {'episode': '', 'start_m': '00', 'start_s': '00', 'start_ms': '000', 'end_m': '00', 'end_s': '00', 'end_ms': '000'},
    ]
    draft.update(drama_id='101500', drama_title='The Forbidden Ride', author='马涛', business_date='2026-10-08')
    saved = w.save_draft(item['id'], {'revision': stored['revision'], 'draft': draft})
    manifest, errors = validate_draft(saved['draft'], '')
    assert errors == []
    request = manifest.requests[0]
    assert request.hook_text == '今晚别回头'
    assert request.hook_options.mode == 'ai'
    assert request.hook_options.style == 'auto'
    assert request.hook_options.position == 'auto'
    assert request.hook_options.duration_ms is None
    assert request.timeline[0].episode == 'EP12'
    assert request.timeline[0].start_ms == 62008
    assert request.timeline[0].end_ms == 78000
    assert saved['draft']['rows'][0]['timeline_text'] == 'EP12 01:02.008-01:18.000'
    assert saved['draft']['business_date'] == '2026-10-08'
    saved['draft']['rows'][0]['clips'] = [
        {'episode': 'EP1', 'start_m': '', 'start_s': '5', 'start_ms': '', 'end_m': '', 'end_s': '20', 'end_ms': ''},
    ]
    saved = w.save_draft(item['id'], {'revision': saved['revision'], 'draft': saved['draft']})
    assert saved['draft']['rows'][0]['timeline_text'] == 'EP1 00:05.000-00:20.000'
    manifest, errors = validate_draft(saved['draft'], '')
    assert errors == []
    assert manifest.requests[0].timeline[0].start_ms == 5000
    assert manifest.requests[0].timeline[0].end_ms == 20000
    bad = json.loads((path / 'draft.json').read_text())
    bad['draft']['rows'][0]['clips'][0]['start_s'] = '60'
    saved = w.save_draft(item['id'], {'revision': bad['revision'], 'draft': bad['draft']})
    assert any('秒为 0～59' in error for error in saved['errors'])
    assert saved['draft']['rows'][0]['timeline_text'] == ''
