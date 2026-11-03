from test_phase1_recovery import workspace
import pytest
from test_preview_oss import Bucket
from exactcut import workflow as w, preview_oss, media_backfill, result_store
from exactcut.util import file_sha256, write_json_atomic

pytestmark = pytest.mark.integration


def test_backfill_dry_run_uploads_both_once_and_preserves_existing_records(workspace,monkeypatch):
    item=workspace;w.start_batch(item['id'],1);path=w.folder(item['id'])
    _,request=w.request_for(path,item['assets'][0]['request_id'])
    master=path/'production/videos'/request.output_filename;master.parent.mkdir(parents=True);master.write_bytes(b'master')
    preview=path/'production/previews'/request.output_filename;preview.parent.mkdir(parents=True);preview.write_bytes(b'preview')
    record={'status':'qc_passed','output_sha256':file_sha256(master),'custom':'preserved',
            'preview':{'master_sha256':file_sha256(master),'sha256':file_sha256(preview)}}
    write_json_atomic(path/'production/results.json',{'requests':{request.request_id:record}})
    bucket=Bucket();monkeypatch.setattr(preview_oss,'enabled',lambda:True);monkeypatch.setattr(preview_oss,'_connect',lambda:bucket)
    report=media_backfill.backfill(apply=False)
    assert len(report)==2 and len(bucket.puts)==0
    report=media_backfill.backfill(apply=True)
    assert [r['status'] for r in report]==['ready','ready'] and len(bucket.puts)==2
    media_backfill.backfill(apply=True)
    assert len(bucket.puts)==2
    saved=result_store.read(path/'production')['requests'][request.request_id]
    assert saved['custom']=='preserved' and saved['status']=='qc_passed'
    assert saved['original_oss']['sha256']==file_sha256(master)
    assert saved['preview']['oss']['sha256']==file_sha256(preview)
    assert master.read_bytes()==b'master' and preview.read_bytes()==b'preview'
