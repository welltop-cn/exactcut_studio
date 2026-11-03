"""Do not process invisible overlay frames after the Hook ends."""
import pytest
from PIL import Image
from exactcut import renderer
from exactcut.media import run_checked
from exactcut.util import load_config

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('end_ms,fade_ms', [(900,400),(917,0),(1500,1000)])
def test_bounded_overlay_matches_every_frame_and_audio(tmp_path, monkeypatch, end_ms, fade_ms):
    source=tmp_path/'source.mp4'
    run_checked(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=s=108x192:r=30',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','1.5',
                 '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)])
    png=tmp_path/'hook.png'; Image.new('RGBA',(108,192),(200,30,80,180)).save(png)
    monkeypatch.setattr(renderer,'resolve_hook_visual',lambda *a,**kw: {})
    monkeypatch.setattr(renderer,'render_personalized_hook_png',lambda *a,**kw: png)
    commands=[]
    def capture(command):
        commands.append(command)
        return run_checked(command)
    monkeypatch.setattr(renderer,'run_checked',capture)
    render={**load_config()['render'],'width':108,'height':192,
            'hook_duration_ms':end_ms,'hook_fade_out_ms':fade_ms}
    output=tmp_path/'new.mp4'
    renderer._apply_hook(source,'Hook',output,render,tmp_path)
    command=commands[-1]
    image_input=command.index(str(png))
    assert '-t' in command[command.index('-loop'):image_input-1]
    reference=list(command)
    pos=reference.index('-t'); del reference[pos:pos+2]
    reference[-1]=str(tmp_path/'old.mp4'); run_checked(reference)
    def hashes(path):
        return run_checked(['ffmpeg','-v','error','-i',str(path),'-map','0:v:0','-map','0:a:0',
                            '-f','framemd5','-']).stdout
    assert hashes(output)==hashes(tmp_path/'old.mp4')


@pytest.mark.parametrize('duration', ['1.5', '4'])
def test_normalized_full_hook_keeps_every_frame_through_eof(tmp_path, monkeypatch, duration):
    from exactcut.media import ffprobe
    source = tmp_path / 'source.mp4'
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=108x192:r=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', duration,
                 '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)])
    png = tmp_path / 'hook.png'
    Image.new('RGBA', (108, 192), (200, 30, 80, 180)).save(png)
    monkeypatch.setattr(renderer, 'resolve_hook_visual', lambda *a, **kw: {})
    monkeypatch.setattr(renderer, 'render_personalized_hook_png', lambda *a, **kw: png)
    render = {**load_config()['render'], 'width': 108, 'height': 192,
              'crf': 0, 'hook_duration_ms': 900, 'hook_fade_out_ms': 400}
    output = tmp_path / 'hooked.mp4'
    renderer._apply_hook(source, 'Hook', output, render, tmp_path, normalized_input=True)
    original = next(s for s in ffprobe(source)['streams'] if s['codec_type'] == 'video')
    actual = next(s for s in ffprobe(output)['streams'] if s['codec_type'] == 'video')
    assert int(actual['nb_frames']) == int(original['nb_frames']) == round(float(duration) * 30)
    assert actual['duration'] == original['duration']
