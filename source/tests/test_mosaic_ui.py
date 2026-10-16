"""Independent mosaic workspace; mocked model API, real browser/frame geometry."""
import io
import re
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def editor():
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root/'index.html').read_text())
    jpeg = io.BytesIO()
    Image.new('RGB', (160, 96), '#555555').save(jpeg, 'JPEG')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.set_default_timeout(3000)
        page.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        page.route('**/frame/*', lambda r: r.fulfill(body=jpeg.getvalue(), content_type='image/jpeg'))
        page.goto('http://fixture.test/')
        page.evaluate('''() => {
          window.$=id=>document.getElementById(id);
          window.esc=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          window.appUrl=u=>u;
          window.currentUser={username:'fixture',features:{cutting:true}};
          window.current={id:'batch',assets:[{title:'测试',can_edit_video:true,request_id:'R1',sha256:'a'.repeat(64),preview:'/video'}]};
          window.calls=[];
          window.session={id:'session',source_sha256:'a'.repeat(64),source_filename:'source.mp4',metadata:{frame_count:300,fps_num:30,fps_den:1,width:160,height:96},capabilities:{region_recognition:true},jobs:[]};
          window.findings=[{id:'0',label:'候选区域',reason:'需要人工确认',box_2d:[250,250,750,750],x:40,y:24,w:80,h:48}];
          window.api=async (url,body)=>{
            calls.push({url,body});
            if(url.endsWith('/locate'))return {id:'job',status:'completed',...body,candidates:findings};
            return session;
          };
          $('assets').innerHTML='<article data-asset="0"><div class="cut-tools"><button data-open-cut>剪切</button></div></article>';
          $('assets').closest('[hidden]')?.removeAttribute('hidden');
        }''')
        script=root/'mosaic.js'
        if script.exists():
            page.add_script_tag(content=script.read_text())
        yield page
        browser.close()


def open_editor(page):
    page.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    page.locator('#mosaicRecognize').wait_for(state='visible')
    page.wait_for_function("!document.getElementById('mosaicRecognize').disabled")


def test_separate_entry_identify_confirm_and_restore(editor):
    open_editor(editor)
    assert not editor.locator('#cutDialog').evaluate('(el)=>el.open')
    editor.locator('#mosaicRecognize').click()
    editor.locator('[data-candidate="0"]').wait_for()
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/locate')).body")=={'start':0,'end':300,'seed_frame':0,'reset_at':''}
    editor.locator('#mosaicConfirm').click()
    assert '待跟踪' in editor.locator('#mosaicTargets').inner_text()
    assert not editor.evaluate("calls.some(c=>c.url.includes('/cuts/')||c.url.endsWith('/render'))")
    editor.locator('#mosaicClose').click()
    open_editor(editor)
    assert editor.locator('[data-target-view]').count()==1


def test_boxes_only_on_reference_frame_and_manual_adjustment(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    editor.locator('[data-candidate="0"]').wait_for()
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    editor.locator('#mosaicConfirm').click()
    editor.locator('#mosaicNext').click()
    assert editor.locator('#mosaicOverlay').is_hidden()
    editor.locator('[data-target-view]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('#mosaicBoxX').input_value()=='20'
    editor.locator('[data-target-remove]').click()
    assert editor.locator('[data-target-view]').count()==0


def test_empty_and_error_are_distinct_retryable_states(editor):
    open_editor(editor)
    editor.evaluate('window.findings=[]')
    editor.locator('#mosaicRecognize').click()
    editor.wait_for_function("document.getElementById('mosaicStatus').textContent.includes('未找到')")
    assert editor.locator('#mosaicConfirm').is_disabled()
    editor.evaluate("() => {window.api=async()=>{throw Error('识别暂不可用，请重试')}}")
    editor.locator('#mosaicRecognize').click()
    editor.wait_for_function("document.getElementById('mosaicStatus').textContent.includes('暂不可用')")
    assert editor.locator('#mosaicRecognize').is_enabled()


def test_late_model_response_does_not_follow_changed_frame(editor):
    open_editor(editor)
    editor.evaluate("() => {window.api=()=>new Promise(r=>window.finishLocate=r)}")
    editor.locator('#mosaicRecognize').click()
    editor.locator('#mosaicNext').click()
    editor.evaluate("finishLocate({id:'job',status:'completed',start:0,end:300,seed_frame:0,candidates:findings})")
    editor.wait_for_timeout(100)
    assert editor.locator('#mosaicOverlay').is_hidden()
    assert editor.locator('#mosaicConfirm').is_disabled()


def test_confirming_two_candidates_keeps_both_targets(editor):
    open_editor(editor)
    editor.evaluate("findings.push({...findings[0],id:'1',label:'另一区域',x:10,w:20})")
    editor.locator('#mosaicRecognize').click()
    editor.locator('[data-candidate="0"]').wait_for()
    editor.locator('#mosaicConfirm').click()
    editor.locator('[data-candidate="1"]').click()
    editor.locator('#mosaicConfirm').click()
    assert editor.locator('[data-target-view]').count()==2


def test_manual_draw_maps_contained_image_and_rejects_black_bars(editor):
    open_editor(editor)
    editor.locator('#mosaicDraw').click()
    bounds=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(bounds['x']+bounds['width']*.25,bounds['y']+bounds['height']*.25)
    editor.mouse.down()
    editor.mouse.move(bounds['x']+bounds['width']*.75,bounds['y']+bounds['height']*.75)
    editor.mouse.up()
    assert editor.locator('#mosaicBoxX').input_value()=='40'
    assert editor.locator('#mosaicBoxY').input_value()=='24'
    assert editor.locator('#mosaicBoxW').input_value()=='80'
    assert editor.locator('#mosaicBoxH').input_value()=='48'
    editor.locator('#mosaicConfirm').click()
    assert editor.locator('[data-target-view]').count()==1


def test_workspace_keeps_transport_and_footer_visible(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    editor.locator('#mosaicConfirm').click()
    workspace=editor.locator('#mosaicWorkspace').bounding_box()
    transport=editor.locator('.mosaic-transport').bounding_box()
    assert transport['y']+transport['height'] <= workspace['y']+workspace['height']
    sidebar=editor.locator('.mosaic-side').bounding_box()
    assert sidebar['y']+sidebar['height'] <= workspace['y']+workspace['height']
