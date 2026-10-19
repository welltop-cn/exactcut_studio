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
            if(url.endsWith('/scan'))return {enabled:true,report:{id:'existing',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]}};
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
        page.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    page.locator('#mosaicConfirm').click()


def active_count(page):
    return page.evaluate("() => {const v3=Object.keys(localStorage).find(k=>k.includes('workbench.v3'));const old=Object.keys(localStorage).find(k=>k.includes('targets.v1'));const data=JSON.parse(localStorage.getItem(v3 || old) || '[]');return (data.targets || data).filter(t=>t.adopted!==false).length}")


def open_editor(page):
    page.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    page.locator('#mosaicAdjust').evaluate('(el)=>el.open=true')
    page.locator('#mosaicMoreTools').evaluate('(el)=>el.open=true')
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
    editor.locator('#mosaicMoreTools').evaluate('(el)=>el.open=true')
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    confirm_region(editor)
    if not editor.locator('#mosaicNext').is_visible():editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicNext').click()
    assert editor.locator('#mosaicOverlay').is_hidden()
    editor.locator('#mosaicAdjust > summary').click()
    editor.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('#mosaicBoxX').input_value()=='20'
    editor.locator('#mosaicAdjust > summary').click()
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
    if not editor.locator('#mosaicNext').is_visible():editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
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
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('[data-candidate="1"]').click()
    confirm_region(editor)
    assert editor.locator('[data-target-view]').count()==2


def test_manual_draw_maps_contained_image_and_rejects_black_bars(editor):
    open_editor(editor)
    editor.locator('#mosaicAddRegion').click()
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
    assert editor.locator('#mosaicConfirm').is_enabled()  # Time can be saved without a mask.
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
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.wait_for_function('typeof finishScan === "function"')
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


def test_running_scan_allows_view_but_locks_editing(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const original=api;
      window.api=async(url,body)=>url.endsWith('/scan')
        ? {enabled:true,report:{...scanJob,status:'running',progress:'正在定位第 2 个区间'}}
        : original(url,body);
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert '完成后可继续编辑' in editor.locator('#mosaicScanStatus').inner_text()
    assert editor.locator('#mosaicScan').is_disabled()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('[data-proposal]').is_disabled()
    assert editor.locator('#mosaicConfirm').is_disabled()


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
    assert editor.locator('.mosaic-side #mosaicAdjust').is_hidden()
    assert editor.locator('#mosaicSaved').count()==0
    card.locator('[data-target-view]').click()
    assert editor.locator('#mosaicConfirm').inner_text()=='保存修改'
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
    assert card.get_by_role('checkbox',name='打码',exact=True).is_visible()
    card.get_by_role('checkbox',name='打码',exact=True).click()
    assert card.get_by_role('checkbox',name='打码',exact=True).get_attribute('aria-pressed')=='true'
    card.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicMoreTools').evaluate('(el)=>el.open=true')
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    confirm_region(editor)
    card.get_by_role('checkbox',name='打码',exact=True).click()
    assert card.get_by_role('checkbox',name='打码',exact=True).get_attribute('aria-pressed')=='false'
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    row=editor.locator('[data-target-view]').locator('..')
    assert row.locator('[data-target-remove]').get_attribute('aria-pressed')=='false'
    row.locator('[data-target-remove]').click()
    row.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
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
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
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
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
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
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicTrack').evaluate('(el)=>el.click()')
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
    delete=card.get_by_role('checkbox',name='删除',exact=True)
    mosaic=card.get_by_role('checkbox',name='打码',exact=True)
    assert card.locator('input[type=checkbox]').count()==0
    assert delete.bounding_box()['width']==mosaic.bounding_box()['width']
    assert delete.bounding_box()['height']==mosaic.bounding_box()['height']
    mosaic.click();assert card.get_by_role('checkbox',name='打码',exact=True).get_attribute('aria-pressed')=='true'
    delete.click();assert delete.get_attribute('aria-pressed')=='true'
    assert card.get_by_role('checkbox',name='打码',exact=True).get_attribute('aria-pressed')=='false'
    delete.click();assert delete.get_attribute('aria-pressed')=='false'
    card.locator('[data-scan-event]').click()
    assert editor.locator('#mosaicConfirm').is_hidden()
    assert editor.locator('#mosaicConfirmNext').is_hidden()
    assert editor.locator('#mosaicCandidates').is_hidden()
    assert editor.locator('#mosaicReview').bounding_box()['height']<90
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
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


def test_effect_popup_is_clip_local_and_cancel_discards(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('[data-effect-target]').count()==0
    editor.locator('[data-proposal]').click()
    editor.locator('[data-effect-target]').click()
    assert '00:01:00' in editor.locator('#mosaicEffectTitle').inner_text()
    assert editor.locator('#mosaicEffects').bounding_box()['height']<350
    editor.locator('#mosaicEffectStyle').select_option('solid')
    editor.locator('#mosaicEffectCancel').click()
    editor.locator('[data-effect-target]').click()
    assert editor.locator('#mosaicEffectStyle').input_value()=='pixel'
    editor.locator('#mosaicEffectStyle').select_option('soft_blur')
    editor.keyboard.press('Escape')
    editor.locator('[data-effect-target]').click()
    assert editor.locator('#mosaicEffectStyle').input_value()=='pixel'
    editor.locator('#mosaicEffectSave').click()



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


def test_clip_effects_are_independent_persist_and_submit(editor):
    editor.evaluate('session.capabilities.tracking=true')
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:120})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    for event,style in [('e1','soft_blur'),('e2','blur')]:
        editor.locator(f'[data-proposal="{event}:0"]').click()
        editor.locator(f'[data-mosaic-card="{event}"] [data-effect-target]').click()
        editor.locator('#mosaicEffectStyle').select_option(style)
        editor.locator('#mosaicEffectSave').click()
    editor.locator('#mosaicClose').click();open_auto(editor);editor.locator('#mosaicScan').click()
    for event,style in [('e1','soft_blur'),('e2','blur')]:
        editor.locator(f'[data-mosaic-card="{event}"] [data-effect-target]').click()
        assert editor.locator('#mosaicEffectStyle').input_value()==style
        editor.locator('#mosaicEffectCancel').click()
    editor.evaluate("""() => {session.capabilities.tracking=true;const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.effectBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('effectBody.targets.map(t=>t.effect.style)')==['soft_blur','blur']


def test_effect_preview_sends_all_clip_regions_without_saving(editor):
    scan_result(editor)
    editor.evaluate("""() => {
      session.capabilities.tracking=true;
      scanJob.events[0].candidates.push({...findings[0],id:'second',x:10,w:20});
      scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:120,candidates:[findings[0]]});
      const old=api;window.api=async(url,body)=>{
        if(url.endsWith('/clip-preview')){window.previewBody=body;return {id:'c'.repeat(32),status:'completed',preview:true,result:{},completed_at:'now'};}
        return old(url,body);
      };
    }""")
    open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('#mosaicClipPreview').count()==0
    editor.locator('[data-event-adopt="e1"]').click()
    editor.locator('[data-proposal="e2:0"]').click()
    editor.locator('[data-mosaic-card="e1"] [data-effect-target]').click()
    editor.locator('#mosaicEffectStyle').select_option('soft_blur')
    editor.locator('#mosaicEffectPreview').click()
    editor.locator('#mosaicTrackDialog').wait_for()
    assert editor.evaluate('previewBody.targets.length')==2
    assert editor.evaluate('previewBody.targets.every(t=>t.start===30&&t.effect.style==="soft_blur")')
    assert '/render/' in editor.locator('#mosaicTrackVideo').get_attribute('src')
    editor.locator('#mosaicTrackClose').click()
    assert editor.locator('#mosaicEffects').is_visible()
    editor.locator('#mosaicEffectCancel').click()
    editor.locator('[data-mosaic-card="e1"] [data-effect-target]').click()
    assert editor.locator('#mosaicEffectStyle').input_value()=='pixel'


def test_closing_running_clip_preview_stops_without_discarding_effect_draft(editor):
    scan_result(editor)
    editor.evaluate("""() => {
      session.capabilities.tracking=true;window.previewStopped=false;
      const old=api;window.api=async(url,body)=>{
        if(url.endsWith('/clip-preview'))return {id:'c'.repeat(32),status:'running',preview:true};
        if(url.endsWith('/stop')){window.stopPreviewBody=body;previewStopped=true;return {id:'c'.repeat(32),status:'cancelled'};}
        if(url.endsWith('/render/'+'c'.repeat(32)))return {id:'c'.repeat(32),status:previewStopped?'cancelled':'running'};
        return old(url,body);
      };
    }""")
    open_auto(editor);editor.locator('#mosaicScan').click();editor.locator('[data-proposal]').click()
    editor.locator('[data-effect-target]').click();editor.locator('#mosaicEffectStyle').select_option('soft_blur')
    editor.locator('#mosaicEffectPreview').click()
    assert editor.locator('#mosaicEffectSave').is_disabled()
    assert editor.locator('#mosaicTrackDialog').bounding_box()['height']>=700
    editor.locator('#mosaicTrackClose').click()
    editor.wait_for_function("!document.querySelector('#mosaicEffectSave').disabled")
    assert editor.evaluate('stopPreviewBody.job_id')=='c'*32
    assert editor.locator('#mosaicEffectStyle').input_value()=='soft_blur'


def test_region_can_move_resize_edges_and_stay_inside_frame(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    editor.locator('[data-candidate="0"]').click()
    def values():return [int(editor.locator('#mosaicBox'+axis).input_value()) for axis in 'XYWH']
    def drag(locator,dx,dy):
        b=locator.bounding_box();g=editor.locator('#mosaicOverlay').bounding_box()
        x=b['x']+b['width']/2;y=b['y']+b['height']/2
        editor.mouse.move(x,y);editor.mouse.down()
        editor.mouse.move(x+dx*g['width']/160,y+dy*g['height']/96,steps=3);editor.mouse.up()
    drag(editor.locator('.mosaic-box.selected'),10,5)
    assert values()==[50,29,80,48]
    for edge,dx,dy,expected in [('w',-10,0,[40,29,90,48]),('e',10,0,[40,29,100,48]),('n',0,-5,[40,24,100,53]),('s',0,5,[40,24,100,58])]:
        drag(editor.locator('[data-resize="'+edge+'"]'),dx,dy)
        assert values()==expected
    drag(editor.locator('[data-resize="se"]'),5,5)
    assert values()==[40,24,105,63]
    drag(editor.locator('.mosaic-box.selected'),200,200)
    assert values()==[55,33,105,63]
    confirm_region(editor)
    editor.locator('[data-target-view]').click()
    assert values()==[55,33,105,63]


def test_adjust_sidebar_does_not_shrink_preview(editor):
    open_editor(editor)
    editor.locator('#mosaicAdjust > summary').click()
    before=editor.locator('#mosaicStage').bounding_box()
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    after=editor.locator('#mosaicStage').bounding_box()
    assert abs(before['height']-after['height'])<1
    assert editor.locator('#mosaicAdjust').bounding_box()['x']>after['x']+after['width']
    assert editor.locator('.mosaic-scan-section').is_hidden()
    assert editor.locator('#mosaicConfirm').is_visible()
    editor.locator('#mosaicAdjust > summary').click()
    assert editor.locator('.mosaic-scan-section').is_visible()


def test_manual_delete_and_mosaic_without_ai_persist(editor):
    open_auto(editor)
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicStart').fill('00:00:00')
    editor.locator('#mosaicEnd').fill('00:01:29')
    editor.locator('#mosaicAddSubmit').click()
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('[data-event-delete]').click()
    assert '删除 0 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('[data-event-delete]').click()
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic')
    assert editor.locator('#mosaicAddSubmit').is_disabled()
    editor.locator('#mosaicAddRegion').click()
    b=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(b['x']+b['width']*.2,b['y']+b['height']*.2);editor.mouse.down()
    editor.mouse.move(b['x']+b['width']*.6,b['y']+b['height']*.6);editor.mouse.up()
    editor.locator('#mosaicAddSubmit').click()
    assert '打码 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert editor.locator('[data-mosaic-card]').count()==2
    editor.locator('#mosaicClose').click();open_auto(editor)
    assert editor.locator('[data-mosaic-card]').count()==2
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert '打码 1 段' in editor.locator('#mosaicBatchHint').inner_text()


def test_rescan_keeps_saved_clip_even_after_deselect(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.evaluate("scanJob.events[0]={...scanJob.events[0],start:100,end:160,seed_frame:120}")
    editor.locator('#mosaicScan').click()
    assert editor.locator('[data-target-view]').count()==1
    editor.locator('[data-target-remove]').click()
    assert editor.locator('[data-target-view]').count()==1
    assert editor.locator('[data-scan-event]').count()==2
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert len(draft['targets'])==1 and draft['targets'][0]['adopted'] is False


def test_remove_manual_clip_undo_and_reopen(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('[data-remove-clip]').count()==0
    editor.locator('#mosaicAddClip').click();editor.locator('#mosaicAddSubmit').click()
    editor.locator('[data-remove-clip]').click()
    assert editor.locator('[data-remove-clip]').count()==0
    assert '删除 0 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('#mosaicUndoRemove').click()
    assert editor.locator('[data-remove-clip]').count()==1
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('[data-remove-clip]').click()
    editor.locator('#mosaicClose').click();open_auto(editor)
    assert editor.locator('[data-remove-clip]').count()==0
    assert editor.locator('#mosaicRemoved').is_hidden()


def test_changed_start_outside_reference_frame_can_save_and_updates_card(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:02:10')
    editor.locator('#mosaicStart').dispatch_event('change')
    editor.wait_for_function("!document.querySelector('#mosaicConfirm').disabled")
    assert editor.locator('#mosaicFrameNumber').input_value()=='00:02:10'
    editor.locator('#mosaicConfirm').click()
    assert '00:02:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    editor.locator('#mosaicClose').click();open_auto(editor);editor.locator('#mosaicScan').click()
    assert '00:02:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('#mosaicStart').input_value()=='00:02:10'


def test_clip_time_edit_applies_to_all_regions(editor):
    editor.evaluate("findings.push({...findings[0],id:'1',x:20})")
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-adopt]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:02:10');editor.locator('#mosaicStart').dispatch_event('change')
    editor.wait_for_function("!document.querySelector('#mosaicConfirm').disabled")
    editor.locator('#mosaicConfirm').click()
    assert '00:02:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert len(draft['targets'])==2
    assert all(t['start']==70 and t['end']==90 and 70<=t['seed_frame']<90 for t in draft['targets'])


def test_cancel_mosaic_keeps_saved_clip_time_on_card(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicStart').dispatch_event('change')
    editor.locator('#mosaicConfirm').click()
    editor.locator('[data-target-remove]').click()
    assert '00:01:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()


def test_independent_time_draft_cancel_restore_and_reopen(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:01:10')
    editor.locator('#mosaicConfirm').click()
    assert '00:01:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    editor.locator('[data-event-delete]').click();editor.locator('[data-event-delete]').click()
    editor.locator('#mosaicClose').click();open_auto(editor)
    assert '00:01:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    editor.locator('.mosaic-clip-menu summary').click()
    editor.locator('[data-restore-clip]').click()
    assert '00:01:00–00:02:29' in editor.locator('[data-scan-event]').inner_text()


def test_rescan_keeps_edited_clip_and_original_snapshot(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.evaluate("scanJob.events[0]={...scanJob.events[0],id:'new-event',start:40,end:100}")
    editor.locator('#mosaicScan').click()
    assert editor.locator('[data-scan-event]').count()==1
    assert '00:01:00–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    assert active_count(editor)==1
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert draft['aiEvents'][0]['start']==30
    assert draft['targets'][0]['eventId']==draft['aiEvents'][0]['id']


def test_failed_rescan_preserves_list_and_unlocks(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-delete]').click()
    before=editor.locator('[data-scan-event]').inner_text()
    editor.evaluate("scanJob={...scanJob,status:'failed',events:[],error:'识别失败'}")
    editor.locator('#mosaicScan').click()
    assert editor.locator('[data-scan-event]').inner_text()==before
    assert editor.locator('[data-event-delete]').get_attribute('aria-pressed')=='true'
    assert editor.locator('[data-event-delete]').is_enabled()


def test_unedited_rescan_updates_and_manual_clip_survives(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('#mosaicAddClip').click();editor.locator('#mosaicAddSubmit').click()
    editor.evaluate("scanJob.events[0]={...scanJob.events[0],id:'changed',start:40,end:100}")
    editor.locator('#mosaicScan').click()
    assert editor.locator('[data-manual-card]').count()==1
    assert '00:01:10–00:03:09' in editor.locator('[data-scan-event="e1"]').inner_text()


def test_saved_time_drives_generation_and_keeps_ai_original(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.locator('[data-scan-event]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicConfirm').click()
    editor.evaluate("""() => {session.capabilities.tracking=true;const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.timeBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('timeBody.targets[0].start')==40
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert draft['aiEvents'][0]['start']==30
    assert draft['clipEdits']['e1']=={'start':40,'end':90}


def test_manual_fixed_region_persists_and_generates_without_tracking(editor):
    open_auto(editor);editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic')
    assert editor.locator('#mosaicMotion').input_value()=='fixed'
    editor.locator('#mosaicAddRegion').click();b=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(b['x']+20,b['y']+20);editor.mouse.down();editor.mouse.move(b['x']+80,b['y']+60);editor.mouse.up()
    editor.locator('#mosaicAddSubmit').click()
    editor.locator('#mosaicClose').click();open_auto(editor)
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('#mosaicMotion').input_value()=='fixed'
    editor.locator('#mosaicAdjust > summary').click()
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.fixedBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    assert editor.locator('#mosaicBatchStart').is_enabled()
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('fixedBody.targets[0].motion')=='fixed'


def test_ai_region_defaults_to_tracking_and_can_switch_to_fixed(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click();editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('#mosaicMotion').input_value()=='tracked'
    editor.locator('#mosaicMotion').select_option('fixed');editor.locator('#mosaicConfirm').click()
    editor.locator('[data-effect-target]').click()
    assert editor.locator('#mosaicEffectPreview').is_enabled()
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/clip-preview')){window.fixedPreview=body;return {id:'b'.repeat(32),status:'completed',...body};}return old(url,body);};}""")
    editor.locator('#mosaicEffectPreview').click()
    assert editor.evaluate('fixedPreview.targets[0].motion')=='fixed'


def test_manual_clip_multiple_regions_add_edit_remove_and_preview(editor):
    open_auto(editor);editor.locator('#mosaicAddClip').click();editor.locator('#mosaicManualAction').select_option('mosaic')
    def draw(x,y,w,h):
        b=editor.locator('#mosaicOverlay').bounding_box()
        editor.mouse.move(b['x']+x*b['width']/160,b['y']+y*b['height']/96);editor.mouse.down()
        editor.mouse.move(b['x']+(x+w)*b['width']/160,b['y']+(y+h)*b['height']/96);editor.mouse.up()
    editor.locator('#mosaicAddRegion').click();draw(10,10,30,20)
    editor.locator('#mosaicAddRegion').click();draw(80,50,30,20)
    editor.locator('#mosaicAddSubmit').click()
    assert editor.locator('[data-mosaic-card]').count()==1
    assert active_count(editor)==2
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('.mosaic-box').count()==2
    editor.locator('#mosaicAddRegion').click();draw(50,15,20,20)
    editor.locator('#mosaicConfirm').click()
    assert active_count(editor)==3
    editor.locator('#mosaicClose').click();open_auto(editor)
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('.mosaic-box').count()==3
    editor.locator('.mosaic-box').nth(1).click()
    editor.locator('#mosaicMoreTools').evaluate('(el)=>el.open=true')
    editor.locator('#mosaicBoxX').fill('85');editor.locator('#mosaicBoxX').dispatch_event('change');editor.locator('#mosaicConfirm').click()
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert len(draft['targets'])==3 and draft['targets'][1]['x']==85
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()');editor.locator('#mosaicRemoveRegion').click()
    assert active_count(editor)==2
    editor.locator('#mosaicAdjust > summary').click();editor.locator('[data-effect-target]').click()
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/clip-preview')){window.multiPreview=body;return {id:'b'.repeat(32),status:'completed'};}return old(url,body);};}""")
    editor.locator('#mosaicEffectPreview').click()
    assert editor.evaluate('multiPreview.targets.length')==2


def test_removed_ai_region_stays_removed_and_other_regions_survive(editor):
    editor.evaluate("findings.push({...findings[0],id:'1',x:10,w:20})")
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click();editor.locator('[data-event-adopt]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicRemoveRegion').click()
    assert active_count(editor)==1
    editor.locator('#mosaicClose').click();open_auto(editor)
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('.mosaic-box').count()==1
    editor.locator('#mosaicRemoveRegion').click()
    assert active_count(editor)==0
    editor.locator('#mosaicClose').click();open_auto(editor)
    editor.locator('[data-scan-event]').click()
    editor.locator('#mosaicFrame').wait_for(state='visible')
    assert editor.locator('.mosaic-box:visible').count()==0


def test_remove_unadopted_ai_box_persists(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()');editor.locator('#mosaicRemoveRegion').click()
    editor.locator('#mosaicClose').click();open_auto(editor);editor.locator('[data-scan-event]').click()
    editor.locator('#mosaicFrame').wait_for(state='visible')
    assert editor.locator('.mosaic-box:visible').count()==0
    assert active_count(editor)==0


def test_compact_adjustment_hides_precision_tools(editor):
    open_auto(editor)
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic')
    assert editor.locator('#mosaicRecognize').is_hidden()
    assert editor.locator('.adjust-time #mosaicMarkStart').is_visible()
    assert editor.locator('.adjust-time #mosaicMarkEnd').is_visible()
    assert editor.locator('#mosaicDraw').is_hidden()
    assert editor.locator('#mosaicSaveTime').is_hidden()
    assert editor.locator('#mosaicSaveDelete').is_hidden()
    assert editor.locator('#mosaicAddRegion').is_visible()
    editor.locator('#mosaicMoreTools > summary').click()
    assert editor.locator('#mosaicRecognize').is_visible()
    assert editor.locator('#mosaicMarkStart').is_visible()


def test_save_modifications_commits_time_and_both_region_edits(editor):
    editor.evaluate("findings.push({...findings[0],id:'1',x:10,w:20})")
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-adopt]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicMoreTools > summary').click()
    editor.locator('#mosaicBoxX').fill('45');editor.locator('#mosaicBoxX').dispatch_event('change')
    editor.locator('#mosaicCandidates button').nth(1).click()
    editor.locator('#mosaicBoxX').fill('15');editor.locator('#mosaicBoxX').dispatch_event('change')
    editor.locator('#mosaicStart').fill('00:01:10')
    editor.locator('#mosaicConfirm').click()
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert [t['x'] for t in draft['targets']]==[45,15]
    assert all(t['start']==40 for t in draft['targets'])
    assert '00:01:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()


def test_clip_checkboxes_toggle_cancel_and_are_mutually_exclusive(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    card=editor.locator('[data-mosaic-card="e1"]')
    delete=card.get_by_role('checkbox',name='删除',exact=True)
    mask=card.get_by_role('checkbox',name='打码',exact=True)
    assert not delete.is_checked() and not mask.is_checked()
    mask.click()
    assert mask.is_checked() and not delete.is_checked()
    assert card.locator('[data-effect-target]').is_visible()
    delete.click()
    assert delete.is_checked() and not mask.is_checked()
    assert card.locator('[data-effect-target]').count()==0
    delete.press('Space')
    assert not delete.is_checked() and not mask.is_checked()
    mask.click();mask.click()
    assert not mask.is_checked()


def test_card_edit_opens_clip_and_coordinates_belong_to_region(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('#mosaicAdjustToggle').is_hidden()
    stage=editor.locator('#mosaicStage').bounding_box()
    editor.locator('[data-edit-event="e1"]').click()
    assert editor.locator('#mosaicAdjust').evaluate('(el)=>el.open')
    assert editor.locator('#mosaicStart').input_value()=='00:01:00'
    assert editor.locator('#mosaicRegionTools #mosaicBoxFields').is_visible()
    assert not editor.locator('#mosaicMoreTools').evaluate('(el)=>el.open')
    assert editor.locator('#mosaicBoxFields').bounding_box()['y']>editor.locator('#mosaicCandidates').bounding_box()['y']
    assert abs(editor.locator('#mosaicStage').bounding_box()['height']-stage['height'])<1


def test_saving_one_region_motion_preserves_sibling_default_tracking(editor):
    editor.evaluate("findings.push({...findings[0],id:'1',x:10,w:20})")
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-adopt]').click();editor.locator('[data-edit-event]').click()
    editor.locator('#mosaicMotion').select_option('fixed');editor.locator('#mosaicConfirm').click()
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert [t.get('motion','tracked') for t in draft['targets']]==['fixed','tracked']


@pytest.mark.parametrize('delete_selected', [False,True])
def test_save_time_preserves_cancelled_mask_and_delete_choice(editor,delete_selected):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click();editor.locator('[data-target-remove]').click()
    if delete_selected:editor.locator('[data-event-delete]').click()
    editor.locator('[data-edit-event]').click();editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicConfirm').click()
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert draft['targets'][0]['adopted'] is False
    assert len(draft['deletions'])==int(delete_selected)
    assert draft['targets'][0]['start']==40


def test_open_automatically_scans_once_and_reuses_completed_empty_result(editor):
    editor.evaluate('''() => {const old=api;let report=null;window.autoStarts=0;
      window.api=async(url,body)=>{if(!url.endsWith('/scan'))return old(url,body);
        if(body){autoStarts++;report={id:'auto',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]};return report;}
        return {enabled:true,report};};}''')
    open_auto(editor)
    editor.wait_for_function('autoStarts===1')
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    assert editor.evaluate('autoStarts')==1


def test_mosaic_open_uses_long_preparation_timeout():
    import subprocess
    script=(Path(__file__).parents[1]/'exactcut/web/app.js').read_text()
    api=script[script.index('async function api('):script.index('\nfunction positionToast')]
    harness="""
const current={id:'a'.repeat(32),media_managed:true,revision:1},batchCache=[];
const sessionStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}},randomId=()=> 'token',appUrl=x=>x;
let duration;global.setTimeout=(fn,ms)=>{duration=ms;return 1};global.clearTimeout=()=>{};
global.fetch=async()=>({ok:true,status:200,json:async()=>({id:'session'})});
"""+api+"""
(async()=>{await api('/api/batches/'+current.id+'/mosaics/open',{});
if(duration!==120000)throw Error('wrong preparation timeout '+duration);})();
"""
    subprocess.run(['node','-e',harness],check=True,capture_output=True,text=True)


@pytest.mark.parametrize('enabled,report', [(False,None),(True,{'id':'failed','status':'failed','error':'识别失败'})])
def test_open_does_not_auto_retry_failure_or_disabled_scan(editor,enabled,report):
    editor.evaluate('''value=>{const old=api;window.scanWrites=0;window.api=async(url,body)=>{
      if(url.endsWith('/scan')){if(body)scanWrites++;return value;}return old(url,body);};}''',{'enabled':enabled,'report':report})
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('#mosaicWorkspace').wait_for(state='visible')
    editor.wait_for_timeout(100)
    assert editor.evaluate('scanWrites')==0


def test_auto_scan_waits_for_first_frame_and_recovers_after_frame_retry(editor):
    editor.route('**/frame/*',lambda r:r.fulfill(status=503,body='not ready'))
    editor.evaluate('''()=>{const old=api;window.scanWrites=0;window.api=async(url,body)=>{
      if(url.endsWith('/scan')){if(body){scanWrites++;return {id:'auto',status:'completed',events:[]};}return {enabled:true,report:null};}
      return old(url,body);};}''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('#mosaicFrameRetry').wait_for(state='visible')
    assert editor.evaluate('scanWrites')==0
    jpeg=io.BytesIO();Image.new('RGB',(160,96)).save(jpeg,'JPEG')
    editor.route('**/frame/*',lambda r:r.fulfill(body=jpeg.getvalue(),content_type='image/jpeg'))
    editor.locator('#mosaicFrameRetry').click()
    editor.wait_for_function('scanWrites===1')
