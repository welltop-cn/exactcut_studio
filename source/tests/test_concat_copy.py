"""Normalized cuts should join without another lossy video encode."""
import json
import shutil

import pytest

from exactcut.media import run_checked, ffprobe
from exactcut.renderer import _concat_units
from exactcut.util import load_config

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')


def frame_hashes(path):
    result = run_checked(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                          '-vsync', '0', '-f', 'framemd5', '-'])
    return [line.split(',')[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith('#')]


@pytest.mark.parametrize('frames', [(31, 47, 62, 37), (1, 2, 3, 4)])
def test_join_preserves_every_decoded_frame_and_timing(tmp_path, frames):
    render = load_config()['render']
    render.update(width=108, height=192)
    units = []
    for i, count in enumerate(frames):
        unit = tmp_path / f"clip {i}'s.mp4"
        run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                     f'testsrc2=s=108x192:r=30:d={count/30}', '-f', 'lavfi', '-i',
                     f'sine=frequency={440+i*100}:sample_rate=44100',
                     '-t', str(count/30), '-c:v', 'libx264', '-preset', 'medium',
                     '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2', str(unit)])
        units.append(unit)
    expected = [frame for unit in units for frame in frame_hashes(unit)]
    output = tmp_path / 'joined.mp4'
    _concat_units(units, output, render, round(sum(frames)*1000/30))
    assert frame_hashes(output) == expected  # No second lossy encode or dropped/duplicated frame.
    streams = ffprobe(output)['streams']
    for stream in streams:
        assert abs(float(stream['duration'])-sum(frames)/30) < .05
    video = next(s for s in streams if s['codec_type']=='video')
    assert video['r_frame_rate'] == '30/1'
    timestamps = json.loads(run_checked(['ffprobe', '-v', 'error', '-select_streams', 'v',
        '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(output)]).stdout)
    for index, frame in enumerate(timestamps['frames']):
        assert abs(float(frame['best_effort_timestamp_time'])-index/30) < .001
