"""Browser API-boundary tests; media accuracy is covered separately."""
import base64
import re
from pathlib import Path
import pytest
ROOT = Path(__file__).parents[1] / 'exactcut/web'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG1kAAAAASUVORK5CYII=')

@pytest.fixture
def picker_page(request):
    playwright = pytest.importorskip('playwright.sync_api')
    html = re.sub(r'<script src="[^"]+"></script>', '', (ROOT / 'index.html').read_text())
    script = '\n'.join(line for line in (ROOT / 'app.js').read_text().splitlines() if not line.startswith('(async()=>{try{const initial='))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        def route(req):
            if '/frame/' in req.request.url: req.fulfill(body=PNG, content_type='image/png')
            elif req.request.method == 'POST' and req.request.url.endswith('/source-picker'):
                req.fulfill(json={'key':'a'*64,'episode':'EP1','fps':'30/1','times_ms':[0,33,66,100,133,166],'duration_ms':200})
            elif '/video' in req.request.url: req.fulfill(status=404)
            elif '/api/' in req.request.url: req.fulfill(json={})
            else: req.fulfill(body=html,content_type='text/html')
        page.route('**/*',route)
        page.goto('http://fixture.test/')
        # Product entry stays hidden; exercise the retained component separately.
        if getattr(request, 'param', True):
            page.add_style_tag(content='[data-source-picker], [data-picker-status] { display: inline-block !important; }')
        page.add_script_tag(content=script)
        if (ROOT/'source-picker.js').exists(): page.add_script_tag(content=(ROOT/'source-picker.js').read_text())
        page.evaluate('''() => {
          currentUser={features:{material_fetch:false}};
          current={id:'a'.repeat(32),filename:'网页填写',revision:1,warnings:[],errors:[],assets:[],runtime:{status:'draft'},draft:{drama_id:'101494',author:'test',business_date:'20261013',rows:[{request_id:'R1',seq:1,hook_text:'',hook_options:{mode:'none'},clips:[{episode:'1',start_m:'00',start_s:'00',start_ms:'000',end_m:'00',end_s:'00',end_ms:'200'}]}]}};
          selectedId=current.id;showPage('workspace');renderEditor();
        }''')
        yield page
        browser.close()

def open_picker(page):
    page.get_by_role('button',name='查看画面 / 精调',exact=True).click(timeout=1500)
    page.wait_for_function("document.querySelector('#pickerApply') && !document.querySelector('#pickerApply').disabled")

def test_independent_frames_and_inclusive_end(picker_page):
    page=picker_page;open_picker(page)
    end=page.locator('#pickerEndFrame').get_attribute('src')
    page.locator('#pickerStartNext').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerStartFrame').get_attribute('src').endswith('/frame/1')
    assert page.locator('#pickerEndFrame').get_attribute('src')==end
    page.locator('#pickerEndPrev').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='000'
    assert page.locator('#pickerStartTime').inner_text()=='00:00.033'
    assert page.locator('#pickerStartFrame').get_attribute('src').endswith('/frame/1')
    page.locator('#pickerApply').click()
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='033'
    assert page.evaluate('current.draft.rows[0].clips[0].end_ms')=='166'

def test_raw_values_preserved_and_start_uses_ceiling(picker_page):
    page=picker_page
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_m:'0',start_s:'0',start_ms:'009',end_ms:'190'})")
    before=page.evaluate('JSON.stringify(current.draft.rows[0].clips[0])')
    open_picker(page)
    assert page.locator('#pickerStartFrame').get_attribute('src').endswith('/frame/1')
    assert page.locator('#pickerEndFrame').get_attribute('src').endswith('/frame/5')
    page.locator('#pickerApply').click()
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    assert page.evaluate('JSON.stringify(current.draft.rows[0].clips[0])')==before

def test_one_timeline_static_previews_and_footer(picker_page):
    page=picker_page;page.set_viewport_size({'width':1280,'height':720});open_picker(page)
    assert page.locator('.picker-timeline:visible').count()==1
    assert page.locator('#sourcePickerDialog input[type=range]').count()==0
    assert page.locator('#sourcePickerDialog video:visible').count()==0
    assert page.get_by_role('heading',name='开始画面',exact=True).is_visible()
    assert page.get_by_role('heading',name='结束画面',exact=True).is_visible()
    assert '30/1' not in page.locator('#sourcePickerDialog').inner_text()
    for control in ['pickerApply','pickerCancel','pickerStartPrev','pickerEndNext']:
        assert page.locator('#'+control).evaluate('(e)=>{const r=e.getBoundingClientRect();return r.bottom<=innerHeight&&r.top>=0}')

def test_handles_keyboard_drag_and_no_crossing(picker_page):
    page=picker_page;open_picker(page)
    page.locator('#pickerStartHandle').focus()
    page.keyboard.press('End')
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerStartHandle').get_attribute('aria-valuenow')=='5'
    assert page.locator('#pickerStartNext').is_disabled()
    assert page.locator('#pickerEndPrev').is_disabled()
    assert page.locator('#sourcePickerDialog [role=slider]').count()==2
    assert page.locator('#pickerStartHandle').bounding_box()['y']!=page.locator('#pickerEndHandle').bounding_box()['y']
    track=page.locator('#pickerTimeline').bounding_box()
    handle=page.locator('#pickerStartHandle').bounding_box()
    page.mouse.move(handle['x']+handle['width']/2,handle['y']+handle['height']/2)
    page.mouse.down();page.mouse.move(track['x'],handle['y']+handle['height']/2);page.mouse.up()
    assert page.locator('#pickerStartHandle').get_attribute('aria-valuenow')=='0'

def test_cancel_and_stale_material(picker_page):
    page=picker_page;open_picker(page)
    page.locator('#pickerStartNext').click();page.locator('#pickerCancel').click()
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='000'
    open_picker(page)
    page.evaluate("current.draft.rows[0]={...structuredClone(current.draft.rows[0]),request_id:'R2'}")
    page.locator('#pickerApply').click()
    assert '变化' in page.locator('#pickerMessage').inner_text()
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='000'

def test_retry_and_cancel_discard_pending_frames(picker_page):
    page=picker_page
    page.evaluate("() => {window.pendingFrames=[];window.Image=class {set src(value){this.url=value;pendingFrames.push(this)} get src(){return this.url}};}")
    page.get_by_role('button',name='查看画面 / 精调',exact=True).click()
    page.wait_for_function('pendingFrames.length===2')
    page.evaluate('pendingFrames[0].onerror()')
    page.locator('#pickerStartNext').click()
    page.wait_for_function('pendingFrames.length===3')
    page.evaluate('() => {api=()=>new Promise(()=>{});}')
    page.locator('#pickerRetry').click()
    page.evaluate('pendingFrames[2].onload()')
    assert page.locator('#pickerApply').is_disabled()
    assert page.locator('#pickerStartFrame').is_hidden()
    page.locator('#pickerCancel').click()
    page.evaluate('pendingFrames[1].onload()')
    assert page.locator('#pickerEndFrame').is_hidden()

def test_changed_frames_must_confirm_and_apply_freezes_controls(picker_page):
    page=picker_page;open_picker(page)
    page.evaluate("() => {window.Image=class {set src(value){this.url=value;window.pendingFrame=this} get src(){return this.url}};}")
    page.locator('#pickerStartNext').click()
    assert page.locator('#pickerApply').is_disabled()
    page.evaluate('pendingFrame.onload()')
    page.evaluate("() => {api=()=>new Promise(resolve=>window.finishApply=()=>resolve({key:'a'.repeat(64)}));}")
    page.locator('#pickerApply').click()
    page.wait_for_function("typeof finishApply==='function'")
    for control in ['pickerStartNext','pickerEndPrev','pickerStartHandle','pickerEndHandle','pickerRetry']:
        assert page.locator('#'+control).is_disabled()
    page.evaluate('finishApply()')
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='033'

def test_source_change_rejects_apply_and_retry_resets_on_reopen(picker_page):
    page=picker_page;open_picker(page)
    page.route('**/source-picker',lambda r:r.fulfill(json={'key':'b'*64}))
    page.locator('#pickerApply').click()
    page.wait_for_function("document.querySelector('#pickerMessage').textContent.includes('原片已变化')")
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='000'
    page.locator('#pickerCancel').click()
    page.route('**/source-picker',lambda r:r.fulfill(status=400,json={'error':'原片暂不可用'}))
    page.get_by_role('button',name='查看画面 / 精调',exact=True).click()
    page.locator('#pickerRetry').wait_for(state='visible')
    assert page.locator('#pickerRetry').is_enabled()

def test_editing_end_preserves_unmodified_start_strings(picker_page):
    page=picker_page
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_m:'0',start_s:'00',start_ms:'009'})")
    open_picker(page)
    page.locator('#pickerEndPrev').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    page.locator('#pickerApply').click()
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    clip=page.evaluate('current.draft.rows[0].clips[0]')
    assert (clip['start_m'],clip['start_s'],clip['start_ms'])==('0','00','009')
    assert clip['end_ms']=='166'

def test_composition_preserves_raw_time_string(picker_page):
    page=picker_page
    page.evaluate("() => { current.draft.rows[0].composition={...blankComposition(),plots:[{episode:'1',start:'0:00.009',end:'0:00.190'}]};renderEditor(); }")
    open_picker(page)
    page.locator('#pickerEndPrev').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    page.locator('#pickerApply').click()
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    assert page.evaluate('current.draft.rows[0].composition.plots[0].start')=='0:00.009'
    assert page.evaluate('current.draft.rows[0].composition.plots[0].end')=='00:00.166'

def test_cancel_during_apply_ignores_late_revalidation(picker_page):
    page=picker_page;open_picker(page)
    page.locator('#pickerStartNext').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    page.evaluate("() => {api=()=>new Promise(resolve=>window.finishApply=()=>resolve({key:'a'.repeat(64)}));}")
    page.locator('#pickerApply').click()
    page.locator('#pickerCancel').click()
    page.evaluate('finishApply()')
    assert page.evaluate('current.draft.rows[0].clips[0].start_ms')=='000'
    assert not page.locator('#sourcePickerDialog').evaluate('(e)=>e.open')

def test_empty_raw_interval_has_no_preview_or_apply_until_repaired(picker_page):
    page=picker_page
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_ms:'034',end_ms:'050'})")
    page.get_by_role('button',name='查看画面 / 精调',exact=True).click()
    page.wait_for_function("!document.querySelector('#pickerStartHandle').disabled")
    assert page.locator('#pickerApply').is_disabled()
    assert page.locator('#pickerPlay').is_disabled()
    assert page.locator('#pickerStartFrame').is_hidden()
    assert page.locator('#pickerEndFrame').is_hidden()
    assert '没有可保留画面' in page.locator('#pickerMessage').inner_text()
    page.locator('#pickerStartPrev').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerStartFrame').get_attribute('src').endswith('/frame/1')
    assert page.locator('#pickerEndFrame').get_attribute('src').endswith('/frame/1')

def test_start_after_last_frame_has_no_preview(picker_page):
    page=picker_page
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_ms:'170',end_ms:'190'})")
    page.get_by_role('button',name='查看画面 / 精调',exact=True).click()
    page.wait_for_function("!document.querySelector('#pickerStartHandle').disabled")
    assert page.locator('#pickerApply').is_disabled()
    assert page.locator('#pickerStartFrame').is_hidden()


def test_two_thin_playheads_move_images_independently(picker_page):
    page=picker_page;open_picker(page)
    assert page.locator('#sourcePickerDialog [role=slider]').count()==2
    start=page.locator('#pickerStartFrame').get_attribute('src')
    page.locator('#pickerEndHandle').focus();page.keyboard.press('ArrowLeft')
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerEndFrame').get_attribute('src').endswith('/frame/4')
    assert page.locator('#pickerStartFrame').get_attribute('src')==start
    assert page.locator('#pickerTimeline .picker-tick').count()>=3
    track=page.locator('#pickerTimeline').bounding_box()
    head=page.locator('#pickerEndHandle').bounding_box()
    page.mouse.move(head['x']+head['width']/2,head['y']+5)
    page.mouse.down();page.mouse.move(track['x']+track['width']*.4,head['y']+5);page.mouse.up()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerEndFrame').get_attribute('src').endswith('/frame/2')
    assert page.locator('#pickerStartFrame').get_attribute('src')==start

def frame_mode(page):
    page.evaluate("() => { current.draft.rows[0].clips=[{episode:'1',time_format:'source_frames_v1',start_m:'0',start_s:'00',start_f:'001',end_m:'00',end_s:'00',end_f:'004'}];renderEditor(); }")

def test_frame_fields_render_and_picker_writes_inclusive_ordinal(picker_page):
    page=picker_page;frame_mode(page)
    assert page.get_by_role('textbox',name='片段 1 开始 帧',exact=True).input_value()=='001'
    assert page.get_by_role('textbox',name='片段 1 开始 毫秒',exact=True).count()==0
    open_picker(page)
    assert page.locator('#pickerStartTime').inner_text()=='00:00:001'
    assert page.locator('#pickerEndTime').inner_text()=='00:00:004'
    assert page.locator('#pickerStartFrame').get_attribute('src').endswith('/frame/1')
    assert page.locator('#pickerEndFrame').get_attribute('src').endswith('/frame/4')
    page.locator('#pickerEndNext').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    page.locator('#pickerApply').click()
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    clip=page.evaluate('current.draft.rows[0].clips[0]')
    assert clip['end_f']=='005'
    assert clip['start_m']=='0'
    assert clip['start_f']=='001'
    assert 'end_ms' not in clip

def test_frame_ordinals_reset_per_actual_source_second(picker_page):
    page=picker_page
    page.route('**/source-picker',lambda r:r.fulfill(json={'key':'a'*64,'episode':'EP1','fps':'30/1','times_ms':[0,33,990,1023,1070,2009],'duration_ms':2040}))
    frame_mode(page)
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_f:'002',end_s:'01',end_f:'001'})")
    open_picker(page)
    page.locator('#pickerStartNext').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerStartTime').inner_text()=='00:01:000'
    page.locator('#pickerApply').click()
    page.wait_for_function("!document.querySelector('#sourcePickerDialog').open")
    clip=page.evaluate('current.draft.rows[0].clips[0]')
    assert (clip['start_s'],clip['start_f'])==('01','000')
    assert (clip['end_s'],clip['end_f'])==('01','001')

def test_invalid_frame_ordinal_requires_correcting_that_side(picker_page):
    page=picker_page;frame_mode(page)
    page.evaluate("current.draft.rows[0].clips[0].start_f='999'")
    page.get_by_role('button',name='查看画面 / 精调',exact=True).click()
    page.wait_for_function("!document.querySelector('#pickerStartHandle').disabled")
    assert page.locator('#pickerApply').is_disabled()
    assert page.locator('#pickerStartFrame').is_hidden()
    page.locator('#pickerStartNext').click()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")

def test_new_project_frame_mode_does_not_convert_legacy_projects(picker_page):
    page=picker_page
    assert 'start_ms' in page.evaluate('blankClip()')
    page.evaluate("current.draft.editor_time_format='source_frames_v1'")
    blank=page.evaluate('blankClip()')
    assert blank['time_format']=='source_frames_v1'
    assert blank['start_f']=='000'
    assert 'start_ms' not in blank

def test_frame_input_focusout_and_timing_not_milliseconds(picker_page):
    page=picker_page;frame_mode(page)
    field=page.get_by_role('textbox',name='片段 1 开始 帧',exact=True)
    field.fill('2');field.press('Tab')
    assert field.input_value()=='002'
    assert page.evaluate("clipMillis(current.draft.rows[0].clips[0],'start')") is None
    assert '分:秒:帧' in page.locator('#requests').inner_text()
    assert '请按原片填写分、秒、帧' in page.locator('[data-timing-index="0"]').inner_text()
    assert '打开选帧' not in page.locator('[data-timing-index="0"]').inner_text()

def test_frame_zero_is_a_valid_single_frame_draft(picker_page):
    page=picker_page;frame_mode(page)
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_f:'000',end_f:'000'})")
    assert page.evaluate('hasDraftData(current.draft)')
    page.evaluate('controls()')
    assert page.locator('#saveButton').is_enabled()
    page.evaluate("current.draft.rows[0].clips[0].end_f=''")
    assert not page.evaluate('hasDraftData(current.draft)')

def test_frame_timing_escapes_persisted_input_and_opens_frame_paste(picker_page):
    page=picker_page;frame_mode(page)
    page.evaluate("() => {current.draft.editor_time_format='source_frames_v1';current.draft.rows[0].clips[0].start_f='<img src=x onerror=alert(1)>';renderEditor();openClipText(0);}")
    assert page.locator('[data-timing-index="0"] img').count()==0
    assert page.locator('#clipTextDialog').evaluate('(e)=>e.open')
    assert '分:秒:帧' in page.locator('#clipTextHelp').inner_text()

def test_new_batch_requests_frame_format_explicitly(picker_page):
    page=picker_page
    page.evaluate("() => {api=async (url,body)=>{window.newBatchRequest={url,body};throw Error('fixture stop')};document.getElementById('newBatch').onclick();}")
    page.wait_for_function('window.newBatchRequest')
    assert page.evaluate('newBatchRequest')=={'url':'/api/batches/blank','body':{'time_format':'source_frames_v1'}}

def test_overlapping_handles_keep_end_grab_reachable_after_start_focus(picker_page):
    page=picker_page;frame_mode(page)
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_f:'000',end_f:'000'})")
    open_picker(page)
    page.locator('#pickerStartHandle span').click()
    end=page.locator('#pickerEndHandle span').bounding_box()
    assert page.evaluate('''([x,y]) => document.elementFromPoint(x,y).closest('button').id''', [end['x']+end['width']/2,end['y']+end['height']/2])=='pickerEndHandle'
    track=page.locator('#pickerTimeline').bounding_box()
    page.mouse.move(end['x']+end['width']/2,end['y']+end['height']/2)
    page.mouse.down();page.mouse.move(track['x']+track['width']*.66,end['y']+end['height']/2);page.mouse.up()
    page.wait_for_function("!document.querySelector('#pickerApply').disabled")
    assert page.locator('#pickerEndFrame').get_attribute('src').endswith('/frame/4')
    assert page.locator('#pickerStartFrame').get_attribute('src').endswith('/frame/0')


def test_retry_releases_previous_source_video_before_loading_new_index(picker_page):
    page=picker_page;open_picker(page)
    page.evaluate("document.getElementById('pickerVideo').src='/old-source/video';document.getElementById('pickerPlayer').hidden=false;document.getElementById('pickerRetry').hidden=false")
    page.route('**/source-picker',lambda r:r.fulfill(json={'key':'b'*64,'episode':'EP1','fps':'30/1','times_ms':[0,33,66,100,133,166],'duration_ms':200}))
    page.locator('#pickerHidePlayer').click()
    page.locator('#pickerRetry').click()
    page.wait_for_function("document.querySelector('#pickerStartFrame').getAttribute('src')?.includes('bbbb')")
    assert page.locator('#pickerVideo').get_attribute('src') is None
    assert page.locator('#pickerPlayer').is_hidden()
    page.locator('#pickerPlay').click()
    assert '/'+('b'*64)+'/video' in page.locator('#pickerVideo').get_attribute('src')


@pytest.mark.parametrize('picker_page', [False], indirect=True)
def test_product_hides_picker_entry_and_hint(picker_page):
    page = picker_page
    assert page.locator('[data-source-picker]').count() > 0
    assert page.locator('[data-source-picker]:visible').count() == 0
    assert page.locator('[data-picker-status]:visible').count() == 0
