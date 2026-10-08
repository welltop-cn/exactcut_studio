import shutil
from test_phase1_recovery import workspace
from exactcut import workflow as w
from exactcut.preview import make_preview
from exactcut.util import write_json_atomic, file_sha256
from exactcut.media import run_checked, ffprobe
import pytest


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')
def test_lightweight_preview_preserves_master_and_publishing_path(workspace):
    item=workspace;w.start_batch(item['id'],1);path=w.folder(item['id'])
    _,request=w.request_for(path,item['assets'][0]['request_id'])
    master=path/'production/videos'/request.output_filename;master.parent.mkdir(parents=True)
    run_checked(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=s=1080x1920:r=30',
                 '-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','2','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(master)])
    digest=file_sha256(master)
    write_json_atomic(path/'production/results.json', {'requests':{request.request_id:{'status':'qc_passed','output_sha256':digest}}})
    w.update(path,'review','ready')
    assert w.make_batch_previews(item['id'])==0
    asset=w.detail(item['id'])['assets'][0]
    assert asset['preview_kind']=='lightweight' and '/preview/' in asset['preview']
    derivative,_=w.preview_file(item['id'],request.request_id,lightweight=True)
    original,_=w.preview_file(item['id'],request.request_id)
    assert original==master and derivative!=master
    assert file_sha256(master)==digest
    assert derivative.stat().st_size < master.stat().st_size / 2
    assert next(s for s in ffprobe(derivative)['streams'] if s['codec_type']=='video')['width']==540
    w.queue_publish(item['id'],request.request_id,digest)
    assert w.preview_file(item['id'],request.request_id)[0]==master


def test_invalid_derivative_never_replaces_original(tmp_path):
    master=tmp_path/'original.mp4';master.write_bytes(b'original')
    with pytest.raises(ValueError,match='版本'):
        make_preview(master,tmp_path/'preview.mp4','wrong')
    assert master.read_bytes()==b'original' and not (tmp_path/'preview.mp4').exists()
