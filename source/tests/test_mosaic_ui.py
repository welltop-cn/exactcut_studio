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
            if(url.endsWith('/scan'))return {enabled:true,report:null};
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
    page.locator('#mosaicAdjust').evaluate('(el)=>el.open=true')
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


def scan_result(page, *, needs_review=False):
    page.evaluate('''needs => {
      window.scanJob={id:'scan1',status:'completed',source_sha256:session.source_sha256,reset_at:'',warnings:[],events:[
        {id:'e1',start:30,end:90,seed_frame:needs?null:60,status:needs?'needs_review':'localized',label:'疑似敏感画面',reason:'画面证据',review_note:needs?'自动定位未完成，请复核':'',candidates:needs?[]:findings}
      ]};
      const original=api;
      window.api=async(url,body)=>{
        if(url.endsWith('/scan')){calls.push({url,body});return body?scanJob:{enabled:true,report:null};}
        return original(url,body);
      };
    }''', needs_review)


def open_auto(page):
    page.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    page.locator('#mosaicScan').wait_for(state='visible')
    page.wait_for_function("!document.getElementById('mosaicScan').disabled")


def test_scan_finds_range_and_region_without_manual_frame_selection(editor):
    scan_result(editor)
    open_auto(editor)
    assert editor.locator('#mosaicStart').is_hidden()
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').wait_for()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('#mosaicStart').input_value()=='00:01:00'
    assert editor.locator('#mosaicEnd').input_value()=='00:02:29'
    assert editor.locator('#mosaicFrameNumber').input_value()=='00:02:00'
    assert not editor.locator('#mosaicAdjust').evaluate('(el)=>el.open')
    assert editor.locator('#mosaicConfirm').is_visible()
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/scan')&&c.body).body")=={'reset_at':'','force':False}
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/locate'))")
    editor.locator('#mosaicConfirm').click()
    assert editor.locator('[data-target-view]').count()==1
    assert editor.locator('[data-scan-marker]').count()==1


def test_unlocalized_segment_remains_for_review(editor):
    scan_result(editor,needs_review=True)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert '需复核' in editor.locator('#mosaicScanEvents').inner_text()
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('#mosaicStart').input_value()=='00:01:00'
    assert editor.locator('#mosaicConfirm').is_disabled()
    assert editor.locator('#mosaicCandidates').inner_text()==''
    assert '未完成' in editor.locator('#mosaicStatus').inner_text()


def test_scan_resumes_running_job_on_reopen(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const original=api;window.scanReads=0;
      window.api=async(url,body)=>{
        if(url.endsWith('/scan')){calls.push({url,body});scanReads++;return {enabled:true,report:scanReads===1?{id:'scan1',status:'running',progress:'正在定位区域'}:scanJob};}
        return original(url,body);
      };
    }''')
    open_auto(editor)
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/scan')&&c.body)")


def test_failed_scan_is_not_reported_as_empty_and_retry_preserves_targets(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    editor.locator('#mosaicConfirm').click()
    scan_result(editor)
    editor.evaluate("scanJob={id:'scan1',status:'failed',error:'整段扫描失败，请重试'}")
    editor.locator('#mosaicScan').click()
    editor.wait_for_function("document.getElementById('mosaicScanStatus').textContent.includes('失败')")
    assert '未发现' not in editor.locator('#mosaicScanStatus').inner_text()
    assert editor.locator('[data-target-view]').count()==1
    scan_result(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert editor.locator('[data-target-view]').count()==1


def test_completed_empty_scan_is_explicit(editor):
    scan_result(editor)
    editor.evaluate('scanJob.events=[]')
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.wait_for_function("document.getElementById('mosaicScanStatus').textContent.includes('抽样未发现')")
    assert editor.locator('[data-scan-event]').count()==0
    assert editor.locator('#mosaicConfirm').is_disabled()


def test_late_whole_scan_cannot_overwrite_reopened_results(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const original=api;window.reopened=false;
      window.api=async(url,body)=>{
        if(url.endsWith('/scan')&&body)return new Promise(r=>window.finishScan=r);
        if(url.endsWith('/scan')&&reopened)return {enabled:true,report:{...scanJob,id:'scan2',events:[{...scanJob.events[0],label:'新任务结果'}]}};
        return original(url,body);
      };
    }''')
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('#mosaicClose').click()
    editor.evaluate('window.reopened=true')
    open_auto(editor)
    editor.locator('[data-scan-event="e1"]').wait_for()
    editor.evaluate('finishScan(scanJob)')
    editor.wait_for_timeout(100)
    assert '新任务结果' in editor.locator('#mosaicScanEvents').inner_text()


def test_reopen_while_source_preparing_shares_request(editor):
    editor.evaluate('''() => {
      const original=window.api;
      window.api=(url,body)=>url.endsWith('/open') ? new Promise((resolve,reject)=>{
        calls.push({url,body});window.finishOpen=()=>resolve(session);window.failOpen=reject;
      }) : original(url,body);
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('#mosaicClose').click()
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    assert editor.evaluate("calls.filter(c=>c.url.endsWith('/open')).length")==1
    assert not editor.locator('#mosaicStatus').evaluate("e=>e.classList.contains('error')")
    editor.evaluate('finishOpen()')
    editor.locator('#mosaicWorkspace').wait_for(state='visible')
    assert editor.evaluate("calls.filter(c=>c.url.endsWith('/scan')).length")==1


def test_preparing_failure_can_retry_and_does_not_reopen_closed_dialog(editor):
    editor.evaluate('''() => {
      const original=window.api;
      window.api=(url,body)=>url.endsWith('/open') ? new Promise((resolve,reject)=>{
        calls.push({url,body});window.finishOpen=()=>resolve(session);window.failOpen=()=>reject(Error('初始化失败'));
      }) : original(url,body);
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('#mosaicClose').click()
    editor.evaluate('failOpen()')
    assert not editor.locator('#mosaicDialog').evaluate('e=>e.open')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    assert editor.evaluate("calls.filter(c=>c.url.endsWith('/open')).length")==2
    editor.evaluate('finishOpen()')
    editor.locator('#mosaicWorkspace').wait_for(state='visible')
