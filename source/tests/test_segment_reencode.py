"""Only changed GOPs are re-encoded; copied GOPs are the source frames; any doubt uses the full encode."""
import math
import re

import pytest

from exactcut import cutting, mosaic_render, segment_reencode as segments


def test_plan_encodes_cut_points_and_widened_effects_and_copies_whole_gops():
    keys = {0: 0, 30: 30, 60: 60, 90: 90, 120: 120}  # 120 = end sentinel
    # Deletion [35,40) and [100,120): cut points at 35, 40 and 100.
    assert segments.plan([[0, 35], [40, 100]], keys, [], 120) == [
        ('copy', 0, 30), ('encode', 30, 35), ('encode', 40, 60), ('copy', 60, 90), ('encode', 90, 100)]
    # An effect inside a GOP widens to its safe keys; untouched GOPs stay copies.
    assert segments.plan([[0, 120]], keys, [[65, 70]], 120) == [('copy', 0, 60), ('encode', 60, 90), ('copy', 90, 120)]
    # Effect next to a cut point merges into one encoded piece.
    assert segments.plan([[0, 70]], keys, [[62, 64]], 120) == [('copy', 0, 60), ('encode', 60, 70)]




@pytest.fixture
def events(monkeypatch):
    events = []
    monkeypatch.setattr(cutting.diagnostics, 'event', lambda name, **values: events.append((name, values)))
    return events


def make_source(path, *extra):
    cutting.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=44100', '-t', '6',
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-g', '30', '-keyint_min', '30',
                 '-sc_threshold', '0', '-bf', '3', '-pix_fmt', 'yuv420p', '-c:a', 'aac', *extra, str(path)])
    return path


def frame_hashes(path):
    raw = cutting.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-map', '0:v:0', '-f', 'framemd5', '-']).decode()
    return [line.split(',')[-1].strip() for line in raw.splitlines() if line and not line.startswith('#')]


def psnr(a, b):
    raw = cutting.run(['ffmpeg', '-v', 'info', '-i', str(a), '-i', str(b), '-lavfi', '[0:v][1:v]psnr=stats_file=-', '-f', 'null', '-']).decode()
    return [float(v) for v in re.findall(r'psnr_avg:(\S+)', raw)]


def used(events):
    return [v for n, v in events if n == 'cut.segments'][-1]


def disabled(monkeypatch):
    config = cutting.load_config();config['cutting']['segment_reencode'] = False
    monkeypatch.setattr(cutting, 'load_config', lambda: config)


@pytest.mark.integration
@pytest.mark.parametrize('deleted', [[[35, 40]], [[25, 95]], [[0, 32], [42, 180]], [[0, 35]], [[145, 180]], [[30, 60]]])
def test_cut_copies_source_gops_and_matches_full_encode(tmp_path, events, monkeypatch, deleted):
    source = make_source(tmp_path/'source.mp4');meta = cutting.probe_source(source)
    result = cutting.render_video(source, tmp_path/'smart.mp4', deleted, meta)
    event = used(events);assert event['used'], event
    kept = [n for a, b in cutting.keep_ranges(deleted, 180) for n in range(a, b)]
    assert result['frame_count'] == len(kept) == event['encoded_frames']+event['copied_frames']
    original, actual = frame_hashes(source), frame_hashes(tmp_path/'smart.mp4')
    identical = sum(actual[i] == original[n] for i, n in enumerate(kept))
    assert identical >= event['copied_frames']
    disabled(monkeypatch)
    cutting.render_video(source, tmp_path/'full.mp4', deleted, meta)
    assert min(psnr(tmp_path/'smart.mp4', tmp_path/'full.mp4')) >= 35
    audio = lambda p: next(s for s in cutting.ffprobe(p)['streams'] if s['codec_type'] == 'audio')
    assert abs(float(audio(tmp_path/'smart.mp4')['duration'])-float(audio(tmp_path/'full.mp4')['duration'])) < 1/30


@pytest.mark.integration
@pytest.mark.parametrize('tail_frames', [1, 2])
def test_short_encoded_tail_keeps_constant_frame_rate(tmp_path, events, tail_frames):
    source = make_source(tmp_path/'source.mp4');meta = cutting.probe_source(source)
    output = tmp_path/'smart.mp4'
    result = cutting.render_video(source, output, [[150+tail_frames, 180]], meta)
    event = used(events)
    assert event['used'] is True, event
    assert event['encoded_frames'] == tail_frames
    assert result['frame_count'] == 150+tail_frames
    assert frame_hashes(output)[:150] == frame_hashes(source)[:150]
    video = next(s for s in cutting.ffprobe(output)['streams'] if s['codec_type'] == 'video')
    assert video['avg_frame_rate'] == '30/1'


def tracks(*spans):
    styles = ('pixel', 'blur')
    return [{'target': {'start': s, 'end': e, 'seed_frame': s, 'x': 16+40*i, 'y': 16, 'w': 48, 'h': 40, 'motion': 'fixed',
                        'effect': {'style': styles[i % 2], 'strength': 'medium', 'expansion': 'none'}},
             'result': {'width': 160, 'height': 96}} for i, (s, e) in enumerate(spans)]


@pytest.mark.integration
@pytest.mark.parametrize('ranges', [[], [[10, 20]], [[100, 130]]])
def test_mosaic_encodes_only_masked_gops_and_matches_full_path(tmp_path, events, monkeypatch, ranges):
    source = make_source(tmp_path/'source.mp4');meta = cutting.probe_source(source)
    regions = tracks((45, 70), (150, 160))
    smart = mosaic_render.process_video(source, tmp_path/'smart.mp4', meta, regions, ranges)
    event = used(events);assert event['used'], event
    assert event['copied_frames'] > 0
    disabled(monkeypatch)
    full = mosaic_render.process_video(source, tmp_path/'full.mp4', meta, regions, ranges)
    assert smart['metadata'] == full['metadata']
    quality = psnr(tmp_path/'smart.mp4', tmp_path/'full.mp4')
    assert len(quality) == full['metadata']['frame_count'] and min(quality) >= 35
    kept = [n for a, b in cutting.keep_ranges(ranges, 180) for n in range(a, b)] if ranges else list(range(180))
    original, actual = frame_hashes(source), frame_hashes(tmp_path/'smart.mp4')
    for i, n in enumerate(kept):
        if not (30 <= n < 90 or 150 <= n < 180 or any(abs(n-a) < 30 for r in ranges for a in r)):
            assert actual[i] == original[n], f'frame {n} outside effects and cut GOPs must be the source frame'
    if not ranges:
        audio = lambda p: [x['data_hash'] for x in segments.packets(p, cutting.run, 'a:0')]
        assert audio(tmp_path/'smart.mp4') == audio(source)


@pytest.mark.integration
def test_source_with_other_encoder_settings_falls_back_to_full_encode(tmp_path, events):
    source = make_source(tmp_path/'source.mp4', '-profile:v', 'baseline')
    meta = cutting.probe_source(source)
    result = cutting.render_video(source, tmp_path/'out.mp4', [[35, 40]], meta)
    event = used(events)
    assert not event['used'] and 'differ' in event['reason']
    assert result['frame_count'] == 175


@pytest.mark.integration
def test_failed_validation_falls_back_to_full_encode(tmp_path, events, monkeypatch):
    source = make_source(tmp_path/'source.mp4');meta = cutting.probe_source(source)
    monkeypatch.setattr(segments, 'PSNR_FLOOR', math.inf)
    mosaic_render.process_video(source, tmp_path/'out.mp4', meta, tracks((45, 70)), [[100, 130]])
    event = used(events)
    assert not event['used'] and 'PSNR below' in event['reason']
    assert cutting.probe_source(tmp_path/'out.mp4')['frame_count'] == 150


@pytest.mark.integration
def test_packet_probe_rejects_variable_frame_rate(tmp_path):
    source = tmp_path/'vfr.mp4'
    cutting.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=30', '-t', '3',
                 '-vf', "setpts='if(gte(N,30),PTS+0.5/TB,PTS)'", '-fps_mode', 'passthrough', '-c:v', 'libx264', str(source)])
    with pytest.raises(cutting.CutError, match='可变帧率|不连续'):
        cutting.probe_source(source)
