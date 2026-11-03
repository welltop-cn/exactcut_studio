from unittest.mock import patch
import pytest
from test_phase1_recovery import workspace
from exactcut import workflow as w
from exactcut.orchestrator import _request_fingerprint
from exactcut.util import file_sha256

pytestmark = pytest.mark.integration


def test_repeated_source_is_hashed_once_per_batch_and_invalidated_on_change(workspace):
    item=workspace;w.start_batch(item['id'],1);path=w.folder(item['id'])
    manifest,request=w.request_for(path,item['assets'][0]['request_id'])
    project=w.ROOT/'projects'/manifest.drama_id
    source=project/'source/videos/EP1.mp4';source.write_bytes(b'original')
    cache={}
    with patch('exactcut.orchestrator.file_sha256',wraps=file_sha256) as digest:
        first=_request_fingerprint(project,request,{},cache)
        assert _request_fingerprint(project,request,{},cache)==first
        assert digest.call_count==1
        source.write_bytes(b'changed-content')
        assert _request_fingerprint(project,request,{},cache)!=first
        assert digest.call_count==2


def test_new_batch_reuses_checked_local_source_without_external_sync(workspace):
    item=workspace;w.start_batch(item['id'],1);path=w.folder(item['id'])
    project=w.ROOT/'projects'/item['draft']['drama_id']
    (project/'source/videos/EP1.mp4').write_bytes(b'local-source')
    with patch.object(w,'validate_downloaded_video'), patch('exactcut.media.ffprobe',return_value={'format':{'duration':'130'},'streams':[{'codec_type':'video'}]}), patch.object(w,'download') as download, patch('exactcut.orchestrator.execute_batch',return_value={'requests':{'R1':{'status':'qc_passed'}}}):
        assert w.produce(item['id'])==0
        download.assert_not_called()
    assert w.runtime(path)['reused_local_episodes']==['EP1']
