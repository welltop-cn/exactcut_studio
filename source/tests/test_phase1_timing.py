import shutil
from unittest.mock import patch
import pytest
from PIL import Image, ImageStat
from exactcut import llm_intake as llm
from exactcut.timecode import parse_timepoint, parse_split_timepoint
from exactcut.errors import ValidationError
from exactcut.models import BatchManifest, CutRequest, TimelineItem
from exactcut.orchestrator import execute_batch
from exactcut.media import run_checked
from exactcut.util import load_config, write_json_atomic, read_json

@pytest.mark.parametrize('value', ['00:60', '01:60:00', '00:nan', '00:inf', '-1:20', '1.5:20'])
def test_invalid_time_units_blocked(value):
    with pytest.raises(ValidationError): parse_timepoint(value)


def test_milliseconds_are_not_hours_or_frames():
    assert parse_split_timepoint('01:22:008') == 82008
    assert parse_split_timepoint('00:20:00') == 20000
    assert parse_timepoint('01:22.008') == 82008


def material(segments, cells):
    return llm.Material(seq='1', title_cn='', hook_text='', notes='', source_cells=cells, segments=segments)


def segment(ep, start, end):
    return llm.Segment(type='episode', episode=ep, start_ms=start, end_ms=end, text='', duration_ms=0)


def test_ai_wrong_seconds_are_grounded_in_original_cells():
    item=material([segment('EP1',0,2000)], ['Sheet!C8','Sheet!D8','Sheet!E8'])
    evidence={'Sheet!C7':'集', 'Sheet!D7':'头', 'Sheet!E7':'尾', 'Sheet!C8':'EP1', 'Sheet!D8':'00:00:00', 'Sheet!E8':'00:20:00'}
    grounded=llm.grounded_segments(item, evidence)
    assert grounded[0].end_ms == 20000
    assert item.segments[0].end_ms == 2000  # Original AI response stays auditable.
    inline=material([segment('EP1',0,2000)], ['Sheet!A1'])
    assert llm.grounded_segments(inline, {'Sheet!A1':'EP1 0s-20s'})[0].end_ms==20000


def test_grounding_rejects_order_mismatch_and_ambiguous_units():
    item=material([segment('EP2',0,2000)], ['S!A1'])
    with pytest.raises(ValueError, match='顺序'):llm.grounded_segments(item, {'S!A1':'EP1 0s-20s'})
    item.segments[0].episode='EP1'
    with pytest.raises(ValueError, match='单位'):llm.grounded_segments(item, {'S!A1':'EP1 00:00:00-00:20:00'})


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')
def test_twenty_second_cut_has_correct_source_positions_and_segment_qc(tmp_path):
    source=tmp_path/'source/videos/EP1.mp4'; source.parent.mkdir(parents=True)
    # Four six-second blocks; the requested cut starts 2s into the source.
    cmd=['ffmpeg','-y','-v','error']
    for color in ('red','green','blue','yellow'):
        cmd += ['-f','lavfi','-i',f'color=c={color}:s=108x192:r=30:d=6']
    cmd += ['-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-filter_complex',
            '[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]','-map','[v]','-map','4:a','-t','24',
            '-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)]
    run_checked(cmd)
    write_json_atomic(tmp_path/'project.json', {'project_id':'101460'})
    request=CutRequest(request_id='R1',seq=1,output_filename='cut.mp4', timeline=[TimelineItem(type='episode',episode='EP1',start_ms=2000,end_ms=22000)])
    manifest=BatchManifest(drama_id='101460',drama_title='test',business_date='20261006',author='test',source_workbook='test.xlsx',requests=[request])
    config=load_config();config['render'].update(width=108,height=192,preset='ultrafast')
    results=execute_batch(tmp_path,manifest,config)
    record=results['requests']['R1'];assert record['status']=='qc_passed'
    assert abs(record['duration_ms']-20000)<100
    output=tmp_path/'outputs/20261006/videos/cut.mp4'
    for second, channel in [(0.1,0),(5,1),(11,2),(17,0)]:
        frame=tmp_path/f'{second}.png'
        run_checked(['ffmpeg','-y','-v','error','-ss',str(second),'-i',str(output),'-frames:v','1',str(frame)])
        with Image.open(frame) as im: mean=ImageStat.Stat(im.convert('RGB')).mean
        if second==17: assert mean[0]>200 and mean[1]>200 and mean[2]<30
        else: assert mean[channel] > max(mean[i] for i in range(3) if i != channel)+50
    plan=read_json(tmp_path/'outputs/20261006/audit/R1/clip_plan.json')
    assert abs(plan['unit_checks'][0]['actual_ms']-20000)<100
    with patch('exactcut.renderer.duration_ms', return_value=2000):
        failed=execute_batch(tmp_path,manifest,config,force=True)
    assert failed['requests']['R1']['status']=='failed'
    assert '片段 1 时长不符' in failed['requests']['R1']['error']


@pytest.mark.integration
def test_source_with_duration_but_without_video_track_is_rejected(tmp_path):
    from exactcut.media import validate_manifest_sources
    source=tmp_path/'source/videos/EP1.mp4';source.parent.mkdir(parents=True);source.write_bytes(b'audio-only')
    write_json_atomic(tmp_path/'project.json',{'project_id':'101460'})
    request=CutRequest(request_id='R1',seq=1,output_filename='cut.mp4',timeline=[TimelineItem(type='episode',episode='EP1',start_ms=0,end_ms=1000)])
    manifest=BatchManifest(drama_id='101460',drama_title='test',business_date='20261006',author='test',source_workbook='test.xlsx',requests=[request])
    with patch('exactcut.media.ffprobe',return_value={'format':{'duration':'20'},'streams':[{'codec_type':'audio'}]}):
        with pytest.raises(ValidationError,match='视频轨道'):validate_manifest_sources(manifest,tmp_path)
