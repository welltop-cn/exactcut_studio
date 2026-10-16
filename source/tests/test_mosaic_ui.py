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
          window.session={id:'session',source_sha256:'a'.repeat(64),source_filename:'source.mp4',metadata:{frame_count:300,fps_num:30,fps_den:1,width:160,height:96},capabilities:{region_recognition:true,render_stop:true},jobs:[]};
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


def confirm_region(page):
    if not page.locator('#mosaicConfirm').is_visible():
        page.locator('#mosaicAdjustToggle').click()
    page.locator('#mosaicConfirm').click()


def active_count(page):
    return page.evaluate("() => {const v3=Object.keys(localStorage).find(k=>k.includes('workbench.v3'));const old=Object.keys(localStorage).find(k=>k.includes('targets.v1'));const data=JSON.parse(localStorage.getItem(v3 || old) || '[]');return (data.targets || data).filter(t=>t.adopted!==false).length}")


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
    confirm_region(editor)
    assert '打码' in editor.locator('#mosaicTargets').inner_text()
    assert not editor.evaluate("calls.some(c=>c.url.includes('/cuts/')||(c.url.endsWith('/render')&&c.body))")
    editor.locator('#mosaicClose').click()
    open_editor(editor)
    assert editor.locator('[data-target-view]').count()==1


def test_boxes_only_on_reference_frame_and_manual_adjustment(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    editor.locator('[data-candidate="0"]').wait_for()
    if not editor.locator('#mosaicBoxX').is_visible():editor.locator('#mosaicAdjustToggle').click()
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    confirm_region(editor)
    if not editor.locator('#mosaicNext').is_visible():editor.locator('#mosaicAdjustToggle').click()
    editor.locator('#mosaicNext').click()
    assert editor.locator('#mosaicOverlay').is_hidden()
    editor.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('#mosaicBoxX').input_value()=='20'
    editor.locator('[data-target-remove]').click()
    assert editor.locator('[data-target-view]').count()==1
    assert editor.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'


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
    if not editor.locator('#mosaicNext').is_visible():editor.locator('#mosaicAdjustToggle').click()
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
    confirm_region(editor)
    editor.locator('#mosaicAdjustToggle').click()
    editor.locator('[data-candidate="1"]').click()
    confirm_region(editor)
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
    confirm_region(editor)
    assert editor.locator('[data-target-view]').count()==1


def test_workspace_keeps_transport_and_footer_visible(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    confirm_region(editor)
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
    page.wait_for_function("!document.getElementById('mosaicScan').disabled || !document.getElementById('mosaicStop').hidden")


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
    assert editor.locator('#mosaicConfirm').is_hidden()
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/scan')&&c.body).body")=={'reset_at':'','force':False}
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/locate'))")
    confirm_region(editor)
    assert editor.locator('[data-target-view]').count()==1
    assert editor.locator('[data-scan-marker]').count()==1


def test_unlocalized_segment_remains_for_review(editor):
    scan_result(editor,needs_review=True)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert '打码需框选' in editor.locator('#mosaicScanEvents').inner_text()
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
    confirm_region(editor)
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


def test_running_scan_shows_clickable_partial_results(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const original=api;
      window.api=async(url,body)=>url.endsWith('/scan')
        ? {enabled:true,report:{...scanJob,status:'running',progress:'正在定位第 2 个区间'}}
        : original(url,body);
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert '可先点击检查' in editor.locator('#mosaicScanStatus').inner_text()
    assert editor.locator('#mosaicScan').is_disabled()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    confirm_region(editor)
    assert editor.locator('[data-target-view]').count()==1


def test_wide_list_and_fixed_adopt_next(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:120})")
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.wait_for_function("!document.getElementById('mosaicConfirm').disabled")
    assert editor.locator('.mosaic-side').bounding_box()['width']>=400
    assert editor.locator('#mosaicReview').evaluate("e=>!!e.closest('.mosaic-main')")
    editor.locator('[data-proposal="e1:0"]').click()
    editor.locator('#mosaicNextEvent').click()
    assert editor.locator('[data-scan-event="e2"]').get_attribute('aria-pressed')=='true'
    assert editor.locator('[data-target-view]').count()==1
    assert editor.locator('#mosaicConfirmNext').is_hidden()


def test_adopted_regions_are_in_same_full_height_list(editor):
    scan_result(editor)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.wait_for_function("!document.getElementById('mosaicConfirm').disabled")
    confirm_region(editor)
    card=editor.locator('[data-mosaic-card="e1"]')
    assert '打码' in card.inner_text()
    assert card.locator('[data-target-view]').is_visible()
    assert editor.locator('.mosaic-side #mosaicAdjust').count()==0
    assert editor.locator('#mosaicSaved').count()==0
    card.locator('[data-target-view]').click()
    assert editor.locator('#mosaicConfirm').inner_text()=='保存调整'
    assert card.locator('[data-target-remove]').get_attribute('aria-pressed')=='true'
    assert card.bounding_box()['height']<=110
    card.locator('[data-target-remove]').click()
    assert editor.locator('[data-target-view]').count()==1
    assert card.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'


def test_toggle_retains_adjustment_across_reopen(editor):
    scan_result(editor)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    card=editor.locator('[data-mosaic-card="e1"]')
    assert card.get_by_role('button',name='打码',exact=True).is_visible()
    card.get_by_role('button',name='打码',exact=True).click()
    assert card.get_by_role('button',name='打码',exact=True).get_attribute('aria-pressed')=='true'
    card.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').click()
    if not editor.locator('#mosaicBoxX').is_visible():editor.locator('#mosaicAdjustToggle').click()
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    confirm_region(editor)
    card.get_by_role('button',name='打码',exact=True).click()
    assert card.get_by_role('button',name='打码',exact=True).get_attribute('aria-pressed')=='false'
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    row=editor.locator('[data-target-view]').locator('..')
    assert row.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'
    row.locator('[data-target-remove]').click()
    row.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').click()
    assert editor.locator('#mosaicBoxX').input_value()=='20'


def test_draft_storage_failure_rolls_back_checkbox(editor):
    scan_result(editor);open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.evaluate("() => {Storage.prototype.setItem=function(){throw Error('存储不可用')}}")
    editor.locator('[data-proposal]').click()
    assert editor.locator('[data-proposal]').get_attribute('aria-pressed')=='false'
    assert '存储不可用' in editor.locator('#mosaicStatus').inner_text()
    assert active_count(editor)==0


def test_legacy_draft_import_and_cross_page_conflict(editor):
    editor.evaluate('''() => {
      localStorage.setItem('exactcut.mosaic.targets.v1.fixture.batch.session.'+'a'.repeat(64)+'.',JSON.stringify([{...findings[0],start:30,end:90,seed_frame:60}]));
    }''')
    open_auto(editor)
    assert editor.locator('[data-target-remove]').get_attribute('aria-pressed')=='true'
    editor.locator('[data-target-remove]').click()
    assert editor.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'
    editor.evaluate('''() => {
      const key=Object.keys(localStorage).find(k=>k.includes('workbench.v3'));
      const data=JSON.parse(localStorage.getItem(key));data.targets[0].x=20;localStorage.setItem(key,JSON.stringify(data));
    }''')
    editor.locator('[data-target-remove]').click()
    assert '其他页面更新' in editor.locator('#mosaicStatus').inner_text()
    assert editor.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'
    assert editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3')))).targets[0].x")==20


def test_compact_review_prioritizes_preview_and_keeps_actions_fixed(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    before=editor.locator('#mosaicStage').bounding_box()
    assert editor.locator('#mosaicReview').bounding_box()['height']<90
    editor.locator('#mosaicAdjustToggle').click()
    assert before['height'] >= 350
    assert editor.locator('.mosaic-header').bounding_box()['height'] <= 60
    assert editor.locator('#mosaicStage').bounding_box()['height'] >= 200
    assert editor.locator('#mosaicNextEvent').is_visible()


def test_multiple_regions_toggle_together_and_keep_saved_geometry(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events[0].candidates.push({...findings[0],id:'1',x:10,w:20,label:'区域二'})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    toggle=editor.locator('[data-event-adopt="e1"]')
    assert toggle.count()==1
    assert toggle.inner_text()=='打码'
    toggle.click()
    assert active_count(editor)==2
    assert toggle.inner_text()=='打码'
    toggle.click()
    assert active_count(editor)==0
    assert toggle.inner_text()=='打码'
    toggle.click()
    assert active_count(editor)==2
    assert editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3')))).targets.length")==2


def test_missing_box_offers_action_instead_of_disabled_adopted(editor):
    scan_result(editor,needs_review=True);open_auto(editor);editor.locator('#mosaicScan').click()
    card=editor.locator('[data-mosaic-card="e1"]')
    assert card.locator('input[type=checkbox]').count()==0
    card.locator('[data-event-draw]').click()
    assert editor.locator('#mosaicDraw').inner_text()=='在画面拖出矩形'


def test_unchecked_saved_region_label(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.locator('[data-target-remove]').click()
    assert editor.locator('[data-target-remove]').inner_text()=='打码'


def test_edit_time_keeps_record_in_same_card(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click();editor.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').click()
    editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicStart').dispatch_event('change')
    confirm_region(editor)
    assert editor.locator('[data-mosaic-card="e1"] [data-target-view]').count()==1
    assert active_count(editor)==1


def test_eight_active_limit_allows_cancel_and_restore(editor):
    editor.evaluate('''() => {
      localStorage.setItem('exactcut.mosaic.targets.v1.fixture.batch.session.'+'a'.repeat(64)+'.',JSON.stringify(Array.from({length:8},(_,i)=>({...findings[0],start:i,end:20,seed_frame:10}))));
    }''')
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('[data-proposal]').is_disabled()
    editor.locator('[data-target-remove="0"]').click()
    assert editor.locator('[data-proposal]').is_enabled()
    editor.locator('[data-proposal]').click()
    assert editor.locator('[data-target-remove="0"]').is_disabled()
    editor.locator('[data-target-remove="8"]').click()
    editor.locator('[data-target-remove="0"]').click()
    assert active_count(editor)==8


def test_checkbox_then_confirm_does_not_duplicate_region(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.wait_for_function("!document.getElementById('mosaicConfirm').disabled")
    editor.locator('[data-proposal]').click();confirm_region(editor)
    assert active_count(editor)==1


def test_tracking_preview_uses_saved_region_without_changing_adoption(editor):
    editor.evaluate('''() => {
      session.capabilities.tracking=true;
      const old=window.api;
      window.api=async(url,body)=>{
        if(url.endsWith('/track')){window.trackBody=body;return {id:'a'.repeat(32),status:'completed',result:{warnings:[],elapsed_seconds:2}};}
        return old(url,body);
      };
    }''')
    open_editor(editor);editor.locator('#mosaicRecognize').click()
    confirm_region(editor)
    editor.locator('#mosaicAdjustToggle').click()
    editor.locator('#mosaicTrack').click()
    assert editor.locator('#mosaicTrackDialog').evaluate('(el)=>el.open')
    assert editor.locator('#mosaicTrackVideo').is_visible()
    assert editor.evaluate('trackBody.target')=={'start':0,'end':300,'seed_frame':0,'x':40,'y':24,'w':80,'h':48}
    editor.locator('#mosaicTrackClose').click()
    assert active_count(editor)==1


def test_complete_video_submits_all_adopted_regions_and_restores_download(editor):
    editor.evaluate('''() => {
      session.capabilities.tracking=true;
      localStorage.setItem('exactcut.mosaic.targets.v1.fixture.batch.session.'+'a'.repeat(64)+'.',JSON.stringify([
        {...findings[0],start:10,end:20,seed_frame:15},
        {...findings[0],x:10,start:30,end:40,seed_frame:35},
        {...findings[0],start:50,end:60,seed_frame:55,adopted:false}
      ]));
      const old=api;window.batchJob=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){
          if(body){window.batchBody=body;batchJob={id:'b'.repeat(32),status:'completed',targets:body.targets,result:{skipped_frames:2,warnings:[{region:1,frame:10,reason:"失跟"}],region_count:2},completed_at:'now'};return batchJob;}
          return {job:batchJob};
        }
        return old(url,body);
      };
    }''')
    open_auto(editor)
    button=editor.locator('#mosaicBatchStart')
    assert button.is_visible() and button.is_enabled()
    assert editor.locator('#mosaicBatchHint').inner_text()=='删除 0 段 · 打码 2 段'
    button.click()
    assert editor.locator('#mosaicBatchDialog').is_visible()
    assert editor.evaluate('batchBody.targets.length')==2
    assert editor.evaluate('batchBody.targets.map(t=>t.start)')==[10,30]
    assert editor.locator('#mosaicBatchDownload').is_visible()
    assert editor.locator('#mosaicBatchVideo').is_visible()
    assert editor.locator('#mosaicBatchStatus').inner_text()=='完整视频已生成，可预览或下载。'
    assert '失跟' not in editor.locator('#mosaicBatchDialog').inner_text()
    editor.locator('#mosaicBatchClose').click();editor.locator('#mosaicClose').click()
    open_auto(editor)
    editor.locator('#mosaicBatchView').wait_for()
    editor.locator('#mosaicBatchView').click()
    assert editor.locator('#mosaicBatchDownload').is_visible()
    assert active_count(editor)==2


def test_completed_clip_preview_available_after_unadopting_without_retracking(editor):
    editor.evaluate('''() => {
      session.capabilities.tracking=true;
      const target={...findings[0],start:30,end:60,seed_frame:40,adopted:false};
      localStorage.setItem('exactcut.mosaic.targets.v1.fixture.batch.session.'+'a'.repeat(64)+'.',JSON.stringify([target]));
      const old=api;window.api=async(url,body)=>{
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),status:'completed',targets:[target],result:{region_count:1},completed_at:'now'}};
        return old(url,body);
      };
    }''')
    open_auto(editor)
    button=editor.locator('#mosaicClipPreview')
    assert button.is_visible() and not button.is_enabled()
    editor.locator('[data-target-view="0"]').click()
    assert button.is_enabled()
    button.click()
    assert editor.locator('#mosaicTrackDialog').evaluate('(el)=>el.open')
    assert '/render/' in editor.locator('#mosaicTrackVideo').get_attribute('src')
    assert '00:01:00–00:01:29' in editor.locator('#mosaicTrackStatus').inner_text()
    assert '上次生成结果' in editor.locator('#mosaicTrackHint').inner_text()
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/track'))")
    assert active_count(editor)==0
    editor.locator('#mosaicTrackVideo').evaluate("el=>el.dispatchEvent(new Event('loadedmetadata'))")
    assert editor.locator('#mosaicTrackVideo').evaluate('el=>el.currentTime')==1
    editor.locator('#mosaicTrackClose').click()
    assert not editor.locator('#mosaicTrackDialog').evaluate('(el)=>el.open')


def test_unified_delete_mosaic_choice_persists_and_discard_resets(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      session.capabilities.tracking=true;
      const old=api;window.planRequests=[];
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){if(!body)return {job:null};planRequests.push(body);return {id:'b'.repeat(32),status:'completed',targets:body.targets,ranges:body.ranges,result:{region_count:body.targets.length},completed_at:'now'};}
        if(url.endsWith('/scan'))return body?scanJob:{enabled:true,report:scanJob};
        if(url.endsWith('/discard')){session.reset_at='reset1';scanJob.reset_at='reset1';return session;}
        return old(url,body);
      };
    }''')
    open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-delete="e1"]').click()
    assert editor.locator('[data-event-delete="e1"]').get_attribute('aria-pressed')=='true'
    editor.locator('#mosaicClose').click();open_auto(editor)
    assert editor.locator('[data-event-delete="e1"]').get_attribute('aria-pressed')=='true'
    editor.locator('[data-proposal]').click()
    assert editor.locator('[data-event-delete="e1"]').get_attribute('aria-pressed')=='false'
    editor.locator('[data-event-delete="e1"]').click()
    assert editor.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('planRequests[0].targets')==[]
    assert editor.evaluate('planRequests[0].ranges')==[[30,90]]
    editor.locator('#mosaicBatchClose').click()
    editor.once('dialog',lambda d:d.accept())
    editor.locator('#mosaicDiscard').click()
    editor.wait_for_function("!document.getElementById('mosaicDialog').open")
    open_auto(editor)
    assert editor.locator('[data-event-delete="e1"]').get_attribute('aria-pressed')=='false'
    assert editor.locator('#mosaicBatchStart').is_disabled()
    assert editor.locator('[data-scan-event="e1"]').count()==1


def test_generated_result_replace_and_compare_use_saved_plan(editor):
    editor.evaluate('''() => {
      session.capabilities.tracking=true;
      const target={...findings[0],start:60,end:90,seed_frame:70,adopted:true};
      localStorage.setItem('exactcut.mosaic.workbench.v3.fixture.batch.session.'+'a'.repeat(64)+'.',JSON.stringify({schema_version:1,targets:[target],deletions:[{start:0,end:30}]}));
      const old=api;window.replaced=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),status:'completed',targets:[target],ranges:[[0,30]],result:{region_count:1},completed_at:'now'}};
        if(url.endsWith('/replace')){replaced=body;return {replacement:{status:'completed'}};}
        if(url==='/api/batches/batch')return current;
        return old(url,body);
      };window.renderRuntime=()=>{};window.toast=()=>{};
    }''')
    open_auto(editor)
    editor.locator('[data-target-view]').click()
    editor.locator('#mosaicClipPreview').click()
    editor.locator('#mosaicTrackVideo').evaluate("el=>el.dispatchEvent(new Event('loadedmetadata'))")
    assert editor.locator('#mosaicTrackVideo').evaluate('el=>el.currentTime')==1
    editor.locator('#mosaicTrackClose').click()
    editor.locator('#mosaicBatchView').click()
    assert editor.locator('#mosaicReplace').is_enabled()
    editor.locator('#mosaicBatchVideo').evaluate('el=>el.currentTime=1')
    editor.locator('#mosaicBatchBefore').click()
    editor.locator('#mosaicBatchVideo').evaluate("el=>el.dispatchEvent(new Event('loadedmetadata'))")
    assert editor.locator('#mosaicBatchVideo').get_attribute('src').endswith('/source')
    assert editor.locator('#mosaicBatchVideo').evaluate('el=>el.currentTime')==2
    editor.locator('#mosaicBatchAfter').click()
    editor.locator('#mosaicBatchVideo').evaluate("el=>el.dispatchEvent(new Event('loadedmetadata'))")
    assert editor.locator('#mosaicBatchVideo').evaluate('el=>el.currentTime')==1
    editor.locator('#mosaicBatchClose').click()
    editor.locator('[data-target-remove]').click()
    editor.locator('#mosaicBatchView').click()
    assert editor.locator('#mosaicReplace').is_disabled()
    assert '变化' in editor.locator('#mosaicBatchVersion').inner_text()
    editor.locator('#mosaicBatchClose').click()
    editor.locator('[data-target-remove]').click()
    editor.locator('#mosaicBatchView').click()
    editor.once('dialog',lambda d:d.accept())
    editor.locator('#mosaicReplace').click()
    editor.wait_for_function("!document.getElementById('mosaicDialog').open")
    assert editor.evaluate('replaced')=={'job_id':'b'*32,'reset_at':''}
    assert editor.evaluate("Object.keys(localStorage).filter(k=>k.includes('workbench.v3')).length")==0


def test_processing_decisions_are_equal_toggle_buttons_and_tools_are_compact(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    card=editor.locator('[data-mosaic-card="e1"]')
    delete=card.get_by_role('button',name='删除',exact=True)
    mosaic=card.get_by_role('button',name='打码',exact=True)
    assert card.locator('input[type=checkbox]').count()==0
    assert delete.bounding_box()['width']==mosaic.bounding_box()['width']
    assert delete.bounding_box()['height']==mosaic.bounding_box()['height']
    mosaic.click();assert card.get_by_role('button',name='打码',exact=True).get_attribute('aria-pressed')=='true'
    delete.click();assert delete.get_attribute('aria-pressed')=='true'
    assert card.get_by_role('button',name='打码',exact=True).get_attribute('aria-pressed')=='false'
    delete.click();assert delete.get_attribute('aria-pressed')=='false'
    card.locator('[data-scan-event]').click()
    assert editor.locator('#mosaicConfirm').is_hidden()
    assert editor.locator('#mosaicConfirmNext').is_hidden()
    assert editor.locator('#mosaicCandidates').is_hidden()
    assert editor.locator('#mosaicReview').bounding_box()['height']<90
    editor.locator('#mosaicAdjustToggle').click()
    assert editor.locator('#mosaicConfirm').is_visible()


def test_processing_keeps_workspace_and_shows_background_progress(editor):
    editor.evaluate('''() => {
      const old=api;window.api=async(url,body)=>{
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),status:'running',progress:'正在跟踪第 1 / 6 个区域',targets:[]}};
        if(url.includes('/render/'))return new Promise(()=>{});
        return old(url,body);
      };
    }''')
    open_auto(editor)
    editor.locator('#mosaicProcessing').wait_for()
    assert editor.locator('#mosaicWorkspace').is_visible()
    assert editor.locator('#mosaicStage').is_visible()
    assert editor.locator('#mosaicBatchDialog').is_hidden()
    assert editor.locator('#mosaicStatus').is_hidden()
    assert '1 / 6' in editor.locator('#mosaicProcessingText').inner_text()
    assert '关闭后后台继续' in editor.locator('#mosaicProcessing').inner_text()
    assert editor.locator('#mosaicBatchView').is_hidden()
    assert editor.locator('#mosaicWorkspace').is_visible()
    assert editor.locator('#mosaicBatchClose').is_hidden()
    assert editor.locator('#mosaicClose').is_enabled()


def test_effect_settings_collapsed_persist_and_submit(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    assert not editor.locator('#mosaicEffects').evaluate('el=>el.open')
    editor.locator('#mosaicEffects summary').click()
    editor.locator('#mosaicEffectStyle').select_option('solid')
    assert editor.locator('#mosaicEffectStrength').is_hidden()
    editor.locator('#mosaicEffectExpansion').select_option('large')
    editor.locator('#mosaicClose').click();open_auto(editor)
    assert editor.locator('#mosaicEffectStyle').input_value()=='solid'
    assert editor.locator('#mosaicEffectExpansion').input_value()=='large'
    assert '纯色' in editor.locator('#mosaicEffectSummary').inner_text()
    editor.locator('#mosaicScan').click();editor.locator('[data-proposal]').click()
    editor.evaluate('''() => {session.capabilities.tracking=true;const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.effectBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}''')
    editor.locator('[data-target-remove]').click();editor.locator('[data-target-remove]').click()
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('effectBody.effect')=={'style':'solid','strength':'medium','expansion':'large'}


def test_generation_locks_edits_until_stop_acknowledged(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const old=api;window.stopped=false;
      window.api=async(url,body)=>{
        if(url.endsWith('/stop')){window.stopBody=body;stopped=true;return {id:'b'.repeat(32),status:'running',cancel_requested:true,targets:[]};}
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),status:'running',targets:[]}};
        if(url.includes('/render/'))return stopped?new Promise(r=>window.finishStop=r):{id:'b'.repeat(32),status:'running',targets:[]};
        if(url.endsWith('/scan')&&!body)return {enabled:true,report:scanJob};
        return old(url,body);
      };
    }''')
    open_auto(editor);editor.locator('#mosaicProcessing').wait_for()
    assert editor.locator('[data-event-delete]').is_disabled()
    assert editor.locator('[data-proposal]').is_disabled()
    assert editor.locator('#mosaicClear').is_disabled()
    assert editor.locator('#mosaicEffectStyle').is_disabled()
    assert editor.locator('#mosaicAdjustToggle').is_disabled()
    assert editor.locator('#mosaicDiscard').is_hidden()
    assert editor.locator('#mosaicPlay').is_enabled()
    editor.locator('#mosaicStop').click()
    assert editor.locator('[data-event-delete]').is_disabled()
    assert editor.locator('#mosaicStop').is_disabled()
    editor.wait_for_function('!!window.finishStop')
    editor.evaluate("finishStop({id:'b'.repeat(32),status:'cancelled',targets:[]})")
    editor.wait_for_function("!document.querySelector('[data-event-delete]').disabled")
    assert editor.locator('#mosaicEffectStyle').is_enabled()
    assert editor.locator('#mosaicStop').is_hidden()
    assert editor.evaluate('stopBody.job_id')=='b'*32
