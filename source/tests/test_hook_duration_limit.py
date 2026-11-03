"""Manual twenty-second Hooks stay compatible with both editor paths and rendering."""
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from exactcut import hook_splice, renderer
from exactcut.hook_options import HookOptions
from exactcut.intake import compile_composition, validate_draft
from exactcut.media import run_checked
from exactcut.util import load_config
from test_hook_splice import frame_hashes, timestamps


@pytest.mark.parametrize('milliseconds', [999, 20001])
def test_manual_hook_rejects_values_outside_supported_range(milliseconds):
    with pytest.raises(ValueError):
        HookOptions(duration_ms=milliseconds)


@pytest.mark.parametrize('milliseconds', [15000, 20000])
def test_draft_accepts_extended_hook_without_changing_timeline(milliseconds):
    draft = {'drama_id': '101619', 'business_date': '20261013', 'author': '测试',
             'rows': [{'request_id': 'Rlimit', 'hook_text': 'Look again!',
                       'timeline_text': 'EP1 00:00.000-00:26.000',
                       'hook_options': {'duration_ms': milliseconds, 'mode': 'none'}}]}
    manifest, errors = validate_draft(draft)
    assert not errors
    assert manifest.requests[0].hook_options.duration_ms == milliseconds
    assert manifest.requests[0].timeline[0].end_ms == 26000


def test_composition_preserves_twenty_seconds():
    row = {'composition': {'opening': {'text': 'Look again!', 'duration_s': 20},
                           'plots': [{'episode': '1', 'start': '0', 'end': '26'}]}}
    compile_composition(row)
    assert row['hook_options']['duration_ms'] == 20000
    assert row['composition']['opening']['duration_s'] == 20


def test_both_editor_duration_inputs_allow_twenty_seconds():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    function = next(line for line in source.splitlines() if line.startswith('function hookSettings('))
    script = """
const assert=require('node:assert/strict');
const hookOptions=r=>r.hook_options||{},esc=String;
const hookSelect=()=>'',composeSelect=()=>'',hookTypography=()=>'',hookWords=()=>'',hookPreviewMarkup=()=>'';
const hookStyleNames={},composeStyles={};
""" + function + """
for (const row of [{hook_options:{duration_ms:20000}}, {composition:{opening:{duration_s:20}}}]) {
 const html=hookSettings(row,0), input=html.match(/<input[^>]*type="number"[^>]*>/)[0];
 assert.match(input,/max="20"/); assert.match(input,/value="20"/);
}
"""
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


@pytest.mark.integration
def test_twenty_second_hook_preserves_frames_audio_and_fades_at_end(tmp_path, monkeypatch):
    source, output, png = (tmp_path / name for name in ('source.mp4', 'output.mp4', 'hook.png'))
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=108x192:r=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '26',
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-g', '60',
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)])
    Image.new('RGBA', (108, 192), (240, 0, 0, 255)).save(png)
    monkeypatch.setattr(renderer, 'presentation', lambda *a, **kw: (png, {}))
    events = []
    monkeypatch.setattr(hook_splice.diag, 'event', lambda name, **kw: events.append((name, kw)))
    render = {**load_config()['render'], 'width': 108, 'height': 192}
    result = renderer._apply_hook(source, 'Look again!', output, render, tmp_path,
        options=HookOptions(mode='none', position='top', duration_ms=20000), normalized_input=True)
    splice = next(kw for name, kw in events if name == 'render.hook_splice')
    assert splice['used']
    before, after = frame_hashes(source), frame_hashes(output)
    assert len(before) == len(after) == 780
    first = 780 - splice['copied_frames']
    assert first >= 600 and before[first:] == after[first:]
    assert frame_hashes(source, 'a:0') == frame_hashes(output, 'a:0')
    for index, frame in enumerate(timestamps(output)):
        assert abs(float(frame['best_effort_timestamp_time']) - index / 30) < .00001
    assert result['duration_ms'] == 20000 and result['fade_out_ms'] == 1000
    red = []
    for second in (18, 19.5, 20.5):
        image = tmp_path / f'{second}.png'
        run_checked(['ffmpeg', '-y', '-v', 'error', '-ss', str(second), '-i', str(output),
                     '-frames:v', '1', str(image)])
        with Image.open(image) as frame:
            red.append(frame.convert('RGB').getpixel((54, 96))[0])
    assert red[0] > 200 and 50 < red[1] < 190 and red[2] < 20
