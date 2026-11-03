"""New optional effects preserve pre-upgrade preview/output identities."""
import pytest
from copy import deepcopy
from exactcut import orchestrator, hook_presentation as display
from exactcut.models import BatchManifest, CutRequest
from exactcut.util import json_sha256,write_json_atomic,file_sha256


def request():
    return CutRequest(request_id='R1',seq=1,output_filename='test.mp4',hook_text='',
        timeline=[{'type':'title_card','text':'Earlier','duration_ms':1500}])


def legacy_timeline(r):
    return [{k:v for k,v in item.model_dump(mode='json').items()
             if k not in {'segment_caption','transition_in','transition_duration_ms'}} for item in r.timeline]


def legacy_fingerprint(r):
    payload=r.model_dump(mode='json');payload.pop('brand_overlay', None);payload['timeline']=legacy_timeline(r)
    return json_sha256({'pipeline':'exact_timeline_v3_unit_qc','request':payload,'sources':[],'config':{}})


def test_approved_old_preview_remains_usable(tmp_path):
    r=request()
    identity={'layout_version':display.HOOK_LAYOUT_VERSION,'sampling_version':display.SAMPLING_VERSION,
              'text':r.hook_text,'options':r.hook_options.model_dump(exclude={'preview_key'}),
              'timeline':legacy_timeline(r),'sources':[]}
    doc={'identity':identity,'decision':{'approved':'keep'},'sources_complete':True}
    key=json_sha256(doc);write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json',doc)
    r.hook_options.preview_key=key
    assert display.approved_preview(tmp_path,r,[i.model_dump(mode='json') for i in r.timeline],[])==doc['decision']


@pytest.mark.integration
def test_finished_old_output_survives_retry_without_render(tmp_path,monkeypatch):
    r=request();write_json_atomic(tmp_path/'project.json',{'project_id':'101573','excluded_episodes':[]})
    m=BatchManifest(drama_id='101573',drama_title='fixture',source_workbook='fixture.xlsx',business_date='20261013',author='test',requests=[r])
    folder=tmp_path/'outputs/20261013';video=folder/'videos/test.mp4';video.parent.mkdir(parents=True);video.write_bytes(b'checked-output')
    record={'status':'fbbm_ready','fingerprint':legacy_fingerprint(r),'output_sha256':file_sha256(video),'publication':{'material_id':'keep'}}
    write_json_atomic(folder/'results.json',{'requests':{'R1':record}})
    def forbidden(*a,**kw):raise AssertionError('old finished output must not render')
    monkeypatch.setattr(orchestrator,'render_request',forbidden)
    monkeypatch.setattr(orchestrator,'upload_generated_preview',lambda *a:None)
    monkeypatch.setattr(orchestrator,'upload_generated_original',lambda *a:None)
    result=orchestrator.execute_batch(tmp_path,m,{})['requests']['R1']
    assert result['status']=='fbbm_ready' and result['cache_reused']
    assert result['publication']==record['publication']
    assert video.read_bytes()==b'checked-output'


def test_active_effects_change_identity_but_disabled_defaults_do_not(tmp_path):
    r=CutRequest(request_id='R1',seq=1,output_filename='x.mp4',timeline=[{'type':'episode','episode':'EP1','start_ms':0,'end_ms':1000}])
    original=legacy_timeline(r)
    baseline=display.preview_identity(r,original,[])
    modern=[i.model_dump(mode='json') for i in r.timeline]
    assert display.preview_identity(r,modern,[])==baseline
    caption=deepcopy(modern);caption[0]['segment_caption']={'text':'x','duration_ms':500}
    assert display.preview_identity(r,caption,[])!=baseline
    flash=deepcopy(modern);flash[0].update(transition_in='flash_white',transition_duration_ms=800)
    assert display.preview_identity(r,flash,[])!=baseline


@pytest.mark.integration
def test_output_identity_changes_for_active_caption_and_flash(tmp_path):
    from exactcut.models import SegmentCaption
    video=tmp_path/'source/videos/EP1.mp4';video.parent.mkdir(parents=True);video.write_bytes(b'source')
    r=CutRequest(request_id='R1',seq=1,output_filename='x.mp4',timeline=[{'type':'episode','episode':'EP1','start_ms':0,'end_ms':1000}]*2)
    baseline=orchestrator._request_fingerprint(tmp_path,r,{})
    r.timeline[0].transition_duration_ms=800
    assert orchestrator._request_fingerprint(tmp_path,r,{})==baseline
    r.timeline[0].segment_caption=SegmentCaption(text='caption',duration_ms=500)
    caption=orchestrator._request_fingerprint(tmp_path,r,{})
    assert caption!=baseline
    r.timeline[0].segment_caption.text='changed'
    assert orchestrator._request_fingerprint(tmp_path,r,{})!=caption
    r.timeline[0].segment_caption=None;r.timeline[1].transition_in='flash_white'
    flash=orchestrator._request_fingerprint(tmp_path,r,{})
    assert flash!=baseline
    r.timeline[1].transition_duration_ms=900
    assert orchestrator._request_fingerprint(tmp_path,r,{})!=flash


@pytest.mark.integration
def test_edited_current_survives_batch_retry(tmp_path,monkeypatch):
    r=request();write_json_atomic(tmp_path/'project.json',{'project_id':'101573','excluded_episodes':[]})
    m=BatchManifest(drama_id='101573',drama_title='fixture',source_workbook='fixture.xlsx',business_date='20261013',author='test',requests=[r])
    folder=tmp_path/'production';original=folder/'videos/test.mp4';original.parent.mkdir(parents=True);original.write_bytes(b'original')
    master=folder/'edits/R1/job/master/test.mp4';master.parent.mkdir(parents=True);master.write_bytes(b'edited')
    preview=folder/'edits/R1/job/preview/test.mp4';preview.parent.mkdir(parents=True);preview.write_bytes(b'preview')
    record={'status':'qc_passed','fingerprint':legacy_fingerprint(r),'output_sha256':file_sha256(master),
        'edited_media':{'master':str(master),'preview':str(preview)},'preview':{'master_sha256':file_sha256(master),'sha256':file_sha256(preview)}}
    write_json_atomic(folder/'results/R1.json',record)
    def forbidden(*a,**kw):raise AssertionError('edited current must not render')
    monkeypatch.setattr(orchestrator,'render_request',forbidden)
    monkeypatch.setattr(orchestrator,'upload_generated_preview',lambda *a:None)
    monkeypatch.setattr(orchestrator,'upload_generated_original',lambda *a:None)
    result=orchestrator.execute_batch(tmp_path,m,{},output_dir=folder)['requests']['R1']
    assert result['status']=='qc_passed' and result['output_sha256']==file_sha256(master)
    assert original.read_bytes()==b'original'
