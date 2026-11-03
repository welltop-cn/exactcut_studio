import json
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import pytest
from exactcut import cutting
from exactcut.media import bounded_ffmpeg
from exactcut.util import file_sha256


def make_video(path, frames=90, fps='30', audio=True):
    # Seven wide binary stripes encode the actual frame number, survive H264.
    raw = b''.join(bytes([235 if n & (1 << bit) else 16]) * 16
                   for n in range(frames) for _ in range(64) for bit in range(7))
    cmd = ['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'gray',
           '-s', '112x64', '-r', fps, '-i', 'pipe:0']
    if audio:
        cmd += ['-f', 'lavfi', '-i', 'aevalsrc=sin(2*PI*880*t)*lt(mod(t+0.5\\,1)\\,0.035):s=44100',
                '-t', str(float(Fraction(frames, 1) / Fraction(fps))), '-c:a', 'aac']
    cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18', '-g', '250',
            '-pix_fmt', 'yuv420p', str(path)]
    subprocess.run(bounded_ffmpeg(cmd), input=raw, capture_output=True, check=True)
    return path


def frame_numbers(path):
    raw = subprocess.check_output(bounded_ffmpeg(['ffmpeg', '-v', 'error', '-i', str(path),
          '-f', 'rawvideo', '-pix_fmt', 'gray', '-']))
    size = 112 * 64
    return [sum((raw[i+32*112+bit*16+8] > 128) << bit for bit in range(7))
            for i in range(0, len(raw), size)]


def test_range_contract():
    assert cutting.normalize_ranges([[25, 26], [15, 25], [10, 20]], 90) == [[10, 26]]
    assert cutting.keep_ranges([[10, 11], [30, 40]], 90) == [[0, 10], [11, 30], [40, 90]]
    for ranges in ([], [[0, 90]], [[True, 4]], [[1.1, 4]], [[-1, 4]], [[2, 2]], [[0, 91]], 'bad'):
        with pytest.raises(ValueError):
            cutting.normalize_ranges(ranges, 90)


@pytest.mark.integration
@pytest.mark.parametrize('audio,fps', [(True, '30'), (False, '30000/1001')])
def test_actual_frames_removed_without_changing_original(tmp_path, audio, fps):
    source = make_video(tmp_path/'source.mp4', fps=fps, audio=audio)
    original_sha = file_sha256(source)
    meta = cutting.probe_source(source)
    assert Fraction(meta['fps_num'], meta['fps_den']) == Fraction(fps)
    output = tmp_path/'cut.mp4'
    result = cutting.render_video(source, output, [[10, 11], [30, 40]], meta)
    assert result['frame_count'] == 79
    assert frame_numbers(output) == list(range(10))+list(range(11, 30))+list(range(40, 90))
    assert file_sha256(source) == original_sha
    assert result['has_audio'] is audio
    if audio:
        import numpy as np
        pcm = subprocess.check_output(bounded_ffmpeg(['ffmpeg', '-v', 'error', '-i', str(output),
              '-f', 'f32le', '-ac', '1', '-ar', '44100', '-']))
        samples = np.frombuffer(pcm, dtype='<f4')
        # Original pulses .5,1.5,2.5 seconds shift by precisely removed durations.
        for when in (.5-1/30, 1.5-11/30, 2.5-11/30):
            window = samples[int((when+.008)*44100):int((when+.025)*44100)]
            assert np.sqrt(np.mean(window**2)) > .3
        assert abs(len(samples)/44100-79/30) < 2048/44100


@pytest.mark.integration
def test_many_cuts_and_head_tail_preserve_exact_order(tmp_path):
    source = make_video(tmp_path/'many.mp4', frames=120, fps='30000/1001')
    deleted = [[i, i+1] for i in range(0, 114, 3)] + [[119, 120]]
    output = tmp_path/'out.mp4'
    result = cutting.render_video(source, output, deleted, cutting.probe_source(source))
    expected = [n for n in range(120) if not any(a <= n < b for a, b in deleted)]
    assert result['frame_count'] == len(expected)
    assert frame_numbers(output) == expected


@pytest.mark.integration
def test_rejects_variable_frame_timestamps(tmp_path):
    source = make_video(tmp_path/'source.mp4', audio=False)
    variable = tmp_path/'variable.mp4'
    subprocess.run(bounded_ffmpeg(['ffmpeg', '-y', '-v', 'error', '-i', str(source),
                   '-vf', "select='not(eq(n,10))'", '-fps_mode', 'vfr', str(variable)]), check=True)
    with pytest.raises(ValueError, match='帧率|时间戳'):
        cutting.probe_source(variable)


@pytest.mark.integration
@pytest.mark.parametrize('fps', ['25', '30', '30000/1001', '60000/1001'])
def test_fast_frame_window_matches_sequential_decode(tmp_path, monkeypatch, fps):
    from PIL import Image
    import io
    source = make_video(tmp_path/'source.mp4', frames=420, fps=fps, audio=False)
    bframes = tmp_path/'bframes.mp4'
    subprocess.run(bounded_ffmpeg(['ffmpeg', '-v', 'error', '-i', str(source), '-c:v', 'libx264',
                   '-g', '100', '-bf', '3', '-pix_fmt', 'yuv420p', str(bframes)]), check=True)
    bframes.replace(source)
    meta = cutting.probe_source(source)
    monkeypatch.setattr(cutting, 'read_session', lambda *args: {'metadata': meta})
    monkeypatch.setattr(cutting, 'session_path', lambda *args: tmp_path)
    monkeypatch.setattr(cutting, 'lock_root', lambda: tmp_path/'locks')
    for index in (0, 1, 99, 100, 101, 249, 250, 419):
        actual = cutting.frame_file('batch', 'session', index)
        reference = cutting.run(['ffmpeg', '-v', 'error', '-i', str(source), '-vf',
             f"select='eq(n,{index})',scale=w='min(540,iw)':h=-2", '-frames:v', '1',
             '-q:v', '2', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'])
        assert Image.open(actual).tobytes() == Image.open(io.BytesIO(reference)).tobytes(), (fps, index)
    cutting.frame_file('batch', 'session', 200)
    monkeypatch.setattr(cutting, 'run', lambda *args: pytest.fail('adjacent frame cache missed'))
    assert cutting.frame_file('batch', 'session', 201).is_file()


def checker_video(path, frames=30, audio=False, fps='30'):
    width, height = 160, 96
    raw = bytearray()
    for n in range(frames):
        for y in range(height):
            for x in range(width):
                bit = ((x // 8) + (y // 8) + (n % 2)) & 1
                raw.append(235 if bit else 16)
    cmd = ['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'gray',
           '-s', f'{width}x{height}', '-r', fps, '-i', 'pipe:0']
    if audio:
        cmd += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                '-t', str(float(Fraction(frames) / Fraction(fps))), '-c:a', 'aac']
    cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18', '-pix_fmt', 'yuv420p', str(path)]
    subprocess.run(bounded_ffmpeg(cmd), input=bytes(raw), capture_output=True, check=True)
    return path


def gray_frames(path, width=160, height=96):
    raw = subprocess.check_output(bounded_ffmpeg(['ffmpeg', '-v', 'error', '-i', str(path),
          '-f', 'rawvideo', '-pix_fmt', 'gray', '-']))
    size = width * height
    return [raw[i:i + size] for i in range(0, len(raw), size)]


def block_span(frame, x, y, width=160, size=16):
    vals = [frame[(y + dy) * width + x + dx] for dy in range(size) for dx in range(size)]
    return max(vals) - min(vals)


def cell_delta(frame, x, y, width=160):
    return abs(frame[y * width + x] - frame[y * width + x + 8])


def test_mosaic_rejects_bad_boxes_and_empty_edit():
    meta = {'width': 160, 'height': 96, 'frame_count': 30}
    box = {'start': 8, 'end': 16, 'x': 32, 'y': 16, 'w': 64, 'h': 48}
    assert cutting.normalize_mosaics([box], meta) == [box]
    assert cutting.normalize_mosaics([], meta) == []
    bad = [
        'bad',
        [{'start': 8, 'end': 8, 'x': 32, 'y': 16, 'w': 64, 'h': 48}],
        [{'start': True, 'end': 16, 'x': 32, 'y': 16, 'w': 64, 'h': 48}],
        [{'start': 8, 'end': 16, 'x': 1, 'y': 16, 'w': 64, 'h': 48}],
        [{'start': 8, 'end': 16, 'x': 32, 'y': 16, 'w': 8, 'h': 48}],
        [{'start': 8, 'end': 16, 'x': 128, 'y': 16, 'w': 64, 'h': 48}],
        [{'start': 0, 'end': 31, 'x': 32, 'y': 16, 'w': 64, 'h': 48}],
        [box] * 41,
    ]
    for item in bad:
        with pytest.raises(ValueError):
            cutting.normalize_mosaics(item, meta)
    with pytest.raises(ValueError, match='删除范围或打码'):
        cutting.render_video(Path('missing.mp4'), Path('out.mp4'), [], meta)


@pytest.mark.integration
def test_mosaic_pixelates_marked_box_without_removing_frames(tmp_path):
    source = checker_video(tmp_path / 'checker.mp4', audio=True)
    original = file_sha256(source)
    meta = cutting.probe_source(source)
    mosaics = [
        {'start': 8, 'end': 16, 'x': 32, 'y': 16, 'w': 64, 'h': 48},
        {'start': 8, 'end': 12, 'x': 112, 'y': 64, 'w': 32, 'h': 16},
    ]
    output = tmp_path / 'mosaic.mp4'
    result = cutting.render_video(source, output, [[0, 4]], meta, mosaics)
    assert result['frame_count'] == 26
    assert result['has_audio'] is True
    assert file_sha256(source) == original
    frames = gray_frames(output)
    # Original frames 4..29 remain. Mosaic used original indexes before the cut.
    for index, original_index in enumerate(range(4, 30)):
        frame = frames[index]
        assert cell_delta(frame, 8, 8) > 150
        if 8 <= original_index < 16:
            assert block_span(frame, 32, 16) < 8
        else:
            assert cell_delta(frame, 40, 24) > 150
        if 8 <= original_index < 12:
            assert block_span(frame, 112, 64) < 8
        else:
            assert cell_delta(frame, 120, 64) > 150
