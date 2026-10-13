"""HTTP Hook preview must resolve explicit frame drafts using their batch index."""
from copy import deepcopy
import json

import pytest

from exactcut import hook_preview, material_versions, source_frames, webserver as web, workflow as w
from test_source_frames import source
from test_source_frame_timecode import frame_clip
from test_web_base_path import app, request


@pytest.mark.parametrize('managed', [False, True])
def test_frame_draft_hook_preview_uses_ready_source_index(source, monkeypatch, managed):
    server, _, headers, _ = source
    monkeypatch.setattr(web, 'JOBS', w.ROOT / '.web-state/jobs')
    web.JOBS.mkdir(parents=True, exist_ok=True)
    created = w.create_blank(time_format='source_frames_v1')
    path = w.folder(created['id'])
    draft = deepcopy(created['draft'])
    draft.update(drama_id='101573', author='frame-preview')
    row = draft['rows'][0]
    row.update(clips=[frame_clip()], hook_text='LOOK!',
               hook_options={'mode': 'none', 'style': 'default', 'position': 'top'})
    index = source_frames.prepare(path, draft, 'EP1')
    saved = w.save_draft(path.name, {'revision': created['revision'], 'draft': draft})
    assert not saved['errors']
    if managed:
        material_versions.enroll(path)
        saved = material_versions.detail(path)
    computed = []
    def compute(project, cut_request, work):
        computed.append((project, cut_request))
        cache = work / 'cache'
        cache.mkdir()
        return {'preview_key': 'a' * 64}, {'decision': {}}, cache
    monkeypatch.setattr(hook_preview, 'compute', compute)
    status, _, body = request(server, 'POST', f'/home/api/batches/{path.name}/hook-preview',
                              {'revision': saved['revision'], 'index': 0}, headers)
    assert status == 200, body
    assert json.loads(body)['preview_key'] == 'a' * 64
    assert len(computed) == 1
    project, cut_request = computed[0]
    assert project == w.ROOT / 'projects/101573'
    assert cut_request.request_id == row['request_id']
    assert cut_request.hook_text == 'LOOK!'
    segment = cut_request.timeline[0]
    assert (segment.start_ms, segment.end_ms) == (index['times_ms'][19], index['times_ms'][43])
    assert segment.start_ms == 633  # Explicitly not legacy 19 milliseconds.
    assert not web.INLINE_RENDERS
