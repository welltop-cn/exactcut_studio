"""Re-encode only the frames that change; stream-copy every other closed GOP.

Used by cutting.render_video for deletions (cut points) and for mosaic layers.
Re-encoded pieces use the full-encode x264 settings and must reproduce the
source SPS/PPS exactly; copied pieces must be the source packets byte for byte.
Every uncertainty raises; the caller then runs the unchanged full encode.
Ported from the perf/smart-cut and perf/mosaic-gop experiments.
"""
from __future__ import annotations

import json
import math
import re
from fractions import Fraction
from pathlib import Path

from . import diagnostics
from .hook_splice import _idr, _require, _timeline

PSNR_FLOOR = 35  # dB, re-encoded frames against their lossless reference
_COPY_FIELDS = ('codec_name', 'profile', 'level', 'width', 'height', 'pix_fmt', 'sample_aspect_ratio',
                'time_base', 'extradata')


def enabled(settings):
    return settings.get('segment_reencode') is True


def attempt(render, state):
    """Run ``render`` (which builds, muxes and validates). None means: use the full encode."""
    from .cutting import CutError
    from .processing_cancel import Cancelled
    from .stage_results import ResourceDeferred
    try:
        result = render()
    except (Cancelled, ResourceDeferred):
        raise
    except (ValueError, KeyError, StopIteration, OSError, RuntimeError, CutError) as exc:
        cause = exc.__cause__
        while cause is not None:
            if isinstance(cause, (Cancelled, ResourceDeferred)):
                raise cause
            cause = cause.__cause__
        diagnostics.event('cut.segments', used=False, reason=str(exc)[-500:], **state.get('counts', {}))
        return None
    diagnostics.event('cut.segments', used=True, reason='validated', **state.get('counts', {}))
    return result


def _probe(path, run):
    streams = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_streams',
                              '-show_data', '-of', 'json', str(path)], 60, light=True))['streams']
    return streams[0]


def packets(path, run, selector='v:0'):
    return json.loads(run(['ffprobe', '-v', 'error', '-select_streams', selector, '-show_packets',
                           '-show_data_hash', 'sha256', '-show_entries',
                           'packet=pts,dts,duration,pos,size,flags,data_hash', '-of', 'json', str(path)],
                          120, light=True))['packets']


def _seek(frame, rate):
    # Half a frame early: accurate seek then lands exactly on ``frame``.
    return ['-ss', f'{float(Fraction(2*frame-1, 2)/rate):.9f}'] if frame else []


def _copy_seek(frame, rate):
    if not frame:return []
    micros = math.ceil(Fraction(frame, 1)/rate*1000000)
    return ['-ss', f'{micros//1000000}.{micros%1000000:06d}']


def safe_keys(source, video_packets, tick, rate):
    """Source frames that start a closed GOP in decode order (IDR, nothing earlier shown later)."""
    pts = [int(p['pts']) for p in video_packets]
    suffix = [0]*len(pts);minimum = max(pts)
    for i in range(len(pts)-1, -1, -1):
        minimum = min(minimum, pts[i]);suffix[i] = minimum
    keys = {};maximum = min(pts)-1
    for i, packet in enumerate(video_packets):
        if 'K' in packet['flags']:
            _require(_idr(source, packet, 4), 'non-IDR keyframe / open GOP')
            if maximum < pts[i] and suffix[i] >= pts[i]:
                frame = Fraction(pts[i])*tick*rate
                _require(frame.denominator == 1 and int(frame) == i, 'IDR frame boundary differs')
                keys[int(frame)] = i
        maximum = max(maximum, pts[i])
    _require(0 in keys, 'no safe initial IDR')
    keys[len(video_packets)] = len(video_packets)
    return keys


def plan(kept, keys, active, count):
    """Pieces (kind, start, end) in source frames, in output order.

    ``active``: source intervals whose pixels change (mosaic). Encoded pieces cover
    each cut point up to the neighbouring safe key and each active interval widened
    to safe keys; everything else is copied whole-GOP.
    """
    ordered = sorted(keys)
    pieces = []
    for a, b in kept:
        need = []
        first = next(k for k in ordered if k >= a)
        if a < first:need.append([a, min(first, b)])
        if b not in keys:need.append([max(a, max(k for k in ordered if k <= b)), b])
        for s, e in active:
            s, e = max(s, a), min(e, b)
            if s >= e:continue
            need.append([max(a, max(k for k in ordered if k <= s)), min(b, next(k for k in ordered if k >= e))])
        merged = []
        for s, e in sorted(need):
            if merged and s <= merged[-1][1]:merged[-1][1] = max(merged[-1][1], e)
            else:merged.append([s, e])
        cursor = a
        for s, e in merged:
            if cursor < s:pieces.append(('copy', cursor, s))
            pieces.append(('encode', s, e));cursor = e
        if cursor < b:pieces.append(('copy', cursor, b))
    for kind, s, e in pieces:
        assert s < e and (kind == 'encode' or (s in keys and e in keys)), (kind, s, e)
    return pieces


def _shift(intervals, start, end):
    return [[max(s, start)-start, min(e, end)-start] for s, e in intervals if s < end and e > start]


def build(source, output, kept, meta, render, work, state, run, layers=None):
    """Write the video-only stream to ``output``. ``layers`` describes mosaic effects:
    {'appearances': [...], 'intervals': [[[s,e],...] per layer], 'masks': callable(start,end,dir)->[paths]}."""
    from . import mosaic_effects as effects
    rate = Fraction(meta['fps_num'], meta['fps_den']);fps = f'{rate.numerator}/{rate.denominator}'
    video = _probe(source, run)
    _require(video['codec_name'] == 'h264' and video['codec_tag_string'] == 'avc1'
             and video['is_avc'] == 'true' and int(video['nal_length_size']) == 4
             and video['pix_fmt'] == 'yuv420p' and video.get('field_order') == 'progressive'
             and int(video['has_b_frames']) <= 2 and bool(video.get('extradata'))
             and Fraction(video['avg_frame_rate']) == rate and Fraction(video['r_frame_rate']) == rate,
             'unsupported source codec')
    tick = Fraction(video['time_base'])
    encoder = ['-c:v', 'libx264', '-crf', str(render['crf']), '-preset', render['preset'], '-pix_fmt', 'yuv420p', '-r', fps]
    colors = []
    for field, option in (('color_primaries', '-color_primaries'), ('color_transfer', '-color_trc'),
                          ('color_space', '-colorspace'), ('color_range', '-color_range')):
        if video.get(field) not in (None, 'unknown', 'unspecified', 'reserved'):colors += [option, str(video[field])]
    # SPS/PPS depend on settings and size, not content: one frame tells whether
    # our encoder can continue this stream before any real piece is encoded.
    trial = work/'segment-trial.mp4'
    run(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-map', '0:v:0', '-an', '-frames:v', '1', *encoder, *colors, str(trial)], light=True)
    trial_video = _probe(trial, run)
    _require(all(trial_video.get(k) == video.get(k) for k in _COPY_FIELDS), 'encoder codec parameters differ from source')
    source_packets = packets(source, run)
    _timeline(source_packets, tick, rate, meta['frame_count'])
    keys = safe_keys(source, source_packets, tick, rate)
    active = sorted(span for group in (layers or {}).get('intervals', []) for span in group)
    pieces = plan(kept, keys, active, meta['frame_count'])
    state['counts'] = dict(encoded_frames=sum(e-s for k, s, e in pieces if k == 'encode'),
                           copied_frames=sum(e-s for k, s, e in pieces if k == 'copy'), pieces=len(pieces))
    state['copied'], state['encoded'] = [], []
    parts, cursor = [], 0
    for index, (kind, s, e) in enumerate(pieces):
        part = work/f'segment-{index}.mp4';n = e-s
        if kind == 'encode':
            masks, graph = [], None
            shifted = [_shift(group, s, e) for group in (layers or {}).get('intervals', [])]
            if layers and any(shifted):
                masks = layers['masks'](s, e, work/f'segment-{index}')
                # Zero-based span frames line up with the zero-based mask inputs.
                graph = '[0:v]setpts=PTS-STARTPTS[span];'+effects.layered_graph(
                    meta, layers['appearances'], shifted).replace('[0:v]', '[span]', 1)
            filters = ['-filter_complex', graph, '-map', '[out]'] if graph else ['-map', '0:v:0', '-vf', 'setpts=PTS-STARTPTS']
            run(['ffmpeg', '-y', '-v', 'error', *_seek(s, rate), '-i', str(source),
                 *[arg for mask in masks for arg in ('-i', str(mask))], '-an', *filters, '-frames:v', str(n),
                 *encoder, *colors, str(part)])
            encoded = _probe(part, run)
            _require(all(encoded.get(k) == video.get(k) for k in _COPY_FIELDS), 'encoded piece codec parameters differ')
            samples = packets(part, run)
            _timeline(samples, tick, rate, n)
            _require('K' in samples[0]['flags'] and _idr(part, samples[0], 4), 'encoded piece does not start with IDR')
            state['encoded'].append({'output': cursor, 'source': s, 'frames': n, 'masks': masks, 'graph': graph})
        else:
            run(['ffmpeg', '-y', '-v', 'error', *_copy_seek(s, rate), '-i', str(source),
                 '-map', '0:v:0', '-c:v', 'copy', '-an', '-frames:v', str(n), str(part)], light=True)
            samples = packets(part, run)
            original = source_packets[keys[s]:keys[e]]
            _timeline(samples, tick, rate, n)
            _require([p['data_hash'] for p in samples] == [p['data_hash'] for p in original], 'copy payload differs')
            _require([int(p['pts']) for p in samples] == [int(p['pts'])-int(original[0]['pts']) for p in original],
                     'copy presentation timing differs')
            state['copied'].append((cursor, cursor+n))
        parts.append((part, n));cursor += n
    listing = work/'segments.ffconcat'
    quote = lambda path: str(path.resolve()).replace("'", "'\\''")
    listing.write_text('ffconcat version 1.0\n'+''.join(
        f"file '{quote(part)}'\nduration {float(Fraction(n, 1)/rate):.12f}\n" for part, n in parts))
    command = ['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing), '-map', '0:v:0', '-c', 'copy']
    run(command+['-movflags', '+faststart', str(output)], light=True)
    def decode_order_ok(samples):
        frame_ticks = 1/(rate*tick)
        _require(frame_ticks.denominator == 1, 'frame duration is not an integer number of ticks')
        return (all(int(p['dts']) <= int(p['pts']) for p in samples)
                and all(int(x['dts']) < int(y['dts']) for x, y in zip(samples, samples[1:]))
                and all(int(y['dts'])-int(x['dts']) == frame_ticks for x, y in zip(samples, samples[1:])))
    joined = packets(output, run)
    if not decode_order_ok(joined):
        run(command+['-bsf:v', f'setts=pts=PTS:dts=(N-2)/({fps}*TB)', '-movflags', '+faststart', str(output)], light=True)
        joined = packets(output, run)
    _timeline(joined, tick, rate, cursor)
    _require(decode_order_ok(joined), 'joined decode timestamps differ')


def _psnr(run, inputs, graph, stats, count):
    run(['ffmpeg', '-v', 'error', '-xerror', *inputs, '-filter_complex', graph, '-map', '[quality]',
         '-frames:v', str(count), '-an', '-f', 'null', '-'], 900)
    lines = stats.read_text().splitlines()
    _require(len(lines) == count, 'PSNR frame count differs')
    values = []
    for index, line in enumerate(lines):
        fields = dict(re.findall(r'(\w+):(\S+)', line))
        _require(int(fields['n']) == index+1, 'PSNR frame sequence differs')
        value = float(fields['psnr_avg'])
        _require(not math.isnan(value), 'invalid PSNR')
        values.append(value)
    return values


def _escape(path):
    return str(path).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")


def validate(final, source, kept, meta, work, state, run):
    """One -xerror decode of the whole result against the kept source frames:
    exact frame count, copied frames bit-identical, re-encoded frames >= PSNR_FLOOR.
    Mosaic pieces are compared with the same graph applied losslessly to the source."""
    rate = Fraction(meta['fps_num'], meta['fps_den']);fps = f'{rate.numerator}/{rate.denominator}'
    expected = sum(b-a for a, b in kept)
    predicate = '+'.join(f'gte(n,{a})*lt(n,{b})' for a, b in kept)
    stats = work/'segments-psnr.log'
    graph = (f"[1:v]select='{predicate}',setpts=N/({fps}*TB)[r];[0:v]setpts=N/({fps}*TB)[o];"
             f"[o][r]psnr=stats_file='{_escape(stats)}'[quality]")
    values = _psnr(run, ['-i', str(final), '-i', str(source)], graph, stats, expected)
    effect = set()
    for piece in state['encoded']:
        if piece['graph']:effect.update(range(piece['output'], piece['output']+piece['frames']))
    for index, value in enumerate(values):
        if any(a <= index < b for a, b in state['copied']):
            _require(value == math.inf, f'copied frame differs at frame {index}')
        elif index not in effect:
            _require(value >= PSNR_FLOOR, f'PSNR below {PSNR_FLOOR} dB at frame {index}')
    for number, piece in enumerate(p for p in state['encoded'] if p['graph']):
        stats = work/f'segments-effect-{number}.psnr'
        reference = re.sub(r'\[(\d+):v\]', lambda m: f'[{int(m[1])+1}:v]', piece['graph']).replace('[out]', '[reference]')
        reference += (f';[0:v]setpts=PTS-STARTPTS,setpts=N/({fps}*TB)[o];[reference]setpts=N/({fps}*TB)[r];'
                      f"[o][r]psnr=stats_file='{_escape(stats)}'[quality]")
        inputs = [*_seek(piece['output'], rate), '-i', str(final), *_seek(piece['source'], rate), '-i', str(source),
                  *[arg for mask in piece['masks'] for arg in ('-i', str(mask))]]
        values = _psnr(run, inputs, reference, stats, piece['frames'])
        _require(all(value >= PSNR_FLOOR for value in values), 'mosaic piece PSNR below floor')
