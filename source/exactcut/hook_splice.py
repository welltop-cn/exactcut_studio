"""Encode only the visible Hook prefix of an internally normalized H.264 base.

A demuxer keyframe flag alone does not prove that the following GOP is closed.
Inspect the actual AVCC sample for an IDR slice before independently copying it.
Every uncertainty leaves the caller free to use its original full encode.
"""
from __future__ import annotations

import json
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import diagnostics as diag
from .media import ffprobe, run_checked


class _Unsupported(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Unsupported(reason)


def _packets(path: Path, selector: str = 'v:0') -> list[dict]:
    return json.loads(run_checked([
        'ffprobe', '-v', 'error', '-select_streams', selector, '-show_packets',
        '-show_data_hash', 'sha256', '-show_entries',
        'packet=pts,dts,duration,pos,size,flags,data_hash', '-of', 'json', str(path),
    ]).stdout)['packets']


def _idr(path: Path, packet: dict, length_size: int) -> bool:
    with path.open('rb') as source:
        source.seek(int(packet['pos']))
        data = source.read(int(packet['size']))
    offset = 0
    slices = []
    while offset + length_size < len(data):
        size = int.from_bytes(data[offset:offset + length_size], 'big')
        offset += length_size
        if size <= 0 or offset + size > len(data):
            return False
        kind = data[offset] & 31
        if kind in (1, 2, 3, 4, 5):
            slices.append(kind)
        offset += size
    return offset == len(data) and bool(slices) and all(kind == 5 for kind in slices)


def _timeline(packets: list[dict], tick: Fraction, fps: Fraction, count: int) -> None:
    _require(len(packets) == count, 'packet/frame count differs')
    _require(sorted(Fraction(p['pts']) * tick * fps for p in packets) == list(range(count)),
             'video is not a zero-based constant frame-rate timeline')
    _require(all(Fraction(p['duration']) * tick * fps == 1 for p in packets),
             'video packet duration differs from one frame')


def _boundary_hashes(path: Path, first: int) -> list[str]:
    result = run_checked(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path),
                          '-map', '0:v:0', '-vf', f'trim=start_frame={first},setpts=PTS-STARTPTS',
                          '-frames:v', '8', '-vsync', '0', '-f', 'framemd5', '-'])
    return [line.split(',')[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith('#')]


def try_hook_splice(input_video: Path, output: Path, render: dict[str, Any],
                    end_s: float, command: list[str]) -> bool:
    """Try a validated splice. ``command`` is the unchanged full-encode fallback."""
    try:
        streams = ffprobe(input_video)['streams']
        video = next(s for s in streams if s['codec_type'] == 'video')
        fps = Fraction(str(render['fps']))
        tick = Fraction(video['time_base'])
        count = int(video['nb_frames'])
        _require(render['video_codec'] == 'libx264' and video['codec_name'] == 'h264'
                 and video['codec_tag_string'] == 'avc1' and video['is_avc'] == 'true'
                 and int(video['nal_length_size']) == 4 and video['pix_fmt'] == 'yuv420p'
                 and video['width'] == int(render['width']) and video['height'] == int(render['height'])
                 and video['field_order'] == 'progressive' and int(video['has_b_frames']) <= 2
                 and Fraction(video['r_frame_rate']) == fps and Fraction(video['avg_frame_rate']) == fps,
                 'unsupported normalized video format')
        _require(count / fps >= 10 and end_s + 2 < count / fps, 'short video or long Hook')
        packets = _packets(input_video)
        _timeline(packets, tick, fps, count)
        boundary = None
        for index, packet in enumerate(packets):
            seconds = Fraction(packet['pts']) * tick
            if (end_s <= seconds <= end_s + 10 and seconds + 2 < count / fps
                    and 'K' in packet['flags'] and _idr(input_video, packet, 4)):
                # IDR presentation order must divide all preceding and following samples.
                if (all(p['pts'] < packet['pts'] for p in packets[:index])
                        and all(p['pts'] >= packet['pts'] for p in packets[index:])):
                    boundary = index, seconds
                    break
        _require(boundary is not None, 'no safe IDR close to Hook end')
        index, seconds = boundary
        prefix_frames = int(seconds * fps)
        _require(prefix_frames == index, 'IDR does not lie on a frame boundary')
        with tempfile.TemporaryDirectory(prefix='hook-splice-', dir=output.parent) as directory:
            root = Path(directory)
            prefix, tail, candidate = (root / name for name in ('prefix.mp4', 'tail.mp4', 'hook.mp4'))
            # Keep the established overlay/fade and medium/CRF settings verbatim.
            encode = list(command)
            audio_map = encode.index('0:a:0?')
            del encode[audio_map - 1:audio_map + 1]
            audio_codec = encode.index('-c:a')
            del encode[audio_codec:audio_codec + 2]
            duration = len(encode) - 1 - encode[::-1].index('-t')
            encode[duration:duration + 2] = ['-frames:v', str(prefix_frames)]
            encode[-1] = str(prefix)
            run_checked(encode)
            encoded = next(s for s in ffprobe(prefix)['streams'] if s['codec_type'] == 'video')
            fields = ('codec_name', 'profile', 'level', 'width', 'height', 'pix_fmt',
                      'time_base', 'r_frame_rate', 'sample_aspect_ratio')
            _require(all(encoded.get(key) == video.get(key) for key in fields),
                     'encoded prefix codec parameters differ')
            _timeline(_packets(prefix), tick, fps, prefix_frames)
            # FFmpeg seeks in microseconds. Round upward so a fractional-frame IDR
            # cannot accidentally seek to the preceding GOP. Verify all packets below.
            microseconds = -(-(seconds * 1000000).numerator // (seconds * 1000000).denominator)
            seek = f'{microseconds // 1000000}.{microseconds % 1000000:06d}'
            run_checked(['ffmpeg', '-y', '-v', 'error', '-ss', seek, '-i', str(input_video),
                         '-map', '0:v:0', '-c:v', 'copy', '-an', str(tail)])
            copied = _packets(tail)
            _timeline(copied, tick, fps, count - prefix_frames)
            _require([p['data_hash'] for p in copied] == [p['data_hash'] for p in packets[index:]],
                     'tail seek changed packet selection or payload')
            _require([p['pts'] for p in copied] == [p['pts'] - packets[index]['pts'] for p in packets[index:]],
                     'tail seek changed presentation timing')
            listing = root / 'video.ffconcat'
            quote = lambda path: str(path.resolve()).replace("'", "'\\''")
            listing.write_text('ffconcat version 1.0\n'
                               f"file '{quote(prefix)}'\nduration {float(seconds):.12f}\n"
                               f"file '{quote(tail)}'\n")
            run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing),
                         '-i', str(input_video), '-map', '0:v:0', '-map', '1:a:0?', '-c', 'copy',
                         '-bsf:v', f'setts=pts=PTS:dts=(N-2)/({render["fps"]}*TB)',
                         '-movflags', '+faststart', str(candidate)])
            joined_packets = _packets(candidate)
            _timeline(joined_packets, tick, fps, count)
            _require(all(p['dts'] <= p['pts'] for p in joined_packets)
                     and all(Fraction(b['dts'] - a['dts']) * tick * fps == 1
                             for a, b in zip(joined_packets, joined_packets[1:])),
                     'joined decode timestamps are not continuous')
            # Concat auto-converts AVC to Annex B, inserting each input's SPS/PPS.
            # Prove the decoder actually switches to the tail's parameter sets.
            _require(_boundary_hashes(candidate, prefix_frames) == _boundary_hashes(tail, 0),
                     'decoded boundary differs from copied tail')
            def audio_identity(path):
                return [(p['pts'], p['dts'], p['duration'], p['data_hash']) for p in _packets(path, 'a:0')]
            _require(audio_identity(candidate) == audio_identity(input_video), 'audio packets or timing changed')
            candidate.replace(output)
        diag.event('render.hook_splice', used=True, encoded_prefix_seconds=float(seconds),
                   copied_frames=count - prefix_frames, total_frames=count)
        return True
    except (KeyError, ValueError, StopIteration, OSError, RuntimeError) as exc:
        diag.event('render.hook_splice', used=False, reason=str(exc)[-500:])
        return False
