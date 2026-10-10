import copy
import pytest
from test_material_versions import enrolled, token
from test_workflow import workspace
from exactcut import workflow as w
from exactcut.util import write_json_atomic

def test_retained_managed_name_is_allocated_around(workspace):
    m, path, state = enrolled()
    original = copy.deepcopy(state['draft'])
    state = m.confirm(path, {'revision':state['revision'], 'mutation_id':token()})
    assert not (path / 'confirmed.json').exists()
    draft = copy.deepcopy(state['draft'])
    draft['rows'][0]['seq'] = '20'
    m.save_draft(path, {'revision':state['revision'], 'mutation_id':token(), 'draft':draft})
    new_draft = copy.deepcopy(original)
    assert w.allocate_sequences(new_draft, 'other-project'), 'Retained seq 1 not reserved'
    item = w.create_blank()
    other = w.folder(item['id']); other_state = m.enroll(other)
    original['rows'][0]['request_id'] = other_state['draft']['rows'][0]['request_id']
    other_state = m.save_draft(other, {'revision': other_state['revision'], 'mutation_id': token(), 'draft': original})
    assert int(other_state['draft']['rows'][0]['seq']) > 20
    confirmed = m.confirm(other, {'revision': other_state['revision'], 'mutation_id': token()})
    assert next(iter(confirmed['assets'].values()))['candidate_version']

def test_deleted_asset_name_not_reused(workspace):
    m, path, state = enrolled(media=True)
    original = copy.deepcopy(state['draft']['rows'][0])
    old_name = next(iter(state['versions'].values()))['manifest']['requests'][0]['output_filename']
    draft = copy.deepcopy(state['draft']); draft['rows'] = []
    state = m.save_draft(path, {'revision':state['revision'], 'mutation_id':token(), 'draft':draft})
    draft = copy.deepcopy(state['draft']); draft['rows'] = [{**original,'request_id':None}]
    state = m.save_draft(path, {'revision':state['revision'], 'mutation_id':token(), 'draft':draft})
    state = m.confirm(path, {'revision':state['revision'], 'mutation_id':token()})
    new_id = state['draft']['rows'][0]['request_id']
    assert new_id != original['request_id']
    new_name = state['versions'][state['assets'][new_id]['candidate_version']]['manifest']['requests'][0]['output_filename']
    assert new_name != old_name, 'New asset reuses retained completed asset name'

def test_same_asset_keeps_retained_name_when_edited(workspace):
    m, path, state = enrolled(media=True)
    original = copy.deepcopy(state['draft']['rows'][0])
    old_name = next(iter(state['versions'].values()))['manifest']['requests'][0]['output_filename']
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['title_cn'] += ' edited'
    state = m.save_draft(path, {'revision':state['revision'], 'mutation_id':token(), 'draft':draft})
    assert state['draft']['rows'][0]['seq'] == original['seq']
    state = m.confirm(path, {'revision':state['revision'], 'mutation_id':token()})
    asset = state['assets'][original['request_id']]
    assert state['versions'][asset['candidate_version']]['manifest']['requests'][0]['output_filename'] == old_name


def test_confirm_rejects_another_assets_retained_name(workspace):
    m, path, state = enrolled(media=True)
    original = copy.deepcopy(state['draft']['rows'][0])
    draft = copy.deepcopy(state['draft'])
    draft['rows'] = [{**original, 'request_id': None, 'seq': '2'}]
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    # Reproduce a colliding draft saved by the prior release. Confirm must
    # validate retained ownership even when allocation was bypassed earlier.
    state = m.read(path)
    row = state['draft']['rows'][0]; row['seq'] = original['seq']
    state['assets'][row['request_id']]['row'] = copy.deepcopy(row)
    write_json_atomic(path / 'media-state.json', state)
    with pytest.raises(ValueError, match='命名重复'):
        m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
