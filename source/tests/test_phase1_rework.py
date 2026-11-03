import json
import pytest
from test_phase1_recovery import workspace
from exactcut import workflow as w
from exactcut.util import write_json_atomic

pytestmark = pytest.mark.integration


def test_repair_creates_editable_linked_version_and_preserves_original(workspace):
    old=workspace;path=w.folder(old['id']);w.start_batch(old['id'],old['revision'])
    w.update(path,'failed','Hook too long')
    write_json_atomic(path/'publications.json', {'R001': {'status':'fbbm_ready','publication':{'feishu_record_id':'original-record'}}})
    production=path/'production/videos/original.mp4';production.parent.mkdir(parents=True);production.write_bytes(b'original')
    files={str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}
    new=w.clone_batch(old['id'], purpose='repair')
    assert new['source_batch_id']==old['id'] and new['source_revision']==old['revision']
    assert new['clone_purpose']=='repair' and new['runtime']['status']=='draft'
    assert new['assets'][0]['filename']!=old['assets'][0]['filename']
    new['draft']['rows'][0]['hook_text']='A repaired Hook.'
    saved=w.save_draft(new['id'], {'revision':new['revision'],'draft':new['draft']})
    w.start_batch(new['id'], saved['revision'])
    assert files=={str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}
    assert not (w.folder(new['id'])/'publications.json').exists()
    assert w.runtime(path)['status']=='failed'
