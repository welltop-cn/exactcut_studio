"""Confirmed legacy retries use their frozen millisecond manifest, not editor caches."""
from copy import deepcopy
import pytest
from exactcut import intake, workflow as w
from exactcut.util import write_json_atomic
from test_workflow import workspace


def pending_frame_project():
    item=w.create_blank(time_format='source_frames_v1')
    draft=deepcopy(item['draft']);draft.update(drama_id='101573',author='retry-test')
    draft['rows'][0]['clips']=[{'episode':'1','time_format':'source_frames_v1',
        'start_m':'00','start_s':'00','start_f':'019','end_m':'00','end_s':'01','end_f':'012'}]
    return w.save_draft(item['id'],{'revision':item['revision'],'draft':draft})


def test_confirmed_frame_retry_uses_frozen_manifest_without_source_index(workspace):
    item=pending_frame_project();path=w.folder(item['id'])
    resolved=deepcopy(item['draft'])
    resolved['rows'][0]['clips']=[{**intake.blank_clip(),'episode':'1','start_ms':'633','end_s':'01','end_ms':'434'}]
    intake.compile_clips(resolved['rows'][0])
    manifest,errors=intake.validate_draft(resolved,str(path/'source.xlsx'))
    assert not errors
    write_json_atomic(path/'confirmed.json',manifest.model_dump(mode='json'))
    frozen=(path/'confirmed.json').read_bytes();draft=(path/'draft.json').read_bytes()
    w.update(path,'failed','isolated retry fixture')
    command=w.start_batch(path.name,item['revision'])
    assert 'produce' in command
    assert (path/'confirmed.json').read_bytes()==frozen
    assert (path/'draft.json').read_bytes()==draft
    assert not (path/'source-picker').exists()


def test_unconfirmed_frame_draft_still_requires_source_index(workspace):
    item=pending_frame_project();path=w.folder(item['id'])
    with pytest.raises(ValueError,match='正片|帧索引'):
        w.start_batch(path.name,item['revision'])
    assert not (path/'confirmed.json').exists()
