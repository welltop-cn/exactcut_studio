"""Real local media through frame draft, managed child, cut/Hook/ending and QC."""
from copy import deepcopy
import json
import subprocess
import uuid

import pytest

from exactcut import workflow as w, material_versions as versions, material_workers
from exactcut import source_frames, renderer, qc
from exactcut.util import load_config, read_json, write_json_atomic
from test_source_frames import source
from test_web_base_path import app
from test_source_frame_timecode import frame_clip

pytestmark = pytest.mark.integration


def run(*args):
    return subprocess.run(['ffmpeg', '-v', 'error', *args], check=True, capture_output=True).stdout


@pytest.mark.parametrize('pasted', [False, True], ids=['manual', 'pasted-text'])
def test_managed_frame_draft_renders_confirmed_milliseconds_with_hook_ending_and_audio(source, monkeypatch, pasted):
    _, _, _, video = source
    monkeypatch.setenv('EXACTCUT_ALLOW_AI', '0')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    monkeypatch.setenv('EXACTCUT_ALLOW_PUBLISH', '0')
    run('-y', '-f', 'lavfi', '-i', 'testsrc2=size=180x320:rate=30000/1001:duration=4',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
        '-c:v', 'libx264', '-g', '30', '-bf', '3', '-c:a', 'aac', '-ac', '2', str(video))
    item = w.create_blank(time_format='source_frames_v1')
    parent = w.folder(item['id'])
    draft = deepcopy(item['draft'])
    draft.update(drama_id='101573', author='frame-test')
    row = draft['rows'][0]
    row.update(clips=[frame_clip(start_f='019', end_s='02', end_f='012')], hook_text='LOOK!',
               hook_options={'mode': 'manual', 'manual_ranges': [{'start': 0, 'end': 4}],
                             'style': 'default', 'position': 'top', 'duration_ms': 1000},
               ending={'mode': 'last_frame', 'duration_ms': 1000})
    if pasted:
        from exactcut import llm_intake
        def interpret(payload, **kwargs):
            return kwargs['schema'].model_validate({'items': [{
                'source_lines': [1], 'issue': '', 'clips': [{
                    'episode': 1, 'start_m': 0, 'start_s': 0, 'start_f': 19,
                    'end_m': 0, 'end_s': 2, 'end_f': 12}]}]})
        monkeypatch.setattr(llm_intake, 'interpret', interpret)
        recognized = llm_intake.read_clip_text('EP1 00:00:019-00:02:012', time_format='source_frames_v1')
        assert not recognized['errors'], recognized
        row['clips'] = recognized['clips']
    index = source_frames.prepare(parent, draft, 'EP1')
    saved = w.save_draft(parent.name, {'revision': item['revision'], 'draft': draft})
    assert saved['draft']['rows'][0]['clips'][0]['start_f'] == '019'
    versions.enroll(parent)
    state = versions.read(parent)
    confirmed = versions.confirm(parent, {'revision': state['revision'], 'mutation_id': uuid.uuid4().hex})
    event = next(iter(confirmed['outbox'].values()))
    child = material_workers.materialize(parent, event)
    frozen = read_json(child / 'confirmed.json')
    segment = frozen['requests'][0]['timeline'][0]
    assert frozen['schema_version'] == 1
    assert (segment['start_ms'], segment['end_ms']) == (index['times_ms'][19], index['times_ms'][73])
    assert 'start_f' not in json.dumps(frozen)
    assert not (child / 'source-picker').exists()
    child_doc = read_json(child / 'draft.json')
    child_doc['draft']['rows'][0]['clips'][0]['start_f'] = '999'
    child_doc['draft']['rows'][0]['timeline_text'] = 'invalid editable draft'
    write_json_atomic(child / 'draft.json', child_doc)
    manifest, request = w.request_for(child, row['request_id'])
    assert manifest.model_dump(mode='json') == frozen
    config = load_config()
    config['render'].update(width=180, height=320, fps=30000 / 1001,
                            preset='ultrafast', crf=0)
    project = video.parents[2]
    output = child / 'production/videos' / request.output_filename
    timeline = [item.model_dump(mode='json') for item in request.timeline]
    plan = renderer.render_request(project, request, timeline, output, config)
    checked = qc.inspect_output(output, plan['expected_duration_ms'], config, timeline)
    assert checked['allowed'], checked
    assert plan['hook_visual']['duration_ms'] == 1000
    assert plan['ending_visual']['mode'] == 'last_frame'
    assert plan['ending_visual']['start_ms'] == segment['end_ms'] - segment['start_ms']
    assert abs(checked['actual_duration_ms'] - plan['expected_duration_ms']) <= 50
    base = project / 'cache/render-base' / (plan['base_cache_key'] + '.mp4')
    def pixels(path, selection):
        return run('-i', str(path), '-vf', selection, '-fps_mode', 'passthrough',
                   '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-')
    # The complete lossless base cut proves first/last frame and inclusive-end mapping.
    expected = pixels(video, r'select=between(n\,19\,72)')
    assert pixels(base, 'null') == expected
    frame_bytes = 180 * 320 * 3 // 2
    final_frames = pixels(output, 'null')
    assert len(final_frames) % frame_bytes == 0
    assert len(final_frames) // frame_bytes == 54 + 30
    assert final_frames[:frame_bytes] != expected[:frame_bytes]  # Actual Hook changed pixels.
    # Overlay uses RGB internally even after its visible interval has finished.
    expected_hook_tail = pixels(video, r'select=eq(n\,72),format=rgba,format=yuv420p')
    assert final_frames[53 * frame_bytes:54 * frame_bytes] == expected_hook_tail

    # Ending makes a PNG still; account for that documented RGB/YUV round trip.
    reference_still = parent / 'selected-last-frame.png'
    reference_still.write_bytes(run('-i', str(video), '-vf', r'select=eq(n\,72)',
                                   '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'))
    assert final_frames[-frame_bytes:] == pixels(reference_still, 'null')
    probe = renderer.ffprobe(output)
    streams = {stream['codec_type']: stream for stream in probe['streams']}
    assert streams['audio']['channels'] == 2
    assert abs(float(streams['audio']['duration']) - float(streams['video']['duration'])) <= .05
    story_audio = run('-ss', '0.2', '-i', str(output), '-t', '0.5', '-f', 's16le', '-acodec', 'pcm_s16le', '-')
    assert story_audio and any(story_audio)
    ending_audio = run('-ss', str(plan['ending_visual']['start_ms'] / 1000 + .2), '-i', str(output),
                       '-t', '0.5', '-f', 's16le', '-acodec', 'pcm_s16le', '-')
    assert ending_audio and set(ending_audio) == {0}
    run('-i', str(output), '-f', 'null', '-')  # Decode both streams all the way through.
