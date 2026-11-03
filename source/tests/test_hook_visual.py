from pathlib import Path
import shutil

import pytest
from PIL import Image

from exactcut.hook_visual import AUTO_STYLE_IDS, analyze_frame_image, choose_hook_visual, render_personalized_hook_png
from exactcut.errors import ValidationError
from exactcut.media import run_checked, ffprobe
from exactcut.renderer import _apply_hook
from exactcut.util import load_config

HOOK = 'She married her enemy.\nHe knows **her secret.**'


def test_six_styles_and_frame_adaptation(tmp_path):
    warm = analyze_frame_image(Image.new('RGB', (1080, 1920), (100, 60, 30)))
    cool = analyze_frame_image(Image.new('RGB', (1080, 1920), (30, 60, 100)))
    for style in AUTO_STYLE_IDS:
        decision = choose_hook_visual({'hook_text': HOOK}, warm, forced_style=style)
        alternate = choose_hook_visual({'hook_text': HOOK}, cool, forced_style=style)
        assert decision['palette'] != alternate['palette']
        assert decision['accent_phrase'] == 'her secret.'
        png = render_personalized_hook_png(HOOK, tmp_path, decision)
        with Image.open(png) as image:
            assert image.size == (1080, 1920)
            assert image.getbbox() is not None
    assert choose_hook_visual({'hook_text': 'A quiet moment.'}, warm)['style_id'] == 'default'
    assert choose_hook_visual({'hook_text': 'A dangerous gun threat.'}, warm)['style_id'] == 'danger_warning'


def test_long_copy_is_rejected_instead_of_truncated(tmp_path):
    decision = choose_hook_visual({'hook_text': HOOK}, {}, forced_style='default')
    with pytest.raises(ValidationError, match='过长'):
        render_personalized_hook_png('word ' * 150, tmp_path, decision)


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')
def test_overlay_preserves_duration_audio_and_fades(tmp_path):
    source = tmp_path / 'source.mp4'
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'color=c=0x34302e:s=1080x1920:r=30', '-f', 'lavfi', '-i',
                 'anullsrc=r=44100:cl=stereo', '-t', '1.5', '-c:v', 'libx264',
                 '-preset', 'ultrafast', '-c:a', 'aac', str(source)])
    render = {**load_config()['render'], 'preset': 'ultrafast',
              'hook_duration_ms': 900, 'hook_fade_out_ms': 400}
    for style in sorted(AUTO_STYLE_IDS):
        output = tmp_path / f'{style}.mp4'
        decision = _apply_hook(source, HOOK, output, {**render, 'hook_style': style}, tmp_path)
        info = ffprobe(output)
        assert abs(float(info['format']['duration']) - 1.5) < 0.1
        assert any(s['codec_type'] == 'audio' and s['channels'] == 2 for s in info['streams'])
        assert decision['style_id'] == style
        levels = []
        for time in ('0.1', '0.7', '1.2'):
            frame = tmp_path / f'{style}-{time}.png'
            run_checked(['ffmpeg', '-y', '-v', 'error', '-ss', time, '-i', str(output), '-frames:v', '1', str(frame)])
            with Image.open(frame) as image:
                levels.append(image.convert('RGB').crop((0, 130, 1080, 600)).copy())
        from PIL import ImageChops, ImageStat
        # Compare against the post-Hook frame: the dark backing fades with the text.
        difference = lambda frame: sum(ImageStat.Stat(ImageChops.difference(frame, levels[2])).mean)
        assert difference(levels[0]) > difference(levels[1]) > 0
    no_hook = tmp_path / 'empty.mp4'
    assert _apply_hook(source, '', no_hook, render, tmp_path) is None
    assert source.read_bytes() == no_hook.read_bytes()


def test_long_excel_hook_preserves_original_layout_and_all_words(tmp_path):
    text = "Abandoned by my family and sent to the execution block, I awaken my wolf spirit and strike back!"
    decision = choose_hook_visual({'hook_text': text}, {}, forced_style='emotional_diary')
    render_personalized_hook_png(text, tmp_path, decision)
    layout = decision['layout']
    assert layout['font_size'] >= 40
    assert len(layout['lines']) == 3
    assert ' '.join(layout['lines']) == text
