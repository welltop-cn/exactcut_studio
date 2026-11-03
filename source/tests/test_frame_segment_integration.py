"""Combined frame-editor and segment-effect contract across feature branches."""
import pytest
from copy import deepcopy
from exactcut import intake,source_frames,workflow as w
from test_source_frames import source
from test_web_base_path import app
from test_source_frame_timecode import draft,frame_clip

pytestmark = pytest.mark.integration


def test_frame_confirmation_keeps_caption_and_transition(source):
    _, item, _, _ = source
    path = w.folder(item['id'])
    data = draft(frame_clip(start_f='000',end_s='00',end_f='029',
                           segment_caption={'text':'开始', 'duration_ms':500}))
    data['rows'][0]['clips'].append(frame_clip(start_s='01',start_f='000',end_s='01',end_f='029',
        segment_caption={'text':'后来','duration_ms':700},transition_in='flash_white',transition_duration_ms=800))
    before=deepcopy(data)
    source_frames.prepare(path,data,'EP1')
    manifest,errors=intake.validate_draft(data,str(path/'source.xlsx'))
    assert not errors,errors
    assert data==before
    a,b=manifest.requests[0].timeline
    assert a.segment_caption.text=='开始'
    assert b.segment_caption.text=='后来'
    assert b.transition_in=='flash_white'
    assert b.transition_duration_ms==800
    assert (a.start_ms,a.end_ms,b.start_ms,b.end_ms)==(0,1001,1001,2002)


def test_hidden_picker_cached_frame_source_is_indexed_by_background_job(source, monkeypatch, tmp_path):
    import queue
    from exactcut import webserver as web,source_prefetch,source_cache
    _,item,_,_=source
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH','0')
    monkeypatch.setattr(web,'JOBS',w.ROOT/'.web-state/jobs');web.JOBS.mkdir(exist_ok=True)
    monkeypatch.setattr(web,'WORK',queue.Queue())
    data=draft(frame_clip())
    monkeypatch.setattr(source_cache,'ensure',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not download cached source')))
    result=web.prefetch_sources(item['id'],{'draft':data},'admin')
    assert result['episodes'][0]['status']=='queued'
    job,_=web.WORK.get_nowait()
    assert source_prefetch.run(item['id'],job['id'])==0
    assert not intake.validate_draft(data,str(w.folder(item['id'])/'source.xlsx'))[1]
    assert web.prefetch_sources(item['id'],{'draft':data},'admin')['episodes'][0]['status']=='ready'
    assert web.WORK.empty()


def test_incomplete_frame_row_with_caption_is_not_silently_dropped(source):
    _,item,_,_=source
    path=w.folder(item['id']);data=draft(frame_clip())
    data['rows'][0]['clips'].append(frame_clip(episode='',start_f='000',end_s='00',end_f='000',segment_caption={'text':'keep','duration_ms':500}))
    source_frames.prepare(path,data,'EP1')
    assert intake.validate_draft(data,str(path/'source.xlsx'))[1]
