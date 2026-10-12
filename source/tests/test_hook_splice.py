"""Real H.264/AAC coverage of the short Hook splice, including B-frame joins."""
import json
import shutil

import pytest
from PIL import Image

from exactcut import renderer
from exactcut.media import ffprobe, run_checked
from exactcut.util import load_config

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')


def frame_hashes(path, stream='v:0'):
    text = run_checked(['ffmpeg', '-v', 'error', '-i', str(path), '-map', f'0:{stream}',
                        '-vsync', '0', '-f', 'framemd5', '-']).stdout
    return [line.split(',')[-1].strip() for line in text.splitlines()
            if line and not line.startswith('#')]


def timestamps(path):
    return json.loads(run_checked(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(path)]).stdout)['frames']


@pytest.fixture
def hook_case(tmp_path, monkeypatch, request):
    source = tmp_path / 'source.mp4'
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=108x192:r=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '12',
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-g', str(getattr(request, 'param', 60)),
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2', str(source)])
    png = tmp_path / 'hook.png'
    Image.new('RGBA', (108, 192), (200, 30, 80, 180)).save(png)
    monkeypatch.setattr(renderer, 'resolve_hook_visual', lambda *a, **kw: {})
    monkeypatch.setattr(renderer, 'render_personalized_hook_png', lambda *a, **kw: png)
    render = {**load_config()['render'], 'width': 108, 'height': 192,
              'hook_duration_ms': 917, 'hook_fade_out_ms': 400}
    return source, render


@pytest.mark.parametrize('hook_case', [60, 47], indirect=True)
@pytest.mark.parametrize('fade_ms', [0, 400])
def test_splice_preserves_tail_audio_and_frame_timing(hook_case, tmp_path, fade_ms):
    source, render = hook_case
    render['hook_fade_out_ms'] = fade_ms
    output = tmp_path / 'hooked.mp4'
    decision = renderer._apply_hook(source, 'Hook', output, render, tmp_path, normalized_input=True)
    original = frame_hashes(source)
    actual = frame_hashes(output)
    assert len(actual) == len(original) == 360
    assert actual[:27] != original[:27]
    assert actual[60:] == original[60:]
    assert frame_hashes(output, 'a:0') == frame_hashes(source, 'a:0')
    for index, frame in enumerate(timestamps(output)):
        assert abs(float(frame['best_effort_timestamp_time']) - index / 30) < .00001
    assert decision['duration_ms'] == 917
    assert abs(float(ffprobe(output)['format']['duration']) - 12) < .001


def test_untrusted_input_keeps_full_encode(hook_case, tmp_path):
    source, render = hook_case
    output = tmp_path / 'untrusted.mp4'
    renderer._apply_hook(source, 'Hook', output, render, tmp_path)
    assert frame_hashes(output)[60:] != frame_hashes(source)[60:]
    assert frame_hashes(output, 'a:0') == frame_hashes(source, 'a:0')


def test_splice_rejects_a_boundary_that_decodes_differently(hook_case, tmp_path, monkeypatch):
    from exactcut import hook_splice
    source, render = hook_case
    events = []
    monkeypatch.setattr(hook_splice.diag, 'event', lambda name, **kw: events.append((name, kw)))
    def corrupt_join(command, **kwargs):
        result = run_checked(command, **kwargs)
        if '-f' in command and command[command.index('-f') + 1] == 'concat':
            candidate = tmp_path / 'wrong.mp4'
            run_checked(['ffmpeg', '-y', '-v', 'error', '-i', command[-1], '-map', '0:v:0',
                         '-map', '0:a:0', '-c:v', 'libx264', '-crf', '35', '-c:a', 'copy', str(candidate)])
            candidate.replace(command[-1])
        return result
    monkeypatch.setattr(hook_splice, 'run_checked', corrupt_join)
    renderer._apply_hook(source, 'Hook', tmp_path / 'output.mp4', render, tmp_path, normalized_input=True)
    splice_events = [value for name, value in events if name == 'render.hook_splice']
    assert splice_events and splice_events[-1]['used'] is False
    assert 'decoded boundary' in splice_events[-1]['reason']


@pytest.mark.parametrize('fade_ms', [0, 400])
def test_prefix_matches_original_overlay_filter(hook_case, tmp_path, monkeypatch, fade_ms):
    from exactcut import hook_splice
    source, render = hook_case
    render['hook_fade_out_ms'] = fade_ms
    reference = tmp_path / 'reference.mp4'
    counts = []
    def encode_reference(command, **kwargs):
        result = run_checked(command, **kwargs)
        if '-frames:v' in command and command[-1].endswith('prefix.mp4'):
            counts.append(int(command[command.index('-frames:v') + 1]))
            assert command[command.index('-preset') + 1] == 'medium'
            assert command[command.index('-crf') + 1] == '18'
            original = list(command)
            duration = original.index('-t')  # Unbounded PNG reference, same fade/overlay.
            del original[duration:duration + 2]
            original[-1] = str(reference)
            run_checked(original)
        return result
    monkeypatch.setattr(hook_splice, 'run_checked', encode_reference)
    output = tmp_path / 'output.mp4'
    renderer._apply_hook(source, 'Hook', output, render, tmp_path, normalized_input=True)
    assert counts == [60]
    assert frame_hashes(output)[:60] == frame_hashes(reference)


@pytest.mark.parametrize('mode', ['open_gop', 'mpeg4', 'no_audio'])
def test_format_guards_and_audio_optional(hook_case, tmp_path, monkeypatch, mode):
    from exactcut import hook_splice
    source, render = hook_case
    changed = tmp_path / 'changed.mp4'
    command = ['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-map', '0:v:0']
    if mode == 'no_audio':
        command += ['-c:v', 'copy', '-an']
    elif mode == 'open_gop':
        command += ['-map', '0:a:0', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
                    '-x264-params', 'open-gop=1:keyint=47:min-keyint=47:scenecut=0', '-c:a', 'copy']
    else:
        command += ['-map', '0:a:0', '-c:v', 'mpeg4', '-c:a', 'copy']
    run_checked([*command, str(changed)])
    events = []
    monkeypatch.setattr(hook_splice.diag, 'event', lambda name, **kw: events.append((name, kw)))
    output = tmp_path / 'output.mp4'
    renderer._apply_hook(changed, 'Hook', output, render, tmp_path, normalized_input=True)
    splice = [value for name, value in events if name == 'render.hook_splice'][-1]
    assert splice['used'] is (mode == 'no_audio')
    if mode == 'open_gop':
        assert 'safe IDR' in splice['reason']
    if mode == 'no_audio':
        assert frame_hashes(output)[60:] == frame_hashes(changed)[60:]
        assert not any(s['codec_type'] == 'audio' for s in ffprobe(output)['streams'])
    else:
        legacy = tmp_path / 'legacy.mp4'
        renderer._apply_hook(changed, 'Hook', legacy, render, tmp_path)
        assert frame_hashes(output) == frame_hashes(legacy)
        assert frame_hashes(output, 'a:0') == frame_hashes(changed, 'a:0')


def test_splice_across_normalized_unit_join(hook_case, tmp_path):
    source, render = hook_case
    unit = tmp_path / 'unit.mp4'
    renderer._render_card_unit('Card', 1567, unit, render, tmp_path)
    joined = tmp_path / 'joined.mp4'
    renderer._concat_units([unit, source], joined, render, 13567)
    output = tmp_path / 'output.mp4'
    renderer._apply_hook(joined, 'Hook', output, render, tmp_path, normalized_input=True)
    original = frame_hashes(joined)
    actual = frame_hashes(output)
    assert len(actual) == len(original) == 407
    assert actual[47:] == original[47:]
    assert frame_hashes(output, 'a:0') == frame_hashes(joined, 'a:0')
    for index, frame in enumerate(timestamps(output)):
        assert abs(float(frame['best_effort_timestamp_time']) - index / 30) < .00001
