import copy
import io
import subprocess

import pytest
from PIL import Image, ImageChops, ImageStat

from exactcut import renderer
from exactcut.intake import validate_draft, timing_explanation, compile_composition
from exactcut.models import CutRequest
from exactcut.media import duration_ms
from exactcut.util import load_config
from browser_support import shared_playwright


def request(**kwargs):
    return CutRequest(request_id='R1', seq=1, output_filename='test.mp4',
                      timeline=[dict(type='title_card', text='Story', duration_ms=1000)], **kwargs)


def test_ending_contract_defaults_and_rejects_invalid_inputs():
    assert request().ending is None
    assert request(ending={'mode': 'last_frame'}).ending.duration_ms == 3000
    for ending in ({'mode': 'unknown'}, {'mode': 'image'},
                   {'mode': 'image', 'image': '../secret.png'},
                   {'mode': 'last_frame', 'duration_ms': 0},
                   {'mode': 'last_frame', 'duration_ms': 15001},
                   {'mode': 'last_frame', 'guide': 'x' * 201}):
        with pytest.raises(ValueError):
            request(ending=ending)


def test_draft_and_legacy_composition_preserve_explicit_ending_and_total():
    draft = dict(drama_id='101500', business_date='20261012', author='test', rows=[
        dict(request_id='R1', seq=1, timeline_text='EP1 0s-2s',
             ending=dict(mode='last_frame', guide='Watch more!'))])
    manifest, errors = validate_draft(draft, '')
    assert not errors
    assert manifest.requests[0].ending.mode == 'last_frame'
    timing = timing_explanation(manifest)[0]
    assert timing['planned_duration_ms'] == 5000
    assert timing['ending_duration_ms'] == 3000
    row = {'composition': {'plots': [{'episode': '1', 'start': '00:00', 'end': '00:02'}],
                          'ending': {'mode': 'last_frame', 'duration_ms': 4000, 'guide': 'More'}}}
    compile_composition(row)
    assert row['composition']['ending']['mode'] == 'last_frame'
    draft['rows'][0] = dict(row, request_id='R1', seq=1)
    assert validate_draft(draft, '')[0].requests[0].ending.duration_ms == 4000
    del row['composition']['ending']['mode']
    compile_composition(row)
    draft['rows'][0] = dict(row, request_id='R1', seq=1)
    assert validate_draft(draft, '')[0].requests[0].ending.mode == 'none'


def frame(video, seconds):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(video),
        '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'], check=True, capture_output=True)
    return Image.open(io.BytesIO(result.stdout)).convert('RGB')


@pytest.mark.integration
def test_real_last_frame_is_appended_silent_and_reuses_base(tmp_path):
    r = request(ending={'mode': 'last_frame', 'duration_ms': 1200})
    config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast')
    timeline = [item.model_dump(mode='json') for item in r.timeline]
    first = renderer.render_request(tmp_path, r, timeline, tmp_path/'first.mp4', config)
    assert first['expected_duration_ms'] == 2200
    assert abs(duration_ms(tmp_path/'first.mp4') - 2200) < 100
    diff = ImageChops.difference(frame(tmp_path/'first.mp4', .9), frame(tmp_path/'first.mp4', 1.8))
    assert max(ImageStat.Stat(diff).mean) < 3
    r.ending.guide = '继续观看\nWatch more!'
    second = renderer.render_request(tmp_path, r, timeline, tmp_path/'second.mp4', config)
    assert second['base_cache_reused']
    assert second['base_cache_key'] == first['base_cache_key']
    assert ImageChops.difference(frame(tmp_path/'first.mp4', 1.8), frame(tmp_path/'second.mp4', 1.8)).getbbox()
    audio = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '1.2', '-i', str(tmp_path/'second.mp4'),
        '-t', '0.7', '-f', 's16le', '-acodec', 'pcm_s16le', '-'], check=True, capture_output=True).stdout
    assert audio and set(audio) == {0}


@pytest.mark.integration
def test_image_snapshot_and_contain_render(tmp_path, monkeypatch):
    import os
    import stat
    from exactcut.ending import snapshot_images, image_file
    from exactcut.models import BatchManifest
    batch = tmp_path/'batch'; (batch/'endings').mkdir(parents=True)
    Image.new('RGB', (100, 100), 'red').save(batch/'endings/poster.png')
    r = request(ending={'mode': 'image', 'image': 'poster.png', 'duration_ms': 1000})
    manifest = BatchManifest(drama_id='101500', business_date='20261012', author='test',
                             drama_title='', source_workbook='', requests=[r])
    project = tmp_path/'project'
    synced_dirs = set()
    fsync = os.fsync
    def capture_fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode): synced_dirs.add(info.st_ino)
        fsync(fd)
    monkeypatch.setattr(os, 'fsync', capture_fsync)
    snapshot_images(batch, project, manifest)
    assert all(p.stat().st_ino in synced_dirs for p in [project/'assets/endings', project/'assets', project, project.parent])
    saved = image_file(project, r.ending)
    assert saved.name != 'poster.png'
    Image.new('RGB', (100, 100), 'blue').save(batch/'endings/poster.png')
    assert Image.open(saved).getpixel((0, 0)) == (255, 0, 0)
    config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast')
    plan = renderer.render_request(project, r, [i.model_dump(mode='json') for i in r.timeline], tmp_path/'out.mp4', config)
    image = frame(tmp_path/'out.mp4', 1.5)
    assert image.getpixel((90, 160))[0] > 240
    assert max(image.getpixel((90, 10))) < 10
    assert plan['expected_duration_ms'] == 2000
    saved.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='校验'):
        image_file(project, r.ending)


@pytest.mark.ui
def test_editor_ending_controls_keep_plain_rows_and_update_total(tmp_path):
    import re
    from pathlib import Path
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root/'index.html').read_text())
    source = '\n'.join(line for line in (root/'app.js').read_text().splitlines()
                       if not line.startswith('(async()=>{try{const initial='))
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
            page.goto('http://fixture.test/')
            page.add_script_tag(content=(root/'editor-busy.js').read_text())
            page.add_script_tag(content=source)
            page.evaluate('''() => {
              currentUser={features:{material_fetch:false}};
              current={id:'fixture',filename:'网页填写',revision:1,warnings:[],errors:[],assets:[],
                runtime:{status:'draft'},draft:{drama_id:'101500',author:'test',business_date:'20261012',rows:[
                  {request_id:'R1',seq:1,hook_text:'',clips:[{episode:'1',start_s:'0',end_s:'2'}]}]}};
              selectedId=current.id;showPage('workspace');setTab('editor');renderEditor();
            }''')
            assert page.get_by_label('结尾画面').input_value() == 'none'
            assert page.evaluate('current.draft.rows[0].ending === undefined && !dirty')
            page.get_by_label('结尾画面').select_option('last_frame')
            assert page.get_by_label('停留时长（秒）').input_value() == '3'
            page.get_by_label('引导文案（选填）').fill('Watch more!')
            page.get_by_label('停留时长（秒）').fill('4')
            assert '6.000 秒' in page.locator('[data-timing-index="0"]').inner_text()
            assert page.evaluate("current.draft.rows[0].ending.duration_ms === 4000 && !current.draft.rows[0].composition")
            page.get_by_label('结尾画面').select_option('image')
            assert page.get_by_label('结尾图片').is_visible()
            png = io.BytesIO(); Image.new('RGB', (100, 150), 'navy').save(png, format='PNG')
            page.route('**/ending-image', lambda route: route.fulfill(
                body='{"image":"R1.png"}', content_type='application/json'))
            page.get_by_label('结尾图片').set_input_files({'name': 'poster.png', 'mimeType': 'image/png', 'buffer': png.getvalue()})
            page.wait_for_function("current.draft.rows[0].ending.image === 'R1.png'")
            assert page.get_by_alt_text('结尾图片预览').evaluate('(img) => img.complete && img.naturalWidth === 100')
            assert page.evaluate("!('undefined' in current.draft.rows[0])")
            assert page.locator('#saveButton').is_enabled()
            assert page.locator('#confirmButton').is_enabled()
            page.evaluate('''() => {
              globalThis.pending=[];
              api=async()=>await new Promise(resolve=>pending.push(resolve));
              globalThis.sendFile=()=>uploadEnding({files:[new File(['png'], 'poster.png', {type:'image/png'})],dataset:{row:'0'}});
              globalThis.firstUpload=sendFile();
            }''')
            page.wait_for_function('pending.length===1')
            assert page.locator('#saveButton').is_disabled()
            assert page.locator('#confirmButton').is_disabled()
            page.evaluate('()=>{globalThis.secondUpload=sendFile()}')
            page.wait_for_function('pending.length===2')
            page.evaluate("async()=>{pending[1]({image:'B.png'});await secondUpload;pending[0]({image:'A.png'});await firstUpload}")
            assert page.evaluate("current.draft.rows[0].ending.image==='B.png'")
            assert page.evaluate('!controls.endingUploads.size')
            # Restore the decoded image for the visual check.
            page.evaluate("current.draft.rows[0].ending.image='R1.png';renderEditor()")
            page.locator('.request').screenshot(path=str(tmp_path/'ending-editor.png'))
            page.evaluate("current.id='another-project';selectedId=current.id;renderEditor()")
            assert '/another-project/ending-thumbnail/' in page.get_by_alt_text('结尾图片预览').get_attribute('src')
            page.get_by_label('结尾画面').select_option('none')
            assert '2.000 秒' in page.locator('[data-timing-index="0"]').inner_text()
            assert page.get_by_label('结尾图片').count() == 0
        finally:
            browser.close()


@pytest.mark.parametrize('managed', [False, True])
def test_confirm_and_clone_keep_image_bytes_and_ending_settings(tmp_path, monkeypatch, managed):
    import base64
    import uuid
    from exactcut import workflow as w, material_versions as m
    from exactcut.util import read_json
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path/'.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path/'feedback')
    item = w.create_blank('test')
    path = w.folder(item['id'])
    if managed:
        m.enroll(path)
        item = m.detail(path)
    draft = copy.deepcopy(item['draft']); row = draft['rows'][0]
    draft.update(drama_id='101500', author='test')
    row['clips'] = [dict(episode='1', start_s='0', end_s='2')]
    png = io.BytesIO(); Image.new('RGB', (100, 150), 'red').save(png, format='PNG')
    payload = base64.b64encode(png.getvalue()).decode()
    uploaded = m.save_ending_image(path, row['request_id'], payload) if managed else w.save_ending_image(path.name, row['request_id'], payload)
    row['ending'] = dict(mode='image', image=uploaded['image'], guide='Watch more!', duration_ms=4000)
    data = dict(revision=item['revision'], mutation_id=uuid.uuid4().hex, draft=draft)
    saved = m.save_draft(path, data) if managed else w.save_draft(path.name, data)
    assert w.ending_image_file(path.name, row['request_id']).read_bytes() == png.getvalue()
    cloned = w.clone_batch(path.name)
    cloned_row = cloned['draft']['rows'][0]
    assert cloned_row['ending'] == row['ending']
    assert w.ending_image_file(cloned['id'], cloned_row['request_id']).read_bytes() == png.getvalue()
    if managed:
        confirmed = m.confirm(path, dict(revision=saved['revision'], mutation_id=uuid.uuid4().hex))
        version = next(iter(confirmed['versions'].values()))
        ending = version['manifest']['requests'][0]['ending']
    else:
        w.start_batch(path.name, saved['revision'])
        ending = read_json(path/'confirmed.json')['requests'][0]['ending']
    assert ending['mode'] == 'image' and ending['duration_ms'] == 4000
    assert (tmp_path/'projects/101500/assets/endings'/ending['image']).read_bytes() == png.getvalue()


def test_invalid_image_cannot_be_confirmed(tmp_path):
    from exactcut.ending import snapshot_images
    from exactcut.models import BatchManifest
    batch = tmp_path/'batch'; (batch/'endings').mkdir(parents=True)
    (batch/'endings/bad.png').write_bytes(b'\x89PNG\r\n\x1a\nbroken')
    manifest = BatchManifest(drama_id='101500', business_date='20261012', author='test', drama_title='',
                             source_workbook='', requests=[request(ending={'mode':'image', 'image':'bad.png'})])
    with pytest.raises(ValueError, match='无法解码'):
        snapshot_images(batch, tmp_path/'project', manifest)


@pytest.mark.integration
@pytest.mark.parametrize('end_ms', [33, 1033])
def test_freeze_uses_exact_cut_tail_and_never_repeats_story_audio(tmp_path, monkeypatch, end_ms):
    source = tmp_path/'source/videos/EP1.mp4'; source.parent.mkdir(parents=True)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=180x320:r=30:d=1',
        '-f', 'lavfi', '-i', 'color=blue:s=180x320:r=30:d=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]', '-map', '2:a',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-ac', '2', str(source)], check=True)
    r = CutRequest(request_id='R1', seq=1, output_filename='out.mp4',
                   timeline=[dict(type='episode', episode='EP1', start_ms=0, end_ms=end_ms)],
                   ending=dict(mode='last_frame', duration_ms=1000))
    config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast')
    output = tmp_path/'out.mp4'
    monkeypatch.setattr(renderer, '_concat_units_encoded', lambda *args: pytest.fail('Ending must not re-encode the complete story'))
    renderer.render_request(tmp_path, r, [i.model_dump(mode='json') for i in r.timeline], output, config)
    color = frame(output, end_ms/1000 + .5).getpixel((90, 160))
    assert color[2 if end_ms > 1000 else 0] > 240
    audio = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(end_ms/1000+.2), '-i', str(output),
        '-t', '0.5', '-f', 's16le', '-acodec', 'pcm_s16le', '-'], check=True, capture_output=True).stdout
    assert audio and set(audio) == {0}
