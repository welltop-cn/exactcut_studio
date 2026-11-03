import io
import pytest
import json
import subprocess
from PIL import Image
from exactcut import hook_presentation as display, hook_preview
from exactcut.hook_options import HookOptions
from exactcut.models import CutRequest
from exactcut.media import resolve_full_episodes
from exactcut.util import json_sha256,write_json_atomic
from test_phase2_timing import manifest


def source(tmp_path):
    p=tmp_path/'source/videos/EP1.mp4';p.parent.mkdir(parents=True)
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=blue:s=90x160:r=25:d=2',
                    '-f','lavfi','-i','color=red:s=90x160:r=25:d=4',
                    '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]',
                    '-c:v','libx264','-preset','ultrafast',str(p)],check=True)
    return p


@pytest.mark.integration
def test_samples_late_hook_frames_and_vision_avoids_late_face(tmp_path,monkeypatch):
    source(tmp_path);work=tmp_path/'work';work.mkdir()
    timeline=[{'type':'episode','episode':'EP1','start_ms':0,'end_ms':6000}]
    frames,coverage=display.sample_timeline_frames(tmp_path,timeline,work,5500,width=90,height=160)
    assert coverage['timestamps_ms'][-1]==5450 and len(frames)>=6
    def regions(frames,model):
        late=any(Image.open(io.BytesIO(f)).getpixel((180,320))[0]>150 for f in frames)
        result=[{'kind':'face','x':.1,'y':.07,'width':.8,'height':.18}]
        if late:result.append({'kind':'face','x':.1,'y':.36,'width':.8,'height':.18})
        return {'regions':result,'reason':'测试包含后半段人物'}
    monkeypatch.setattr(display,'environment',lambda:'production')
    monkeypatch.setattr(display,'ai_model',lambda:'mock-vision')
    monkeypatch.setattr(display,'request_regions',regions)
    decision=display.placement(frames,HookOptions(),{'source':'ai_semantic'},tmp_path/'placement')
    assert decision['position']=='bottom' and decision['frames_checked']==len(frames)
    assert not decision['warning']


def test_schedule_covers_card_edit_source_offsets_and_indexed_shot_changes(tmp_path):
    index=tmp_path/'data/episodes/EP1.video.json';index.parent.mkdir(parents=True)
    write_json_atomic(index,{'shots':[{'start_ms':2000,'end_ms':3700},{'start_ms':3700,'end_ms':6000}]})
    timeline=[{'type':'title_card','text':'Before','duration_ms':1500},
              {'type':'episode','episode':'EP1','start_ms':2000,'end_ms':6000}]
    schedule,capped=display.frame_schedule(tmp_path,timeline,5000)
    assert not capped and any(point==1550 and source==2050 for point,item,source in schedule)
    assert any(point==3250 and source==3750 for point,item,source in schedule)
    assert all(point<5000 for point,item,source in schedule)
    write_json_atomic(index,{'shots':[{'start_ms':n,'end_ms':n+100} for n in range(2000,6000,100)]})
    schedule,capped=display.frame_schedule(tmp_path,timeline,5000)
    assert capped and len(schedule)==display.MAX_FRAMES
    assert schedule[-1][0]==4950


@pytest.mark.integration
def test_full_episode_preview_has_resolved_identity_and_index_change_invalidates_approval(tmp_path):
    source(tmp_path);work=tmp_path/'work';work.mkdir()
    r=CutRequest(request_id='R1',seq=1,output_filename='test.mp4',hook_text='She returned!',
                 hook_options={'mode':'none','position':'top','duration_ms':4000},
                 timeline=[{'type':'episode','episode':'EP1','start_ms':1000,'to_end':True}])
    result,doc,_=hook_preview.compute(tmp_path,r,work)
    assert result['sources_complete'] and doc['decision']['sampling']['timestamps_ms'][-1]==3950
    assert doc['identity']['timeline'][0]['end_ms']==6000
    key=json_sha256(doc);write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json',doc)
    r.hook_options.preview_key=key
    resolved=resolve_full_episodes(manifest([r.timeline[0].model_dump(mode='json')]),tmp_path)
    timeline=[i.model_dump(mode='json') for i in resolved.requests[0].timeline]
    assert display.approved_preview(tmp_path,r,timeline,doc['identity']['sources'])==doc['decision']
    index=tmp_path/'data/episodes/EP1.video.json';write_json_atomic(index,{'shots':[{'start_ms':0,'end_ms':6000}]})
    assert display.approved_preview(tmp_path,r,timeline,doc['identity']['sources']) is None


def test_old_three_frame_preview_is_not_accepted_under_new_sampling_policy(tmp_path):
    r=CutRequest(request_id='R1',seq=1,output_filename='test.mp4',hook_text='She returned!',
                 timeline=[{'type':'title_card','text':'Before','duration_ms':1500}])
    timeline=[i.model_dump(mode='json') for i in r.timeline]
    identity=display.preview_identity(r,timeline,[]);identity.pop('sampling_version')
    doc={'identity':identity,'decision':{},'sources_complete':True}
    key=json_sha256(doc);write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json',doc)
    r.hook_options.preview_key=key
    assert display.approved_preview(tmp_path,r,timeline,[]) is None
