import pytest
from PIL import Image, ImageDraw

from exactcut import qc
from exactcut.error_guidance import guidance
from exactcut.renderer import _make_card_png
from exactcut.util import load_config


@pytest.fixture
def frame_qc(tmp_path, monkeypatch):
    config = load_config()
    render = config['render']
    size = (render['width'], render['height'])
    video = tmp_path / 'output.mp4'
    video.write_bytes(b'fixture')
    monkeypatch.setattr(qc, 'ffprobe', lambda _: {
        'format': {'duration': '1.0'}, 'streams': [
            {'codec_type': 'video', 'width': size[0], 'height': size[1], 'avg_frame_rate': '30/1'},
            {'codec_type': 'audio', 'channels': render['audio_channels']} ]})
    def inspect(image, timeline=None, expected=1000):
        monkeypatch.setattr(qc, 'run_checked', lambda args, **kwargs: image.save(args[-1]))
        return qc.inspect_output(video, expected, config, timeline)
    return inspect, size, tmp_path


def card(text='Later', duration=1000):
    return {'type': 'title_card', 'text': text, 'duration_ms': duration}


@pytest.mark.integration
def test_sparse_black_title_card_passes_with_timeline(frame_qc):
    inspect, size, path = frame_qc
    _make_card_png(path/'card.png', 'Later', *size)
    image = Image.open(path/'card.png')
    result = inspect(image, [card()])
    assert result['first_frame_mean_luma'] < 2
    assert result['allowed']
    assert not inspect(image)['allowed']  # No general relaxation for episode frames.


@pytest.mark.integration
@pytest.mark.parametrize('hook_only', [False, True])
def test_missing_card_text_is_not_exempted(frame_qc, hook_only):
    inspect, size, _ = frame_qc
    image = Image.new('RGB', size, 'black')
    if hook_only:
        ImageDraw.Draw(image).rectangle((100,100,700,200), fill='white')
    result = inspect(image, [card()])
    assert not result['allowed']
    assert 'title card text is missing or unreadable' in result['errors']


@pytest.mark.integration
def test_black_episode_is_rejected_even_when_followed_by_card(frame_qc):
    inspect, size, _ = frame_qc
    result = inspect(Image.new('RGB', size, 'black'), [
        {'type':'episode','start_ms':0,'end_ms':1000},card()])
    assert 'first frame is black' in result['errors']


@pytest.mark.integration
def test_short_card_does_not_exempt_sample_in_following_episode(frame_qc):
    inspect, size, _ = frame_qc
    result = inspect(Image.new('RGB', size, 'black'), [
        card(duration=50),{'type':'episode','start_ms':0,'end_ms':950}])
    assert 'first frame is black' in result['errors']


@pytest.mark.integration
def test_card_still_checks_duration_and_normal_video_passes(frame_qc):
    inspect, size, path = frame_qc
    _make_card_png(path/'card.png', 'Later', *size)
    assert not inspect(Image.open(path/'card.png'), [card()], expected=2000)['allowed']
    assert inspect(Image.new('RGB', size, '#6080a0'))['allowed']


@pytest.mark.parametrize('error,title', [
    ('first frame is black','成片开头画面过暗或疑似黑屏'),
    ('title card text is missing or unreadable','字卡文字未正常显示'),
    ('cannot extract first frame','无法读取成片开头画面'),
])
def test_specific_qc_guidance(error,title):
    result=guidance('成片QC未通过：'+error, 'qc')
    assert result['title']==title
    assert result['code']=='qc'


@pytest.mark.integration
@pytest.mark.parametrize('text,hook,duration', [
    ('Later','',1000), ('稍后','Concurrent test\n1',1000),
    ('稍后','Concurrent test\n1',18000),
])
def test_real_encoded_sparse_card_passes_production_pipeline(tmp_path, text, hook, duration):
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('requires ffmpeg')
    from exactcut.models import BatchManifest, CutRequest
    from exactcut.orchestrator import execute_batch
    from exactcut.util import write_json_atomic
    config=load_config()
    request=CutRequest(request_id='R1',seq=1,output_filename='card.mp4',hook_text=hook,
                       hook_options={'mode':'none','position':'top','font_size':60,'text_color':'#12AB34'},
                       timeline=[card(text, duration=duration)])
    manifest=BatchManifest(drama_id='989799',drama_title='QC fixture',business_date='20261010',
                           author='test',source_workbook='fixture',requests=[request])
    write_json_atomic(tmp_path/'project.json',{'project_id':'989799'})
    result=execute_batch(tmp_path,manifest,config)
    record=result['requests']['R1']
    assert record['status']=='qc_passed',record.get('error')
    assert record['qc']['first_frame_mean_luma']<2
    assert record['qc']['title_card_text_coverage']>=0.8
    from exactcut.media import ffprobe
    video=next(s for s in ffprobe(record['output'])['streams'] if s['codec_type']=='video')
    numerator,denominator=map(int,video['avg_frame_rate'].split('/'))
    assert numerator/denominator==config['render']['fps']
    # Frame-boundary rounding may differ by one frame; no timing drift is allowed.
    assert abs(int(video['nb_frames'])-round(duration*config['render']['fps']/1000))<=1
    assert abs(float(video['duration'])*1000-duration)<=1000/config['render']['fps']+1


@pytest.mark.integration
def test_sample_can_land_in_card_after_short_episode(frame_qc):
    inspect, size, path=frame_qc
    _make_card_png(path/'card.png','Later',*size)
    assert inspect(Image.open(path/'card.png'),[
        {'type':'episode','start_ms':0,'end_ms':50},card(duration=950)])['allowed']
