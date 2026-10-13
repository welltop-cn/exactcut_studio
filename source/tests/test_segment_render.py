import copy
import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageStat

from exactcut import renderer
from exactcut.errors import ValidationError
from exactcut.media import ffprobe, run_checked
from exactcut.util import load_config


def test_effect_plan_uses_frames_and_clamps_to_own_segment():
    from exactcut.segment_effects import effect_plan
    timeline = [dict(type='episode'), dict(type='episode', transition_in='flash_white', transition_duration_ms=400,
        segment_caption={'text': 'one hour ago', 'duration_ms': 1500})]
    plan = effect_plan(timeline, [31, 17], 30)
    assert plan['total_frames'] == 48
    assert plan['captions'][0]['start_frame'] == 31
    assert plan['captions'][0]['end_frame'] == 48
    assert plan['flashes'][0] == {'boundary_frame': 31, 'before_frames': 6, 'after_frames': 6}
    with pytest.raises(ValidationError, match='短'):
        effect_plan(timeline, [31, 3], 30)


def test_base_projection_preserves_old_fields_without_effects():
    from exactcut.segment_effects import cut_timeline
    original = {'type': 'title_card', 'text': 'Title', 'duration_ms': 1000,
                'episode': None, 'start_ms': None, 'end_ms': None,
                'full_episode': False, 'to_end': False}
    assert cut_timeline([{**original, 'segment_caption': None, 'transition_in': 'none'}]) == [original]


def frame(path, index):
    import subprocess
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf',
        f'select=eq(n\\,{index})', '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
        capture_output=True, check=True)
    return Image.open(io.BytesIO(result.stdout)).convert('RGB')


def test_real_effect_render_preserves_frames_audio_and_base(tmp_path):
    source = tmp_path/'source/videos/EP1.mp4'
    source.parent.mkdir(parents=True)
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=red:s=180x320:r=30:d=2[r];color=c=blue:s=180x320:r=30:d=2[b];[r][b]concat=n=2:v=1:a=0',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '4',
        '-c:v', 'libx264', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(source)])
    config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast', hook_emphasis='none')
    timeline = [dict(type='episode', episode='EP1', start_ms=0, end_ms=1033, duration_ms=1033),
                dict(type='episode', episode='EP1', start_ms=2000, end_ms=4000, duration_ms=2000)]
    request = SimpleNamespace(request_id='effects', hook_text='', hook_options=None, ending=None)
    baseline = renderer.render_request(tmp_path, request, timeline, tmp_path/'baseline.mp4', config)
    decorated = copy.deepcopy(timeline)
    decorated[1].update(transition_in='flash_white', transition_duration_ms=400, segment_caption={'text': 'one hour ago', 'duration_ms': 500})
    result = renderer.render_request(tmp_path, request, decorated, tmp_path/'effects.mp4', config)
    assert result['base_cache_key'] == baseline['base_cache_key']
    assert result['segment_effects']['total_frames'] == 90
    assert result['segment_effects']['flashes'][0]['boundary_frame'] == 31
    assert ImageStat.Stat(frame(tmp_path/'baseline.mp4', 30)).mean[0] > 240
    assert ImageStat.Stat(frame(tmp_path/'baseline.mp4', 31)).mean[2] > 240
    for name in ('baseline', 'effects'):
        video = next(s for s in ffprobe(tmp_path/f'{name}.mp4')['streams'] if s['codec_type'] == 'video')
        assert int(video['nb_frames']) == 90
    white = frame(tmp_path/'effects.mp4', 31).crop((0, 200, 180, 300))
    assert min(ImageStat.Stat(white).mean) > 245
    assert ImageStat.Stat(frame(tmp_path/'effects.mp4', 37)).mean[0] < 30
    caption_region = (10, 65, 170, 115)
    assert ImageStat.Stat(frame(tmp_path/'effects.mp4', 40).crop(caption_region)).mean[0] > 3
    assert ImageStat.Stat(frame(tmp_path/'effects.mp4', 46).crop(caption_region)).mean[0] < 3
    def audio_hash(path):
        return run_checked(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
            '-c', 'copy', '-f', 'hash', '-']).stdout
    assert audio_hash(tmp_path/'effects.mp4') == audio_hash(tmp_path/'baseline.mp4')
    decorated[1]['segment_caption']['text'] = 'Earlier'
    updated = renderer.render_request(tmp_path, request, decorated, tmp_path/'updated.mp4', config)
    assert updated['base_cache_reused']
    assert updated['segment_effects']['identity'] != result['segment_effects']['identity']


def test_effect_fallback_joins_video_by_frame_count_not_audio_padding(tmp_path):
    render = load_config()['render']; render.update(width=108, height=192, fps=24, preset='ultrafast')
    units = []
    for i, color in enumerate(('red', 'blue')):
        unit = tmp_path/f'{i}.mp4'
        run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=108x192:r=24:d=0.5',
            '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo:d=0.8', '-c:v', 'libx264', '-c:a', 'aac', str(unit)])
        units.append(unit)
    output = tmp_path/'encoded.mp4'
    renderer._concat_units_encoded(units, output, render, 1000, frame_lengths=[12, 12])
    video = next(s for s in ffprobe(output)['streams'] if s['codec_type'] == 'video')
    assert int(video['nb_frames']) == 24
    assert ImageStat.Stat(frame(output, 11)).mean[0] > 240
    assert ImageStat.Stat(frame(output, 12)).mean[2] > 240


def test_combined_effects_keep_hook_above_white_and_warn_on_overlap(tmp_path, monkeypatch):
    from exactcut.segment_effects import effect_plan
    source = tmp_path/'source.mp4'
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=180x320:r=30',
        '-t', '3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)])
    overlay = tmp_path/'hook.png'
    image = Image.new('RGBA', (180, 320)); image.paste((255, 0, 0, 255), (0, 60, 180, 110)); image.save(overlay)
    monkeypatch.setattr(renderer, 'resolve_hook_visual', lambda *a, **kw: {})
    monkeypatch.setattr(renderer, 'render_personalized_hook_png', lambda *a, **kw: overlay)
    render = load_config()['render']; render.update(width=180, height=320, preset='ultrafast', hook_duration_ms=3000, hook_fade_out_ms=0)
    timeline = [dict(type='episode'), dict(type='episode', transition_in='flash_white', transition_duration_ms=400,
        segment_caption={'text': 'one hour ago', 'duration_ms': 500})]
    plan = effect_plan(timeline, [30, 60], 30)
    renderer._apply_hook(source, 'Hook', tmp_path/'out.mp4', render, tmp_path, segment_plan=plan)
    rgb = ImageStat.Stat(frame(tmp_path/'out.mp4', 30).crop((20, 65, 160, 100))).mean
    assert rgb[0] > 240 and rgb[1] < 10
    assert plan['warnings'] and '重叠' in plan['warnings'][0]


def test_flash_duration_uses_total_frames_and_clamps_each_side():
    from exactcut.segment_effects import effect_plan
    timeline = [dict(type='episode'), dict(type='episode', transition_in='flash_white')]
    flash = effect_plan(timeline, [60, 60], 30)['flashes'][0]
    assert flash['before_frames'] + flash['after_frames'] == 15
    timeline[1]['transition_duration_ms'] = 800
    flash = effect_plan(timeline, [60, 60], 30)['flashes'][0]
    assert (flash['before_frames'], flash['after_frames']) == (12, 12)
    flash = effect_plan(timeline, [9, 60], 30)['flashes'][0]
    assert (flash['before_frames'], flash['after_frames']) == (4, 12)
