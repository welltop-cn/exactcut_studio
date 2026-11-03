"""Browser API-boundary tests; media accuracy is covered separately."""
import re
from pathlib import Path
import pytest
from browser_support import shared_playwright

pytestmark = pytest.mark.ui
ROOT = Path(__file__).parents[1] / 'exactcut/web'

@pytest.fixture
def frame_page():
    playwright = pytest.importorskip('playwright.sync_api')
    html = re.sub(r'<script src="[^"]+"></script>', '', (ROOT / 'index.html').read_text())
    script = '\n'.join(line for line in (ROOT / 'app.js').read_text().splitlines() if not line.startswith('(async()=>{try{const initial='))
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        def route(req):
            if '/api/' in req.request.url: req.fulfill(json={})
            else: req.fulfill(body=html,content_type='text/html')
        page.route('**/*',route)
        page.goto('http://fixture.test/')
        page.add_script_tag(content=(ROOT/'editor-busy.js').read_text())
        page.add_script_tag(content=script)
        page.evaluate('''() => {
          currentUser={features:{material_fetch:false}};
          current={id:'a'.repeat(32),filename:'网页填写',revision:1,warnings:[],errors:[],assets:[],runtime:{status:'draft'},draft:{drama_id:'101494',author:'test',business_date:'20261013',rows:[{request_id:'R1',seq:1,hook_text:'',hook_options:{mode:'none'},clips:[{episode:'1',start_m:'00',start_s:'00',start_ms:'000',end_m:'00',end_s:'00',end_ms:'200'}]}]}};
          selectedId=current.id;showPage('workspace');renderEditor();
        }''')
        yield page
        browser.close()


def frame_mode(page):
    page.evaluate("() => { current.draft.rows[0].clips=[{episode:'1',time_format:'source_frames_v1',start_m:'0',start_s:'00',start_f:'001',end_m:'00',end_s:'00',end_f:'004'}];renderEditor(); }")

def test_new_project_frame_mode_does_not_convert_legacy_projects(frame_page):
    page=frame_page
    assert 'start_ms' in page.evaluate('blankClip()')
    page.evaluate("current.draft.editor_time_format='source_frames_v1'")
    blank=page.evaluate('blankClip()')
    assert blank['time_format']=='source_frames_v1'
    assert blank['start_f']=='000'
    assert 'start_ms' not in blank

def test_frame_input_focusout_and_timing_not_milliseconds(frame_page):
    page=frame_page;frame_mode(page)
    field=page.get_by_role('textbox',name='片段 1 开始 帧',exact=True)
    field.fill('2');field.press('Tab')
    assert field.input_value()=='002'
    assert page.evaluate("clipMillis(current.draft.rows[0].clips[0],'start')") is None
    assert '分:秒:帧' in page.locator('#requests').inner_text()
    assert '请按原片填写分、秒、帧' in page.locator('[data-timing-index="0"]').inner_text()
    assert '打开选帧' not in page.locator('[data-timing-index="0"]').inner_text()

def test_frame_zero_is_a_valid_single_frame_draft(frame_page):
    page=frame_page;frame_mode(page)
    page.evaluate("Object.assign(current.draft.rows[0].clips[0],{start_f:'000',end_f:'000'})")
    assert page.evaluate('hasDraftData(current.draft)')
    page.evaluate('controls()')
    assert page.locator('#saveButton').is_enabled()
    page.evaluate("current.draft.rows[0].clips[0].end_f=''")
    assert not page.evaluate('hasDraftData(current.draft)')

def test_frame_timing_escapes_persisted_input_and_opens_frame_paste(frame_page):
    page=frame_page;frame_mode(page)
    page.evaluate("() => {current.draft.editor_time_format='source_frames_v1';current.draft.rows[0].clips[0].start_f='<img src=x onerror=alert(1)>';renderEditor();openClipText(0);}")
    assert page.locator('[data-timing-index="0"] img').count()==0
    assert page.locator('#clipTextDialog').evaluate('(e)=>e.open')
    assert '分:秒:帧' in page.locator('#clipTextHelp').inner_text()

def test_new_batch_requests_frame_format_explicitly(frame_page):
    page=frame_page
    page.evaluate("() => {api=async (url,body)=>{window.newBatchRequest={url,body};throw Error('fixture stop')};document.getElementById('newBatch').onclick();}")
    page.wait_for_function('window.newBatchRequest')
    assert page.evaluate('newBatchRequest')=={'url':'/api/batches/blank','body':{'time_format':'source_frames_v1'}}


def test_frame_fields_render_without_visual_picker(frame_page):
    page=frame_page
    frame_mode(page)
    assert page.get_by_role('textbox',name='片段 1 开始 帧',exact=True).input_value()=='001'
    assert page.get_by_role('textbox',name='片段 1 开始 毫秒',exact=True).count()==0
    assert page.locator('[data-source-picker], [data-picker-status], #sourcePickerDialog').count()==0
    assert page.evaluate('typeof window.SourcePicker')=='undefined'
    assert not (ROOT / 'source-picker.js').exists()
    assert 'source-picker.js' not in (ROOT / 'index.html').read_text()

def test_legacy_millisecond_fields_remain_unchanged(frame_page):
    assert frame_page.get_by_role('textbox',name='片段 1 开始 毫秒',exact=True).input_value()=='000'
    assert frame_page.get_by_role('textbox',name='片段 1 开始 帧',exact=True).count()==0
    assert frame_page.evaluate('current.draft.rows[0].clips[0].end_ms')=='200'
