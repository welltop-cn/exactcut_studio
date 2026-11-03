"""Existing unmarked millisecond projects stay independent of source-frame indexing."""
from copy import deepcopy

import pytest

from exactcut import intake, material_versions as versions, source_frames, workflow as w
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace, complete_asset
from test_material_versions import candidate_job, token


@pytest.fixture(autouse=True)
def forbid_frame_resolution(monkeypatch, workspace):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(workspace / 'sources'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    def forbidden(*args, **kwargs):
        pytest.fail('legacy millisecond project must not need a source-frame index')
    for name in ('prepare', '_load', 'resolve_frame_clips'):
        monkeypatch.setattr(source_frames, name, forbidden)


def legacy_project():
    item = w.create_blank()
    draft = deepcopy(item['draft'])
    draft.update(drama_id='101573', author='legacy')
    draft['rows'][0]['clips'] = [{**intake.blank_clip(), 'episode': '1',
        'start_ms': '019', 'end_s': '01', 'end_ms': '012'}]
    return w.save_draft(item['id'], {'revision': item['revision'], 'draft': draft})


def assert_legacy(draft, source=''):
    assert 'editor_time_format' not in draft
    for row in draft['rows']:
        for clip in row['clips']:
            assert 'time_format' not in clip and 'start_f' not in clip and 'end_f' not in clip
            assert clip['start_ms'] == '019' and clip['end_ms'] == '012'
    manifest, errors = intake.validate_draft(draft, source)
    assert not errors, errors
    for request in manifest.requests:
        for segment in request.timeline:
            assert (segment.start_ms, segment.end_ms) == (19, 1012)
    return manifest


@pytest.mark.parametrize('managed', [False, True])
def test_legacy_save_add_and_clone_preserve_nonzero_milliseconds_without_index(managed):
    item = legacy_project()
    path = w.folder(item['id'])
    if managed:
        versions.enroll(path)
        item = versions.detail(path)
    draft = deepcopy(item['draft'])
    draft['rows'][0]['clips'].append(deepcopy(draft['rows'][0]['clips'][0]))
    extra = deepcopy(draft['rows'][0])
    extra.update(request_id=None if managed else 'legacy-extra', seq=2)
    draft['rows'].append(extra)
    data = {'revision': item['revision'], 'mutation_id': token(), 'draft': draft}
    if managed:
        versions.save_draft(path, data)
        saved = versions.detail(path)
    else:
        saved = w.save_draft(path.name, data)
    assert_legacy(saved['draft'], str(path / 'source.xlsx'))
    assert not list(path.glob('source-picker/*'))
    copied = w.clone_batch(path.name)
    assert copied['id'] != path.name
    assert_legacy(copied['draft'])
    assert len(copied['draft']['rows']) == 2
    assert all(len(row['clips']) == 2 for row in copied['draft']['rows'])


def test_failed_legacy_retry_uses_frozen_ms_manifest_without_rewriting_it():
    item = legacy_project()
    path = w.folder(item['id'])
    w.start_batch(path.name, item['revision'])
    original = (path / 'confirmed.json').read_bytes()
    w.update(path, 'failed', 'isolated failed render fixture')
    # A different but valid editable snapshot cannot replace the confirmed cut on retry.
    document = read_json(path / 'draft.json')
    document['draft']['rows'][0]['clips'][0]['start_ms'] = '777'
    intake.compile_clips(document['draft']['rows'][0])
    write_json_atomic(path / 'draft.json', document)
    w.start_batch(path.name, item['revision'])
    assert (path / 'confirmed.json').read_bytes() == original
    _, request = w.request_for(path, item['draft']['rows'][0]['request_id'])
    assert (request.timeline[0].start_ms, request.timeline[0].end_ms) == (19, 1012)


def test_failed_managed_legacy_candidate_retries_original_ms_in_fresh_child():
    item = legacy_project()
    path = w.folder(item['id'])
    versions.enroll(path)
    state = versions.read(path)
    state = versions.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    worker, child, event, job = candidate_job(path, state)
    original = (child / 'confirmed.json').read_bytes()
    worker.stage_enter(job)
    worker.stage_finish(job, 2)
    state = versions.read(path)
    assert state['versions'][event['version_id']]['status'] == 'failed'
    retried = versions.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    _, retry_child, retry_event, _ = candidate_job(path, retried)
    assert retry_child != child
    assert retry_event['manifest'] == event['manifest']
    assert (child / 'confirmed.json').read_bytes() == original
    _, request = w.request_for(retry_child, event['asset_id'])
    assert (request.timeline[0].start_ms, request.timeline[0].end_ms) == (19, 1012)
    assert not (retry_child / 'source-picker').exists()


@pytest.mark.integration
def test_completed_legacy_asset_reuses_existing_version_and_media_when_adding_new_work():
    # Existing lifecycle helper supplies integrity-checked bytes, not a render fixture.
    item = legacy_project()
    path, request, video, record = complete_asset(item)
    frozen_confirmation = (path / 'confirmed.json').read_bytes()
    media_bytes = video.read_bytes()
    result_bytes = (path / 'production/results.json').read_bytes()
    versions.enroll(path)
    state = versions.read(path)
    rid = request['request_id']
    vid = state['assets'][rid]['current_version']
    old_version = deepcopy(state['versions'][vid])
    no_op = versions.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    assert no_op['mutation']['execution_version_ids'] == []
    assert no_op['outbox'] == state['outbox']
    draft = deepcopy(no_op['draft'])
    extra = deepcopy(draft['rows'][0]); extra.update(request_id=None, seq=2)
    draft['rows'].append(extra)
    saved = versions.save_draft(path, {'revision': no_op['revision'], 'mutation_id': token(), 'draft': draft})
    confirmed = versions.confirm(path, {'revision': saved['revision'], 'mutation_id': token()})
    assert confirmed['assets'][rid]['current_version'] == vid
    assert confirmed['assets'][rid]['candidate_version'] is None
    assert confirmed['versions'][vid] == old_version
    assert len(confirmed['mutation']['execution_version_ids']) == 1
    new_id = confirmed['mutation']['execution_version_ids'][0]
    assert confirmed['versions'][new_id]['asset_id'] != rid
    master, current_record = versions.current_file(path, rid)
    assert master.read_bytes() == media_bytes
    assert current_record['output_sha256'] == record['output_sha256']
    assert (path / 'confirmed.json').read_bytes() == frozen_confirmation
    assert (path / 'production/results.json').read_bytes() == result_bytes
    assert_legacy(confirmed['draft'])
