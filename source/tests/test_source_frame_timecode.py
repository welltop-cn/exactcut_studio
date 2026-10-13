"""Explicit source-frame draft notation must never be mistaken for milliseconds."""
from copy import deepcopy
from pathlib import Path

from exactcut import intake, source_prefetch, workflow as w
from test_source_picker import source, make_video
from test_web_base_path import app


def frame_clip(**changes):
    return {'episode': '1', 'time_format': 'source_frames_v1',
            'start_m': '00', 'start_s': '00', 'start_f': '019',
            'end_m': '00', 'end_s': '01', 'end_f': '012', **changes}


def draft(clip):
    return {'drama_id': '101573', 'business_date': '20261013', 'author': '测试',
            'rows': [{'request_id': 'R1', 'seq': 1, 'clips': [clip],
                      'timeline_text': 'EP1 00:00.019-00:01.012'}]}


def test_compile_preserves_explicit_frame_fields_without_fabricating_milliseconds():
    row = draft(frame_clip())['rows'][0]
    intake.compile_clips(row)
    assert row['clips'][0]['time_format'] == 'source_frames_v1'
    assert row['clips'][0]['start_f'] == '019'
    assert 'start_ms' not in row['clips'][0]
    assert row['timeline_text'] == ''


def test_pending_frames_still_prefetch_source_and_block_untrusted_timeline():
    data = draft(frame_clip())
    pending = draft(frame_clip(start_f='019', end_s='00', end_f='020'))
    pending['rows'][0]['timeline_text'] = ''
    assert source_prefetch.snapshot(pending)[1] == ['EP1']
    manifest, errors = intake.validate_draft(data)
    assert manifest is None
    assert any('帧' in error for error in errors)


def test_legacy_milliseconds_keep_original_meaning():
    data = draft({**intake.blank_clip(), 'episode': '1', 'start_ms': '019', 'end_s': '01', 'end_ms': '012'})
    intake.compile_clips(data['rows'][0])
    manifest, errors = intake.validate_draft(data)
    assert not errors
    assert manifest.requests[0].timeline[0].start_ms == 19
    assert manifest.requests[0].timeline[0].end_ms == 1012


def test_actual_source_frame_mapping_and_source_replacement(source):
    from exactcut import source_picker
    _, item, _, video = source
    path = w.folder(item['id'])
    data = draft(frame_clip())
    meta = source_picker.prepare(path, data, 'EP1')
    manifest, errors = intake.validate_draft(data, str(path / 'source.xlsx'))
    assert not errors, errors
    segment = manifest.requests[0].timeline[0]
    second = [i for i, time in enumerate(meta['times_ms']) if time // 1000 == 1]
    assert segment.start_ms == meta['times_ms'][19]
    assert segment.end_ms == meta['times_ms'][second[12] + 1]
    make_video(video, vfr=True)
    assert intake.validate_draft(data, str(path / 'source.xlsx'))[0] is None


def test_vfr_invalid_frame_and_unknown_format_block_execution(source):
    from exactcut import source_picker
    _, item, _, video = source
    make_video(video, vfr=True)
    path = w.folder(item['id'])
    data = draft(frame_clip(start_f='003', end_f='006'))
    meta = source_picker.prepare(path, data, 'EP1')
    manifest, errors = intake.validate_draft(data, str(path / 'source.xlsx'))
    assert not errors, errors
    assert manifest.requests[0].timeline[0].start_ms == meta['times_ms'][3]
    data['rows'][0]['clips'][0]['end_f'] = '999'
    assert intake.validate_draft(data, str(path / 'source.xlsx'))[0] is None
    data['rows'][0]['clips'][0]['time_format'] = 'unknown'
    intake.compile_clips(data['rows'][0])
    assert intake.validate_draft(data, str(path / 'source.xlsx'))[0] is None


def test_save_without_source_preserves_frames_legacy_and_managed(source):
    from exactcut import material_versions as versions
    _, item, _, video = source
    video.unlink()
    data = deepcopy(item['draft'])
    data.update(drama_id='101573', author='测试')
    data['rows'][0]['clips'] = [frame_clip()]
    result = w.save_draft(item['id'], {'revision': item['revision'], 'draft': data})
    assert result['draft']['rows'][0]['clips'][0]['start_f'] == '019'
    assert result['draft']['rows'][0]['timeline_text'] == ''
    assert result['errors']
    assert not (w.folder(item['id']) / 'confirmed.json').exists()
    versions.enroll(w.folder(item['id']))
    managed = versions.detail(w.folder(item['id']))
    # Managed save uses the documented revision CAS like legacy drafts.
    versions.save_draft(w.folder(item['id']), {'revision': managed['revision'], 'mutation_id': 'frame-save-test-0001', 'draft': managed['draft']})
    saved = versions.detail(w.folder(item['id']))
    assert saved['draft']['rows'][0]['clips'][0]['start_f'] == '019'
    assert saved['errors']


def test_last_frame_maps_to_source_duration_and_does_not_modify_draft(source):
    from exactcut import source_picker
    _, item, _, _ = source
    path = w.folder(item['id'])
    data = draft(frame_clip(start_s='03', start_f='029', end_s='03', end_f='029'))
    before = deepcopy(data)
    meta = source_picker.prepare(path, data, 'EP1')
    manifest, errors = intake.validate_draft(data, str(path / 'source.xlsx'))
    assert not errors, errors
    assert manifest.requests[0].timeline[0].end_ms == meta['duration_ms']
    assert data == before


def test_explicit_frame_blank_creation_and_save_preserve_editor_format(app):
    from test_web_base_path import login, request
    import json
    headers = login(app)
    status, _, body = request(app, 'POST', '/home/api/batches/blank',
                              {'time_format': 'source_frames_v1'}, headers)
    assert status == 201
    created = json.loads(body)
    assert created['draft']['editor_time_format'] == 'source_frames_v1'
    clip = created['draft']['rows'][0]['clips'][0]
    assert clip['time_format'] == 'source_frames_v1'
    assert clip['start_f'] == '000' and clip['end_f'] == '000'
    assert 'start_ms' not in clip and 'end_ms' not in clip
    status, _, body = request(app, 'POST', f"/home/api/batches/{created['id']}/save",
        {'revision': created['revision'], 'draft': created['draft']}, headers)
    assert status == 200
    saved = json.loads(body)
    assert saved['draft']['editor_time_format'] == 'source_frames_v1'
    assert saved['draft']['rows'][0]['clips'][0]['start_f'] == '000'
    legacy = w.create_blank()
    assert 'editor_time_format' not in legacy['draft']
    assert legacy['draft']['rows'][0]['clips'][0]['start_ms'] == '000'


def test_blank_api_rejects_invalid_format_without_creating_project(app):
    from test_web_base_path import login, request
    headers = login(app)
    before = set(w.BATCHES.glob('*'))
    for invalid in ['frames', 'milliseconds', None, 0, [], {}]:
        status, _, _ = request(app, 'POST', '/home/api/batches/blank', {'time_format': invalid}, headers)
        assert status == 400, invalid
    assert set(w.BATCHES.glob('*')) == before


def test_frame_bucket_lookup_does_not_scan_entire_source_index(source, monkeypatch):
    from exactcut import source_picker
    _, item, _, _ = source
    path = w.folder(item['id'])
    data = draft(frame_clip(start_s='01', start_f='002', end_s='02', end_f='003'))
    meta = source_picker.prepare(path, data, 'EP1')
    original_load = source_picker._load
    class IndexedOnly(list):
        def __iter__(self):
            raise AssertionError('frame lookup must not scan entire source timestamps')
    def indexed_load(*args):
        doc, video, directory = original_load(*args)
        return {**doc, 'times_ms': IndexedOnly(doc['times_ms'])}, video, directory
    monkeypatch.setattr(source_picker, '_load', indexed_load)
    manifest, errors = intake.validate_draft(data, str(path / 'source.xlsx'))
    assert not errors, errors
    assert manifest.requests[0].timeline[0].start_ms == meta['times_ms'][32]
    assert manifest.requests[0].timeline[0].end_ms == meta['times_ms'][64]


def test_legacy_valid_timeline_with_null_clips_still_validates():
    data = draft(frame_clip())
    data['rows'][0]['clips'] = None
    manifest, errors = intake.validate_draft(data)
    assert not errors, errors
    assert manifest.requests[0].timeline[0].start_ms == 19
    assert manifest.requests[0].timeline[0].end_ms == 1012
