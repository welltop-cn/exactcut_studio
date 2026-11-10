"""Preview receipts do not change the desired media content."""
import copy
import pytest

from exactcut import material_versions as m, hook_presentation
from exactcut.util import json_sha256, write_json_atomic
from test_phase2_hook_workflow import request
from test_material_versions import enrolled, token, workspace


def test_preview_receipt_save_preserves_generation_but_real_hook_and_sequence_edits_invalidate(workspace):
    _, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    generation = state['assets'][rid]['desired_generation']
    current = state['assets'][rid]['current_version']
    for options in ({'preview_key': 'a' * 64}, {}, {'preview_key': 'b' * 64}):
        draft = copy.deepcopy(state['draft'])
        draft['rows'][0]['hook_options'] = options
        state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
        assert state['assets'][rid]['desired_generation'] == generation
        assert m.detail(path)['assets'][0]['version_id'] == current
    for field, value in [('hook_text', 'A real edit'), ('seq', 22)]:
        draft = copy.deepcopy(state['draft'])
        draft['rows'][0][field] = value
        state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
        generation += 1
        assert state['assets'][rid]['desired_generation'] == generation


@pytest.mark.parametrize('broken', [None, [], 7, 'cache', 'decision-list'])
def test_old_preview_pointer_to_malformed_cache_falls_back(tmp_path, broken):
    frozen = request(mode='none')
    timeline = [item.model_dump(mode='json') for item in frozen.timeline]
    doc = ({'sources_complete': True, 'identity': hook_presentation.preview_identity(frozen, timeline, []),
            'decision': []} if broken == 'decision-list' else broken)
    key = json_sha256(doc)
    write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json', doc)
    frozen.hook_options.preview_key = key
    assert hook_presentation.approved_preview(tmp_path, frozen, timeline, []) is None
