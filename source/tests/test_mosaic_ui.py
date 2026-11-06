"""Independent mosaic workspace; mocked model API, real browser/frame geometry."""
import io
import re
from pathlib import Path

import pytest
from PIL import Image
from browser_support import shared_playwright

pytestmark = pytest.mark.ui
CLIP_PREVIEW_REMOVED = 'fixed-only mode removed the server-side clip effect preview entry; fixed regions use the live canvas preview. Re-enable when tracking returns.'


@pytest.fixture
def editor():
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root/'index.html').read_text())
    jpeg = io.BytesIO()
    Image.new('RGB', (160, 96), '#555555').save(jpeg, 'JPEG')
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':900})
        page=context.new_page()
        page.set_default_timeout(10000)  # headroom for parallel runs; passing waits are unaffected
        page.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        page.route('**/frame/*', lambda r: r.fulfill(body=jpeg.getvalue(), content_type='image/jpeg'))
        page.goto('http://fixture.test/')
        install_editor(page)
        yield page
        browser.close()


def install_editor(page):
    root = Path(__file__).parents[1] / 'exactcut/web'
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
        if(url.endsWith('/filmstrip'))return {status:'failed'};
        if(url.endsWith('/scan'))return {enabled:true,report:{id:'existing',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]}};
        if(url.endsWith('/locate'))return {id:'job',status:'completed',...body,candidates:findings};
        return session;
      };
      $('assets').innerHTML='<article data-asset="0"><div class="cut-tools"><button data-open-cut>剪切</button></div></article>';
      $('assets').closest('[hidden]')?.removeAttribute('hidden');
    }''')
    page.add_script_tag(content=(root/'editor-busy.js').read_text())
    page.add_script_tag(content=(root/'editor-source.js').read_text())
    script=root/'mosaic.js'
    if script.exists():
        page.add_script_tag(content=script.read_text())


def adopt_regions_if_needed(page):
    # Segment edits commit on change/pointerup. Only standalone recognition needs adoption.
    page.evaluate('document.activeElement.blur()')
    if page.locator('#mosaicConfirm').is_visible():
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
    adopt_regions_if_needed(editor)
    assert '打码' in editor.locator('#mosaicTargets').inner_text()
    assert not editor.evaluate("calls.some(c=>c.url.includes('/cuts/')||(c.url.endsWith('/render')&&c.body))")
    editor.locator('#mosaicClose').click()
    open_editor(editor)
    assert editor.locator('[data-target-view]').count()==1


def test_boxes_remain_editable_within_selected_range(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    editor.locator('[data-candidate="0"]').wait_for()
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    adopt_regions_if_needed(editor)
    if not editor.locator('#mosaicNext').is_visible():editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicNext').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('.mosaic-box.selected .mosaic-handle').count()==8
    editor.locator('[data-target-view]').click()
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
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
    adopt_regions_if_needed(editor)
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('[data-candidate="1"]').click()
    adopt_regions_if_needed(editor)
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
    adopt_regions_if_needed(editor)
    assert editor.locator('[data-target-view]').count()==1


def test_workspace_keeps_transport_and_footer_visible(editor):
    open_editor(editor)
    editor.locator('#mosaicRecognize').click()
    adopt_regions_if_needed(editor)
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
    page.wait_for_function("!document.getElementById('mosaicBatchDialog').hidden || (!document.getElementById('mosaicScan').disabled && !document.getElementById('mosaicWorkspace').hidden) || !document.getElementById('mosaicStop').hidden")


def render_with_pending_segments(page, pending):
    with page.expect_event('dialog') as confirmation:
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#mosaicBatchStart').click()
    assert confirmation.value.type == 'confirm'
    assert confirmation.value.message == f'还有 {pending} 段待处理，这些段会保持原样。继续生成？'


@pytest.mark.parametrize('credential_state', ['missing', 'empty', 'unset'])
def test_scan_unavailable_offers_manual_segments(editor, tmp_path, monkeypatch, credential_state):
    from exactcut import cutting, cutting_scan
    from exactcut.error_guidance import guidance

    credential = tmp_path / 'scan.env'
    if credential_state == 'empty':
        credential.write_text('')
    monkeypatch.setenv('EXACTCUT_CUT_SCAN_ENV', str(credential))
    if credential_state == 'unset':
        monkeypatch.delenv('EXACTCUT_CUT_SCAN_ENV')
    with pytest.raises(cutting.CutError) as failure:
        cutting_scan.credential()
    body = {'error': str(failure.value), 'error_info': guidance(str(failure.value))}
    editor.evaluate('''body => {
      const original=api;
      window.api=async(url,data)=>{
        if(url.endsWith('/scan')&&data)throw EditorBusy.error(new Response('',{status:503}),body);
        return original(url,data);
      };
    }''', body)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.wait_for_function("document.getElementById('mosaicScanStatus').textContent==='识别服务暂不可用，可以先手动添加片段。'")
    assert '原片' not in editor.locator('#mosaicScanStatus').inner_text()
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicStart').fill('00:00:03')
    editor.locator('#mosaicEnd').fill('00:00:08')
    editor.locator('#mosaicAddSubmit').click()
    assert editor.locator('[data-manual-card]').count() == 1


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
    assert editor.locator('#mosaicAdjust').evaluate('(el)=>el.open')
    assert editor.locator('#mosaicRegionTools').is_hidden()
    assert editor.locator('#mosaicConfirm').is_hidden()
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/scan')&&c.body).body")=={'reset_at':'','force':False}
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/locate'))")
    editor.locator('[data-proposal]').click()
    adopt_regions_if_needed(editor)
    assert editor.locator('[data-target-view]').count()==1
    assert editor.locator('[data-scan-marker]').count()==1


def test_unlocalized_segment_remains_for_review(editor):
    scan_result(editor,needs_review=True)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert '未处理' in editor.locator('#mosaicScanEvents').inner_text()
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('#mosaicStart').input_value()=='00:01:00'
    assert editor.locator('#mosaicConfirm').is_hidden()  # Time changes commit without a mask.
    assert editor.locator('#mosaicCandidates').inner_text()==''
    assert 'AI 没框出区域' in editor.locator('#mosaicStatus').inner_text()


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
    adopt_regions_if_needed(editor)
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
    editor.wait_for_function('typeof finishScan === "function"')
    editor.locator('#mosaicClose').click()
    editor.evaluate('window.reopened=true')
    open_auto(editor)
    editor.locator('[data-scan-event="e1"]').wait_for()
    editor.evaluate('finishScan(scanJob)')
    editor.wait_for_timeout(100)
    assert '新任务结果' in editor.locator('#mosaicScanEvents').inner_text()


def test_preparation_shows_loading_until_ready_or_failed(editor):
    editor.evaluate('''() => {
      const original=window.api;
      window.api=(url,body)=>url.endsWith('/open') ? new Promise((resolve,reject)=>{
        window.finishOpen=()=>resolve(session);window.failOpen=()=>reject(Error('初始化失败'));
      }) : original(url,body);
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    assert editor.locator('#mosaicDialog').get_attribute('aria-busy') == 'true'
    assert editor.locator('#mosaicPreparing').is_visible()
    assert '正在准备视频' in editor.locator('#mosaicPreparing').inner_text()
    assert not editor.locator('#mosaicWorkspace').is_visible()
    assert not editor.locator('#mosaicBatchStart').is_visible()
    editor.evaluate('finishOpen()')
    editor.locator('#mosaicWorkspace').wait_for(state='visible')
    assert editor.locator('#mosaicDialog').get_attribute('aria-busy') == 'false'
    assert not editor.locator('#mosaicPreparing').is_visible()
    assert editor.locator('#mosaicBatchStart').is_visible()
    editor.locator('#mosaicClose').click()
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.evaluate('failOpen()')
    editor.wait_for_function("document.querySelector('#mosaicStatus').textContent==='初始化失败'")
    assert editor.locator('#mosaicDialog').get_attribute('aria-busy') == 'false'
    assert not editor.locator('#mosaicPreparing').is_visible()
    assert not editor.locator('#mosaicBatchStart').is_visible()


@pytest.mark.parametrize('action', ['mosaic', 'delete'])
@pytest.mark.parametrize('part,expected', [('start',[60,90]),('end',[30,120]),('move',[60,120])])
def test_timeline_trim_updates_saved_clip_and_survives_reopen(editor, action, part, expected):
    scan_result(editor)
    editor.evaluate('''() => {
      session.capabilities.tracking=true;const original=api;window.timelineSubmission=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){
          if(!body)return {job:null};
          timelineSubmission=body;return {id:'b'.repeat(32),status:'running',targets:body.targets,ranges:body.ranges};
        }
        if(url.includes('/render/'))return new Promise(()=>{});
        return original(url,body);
      };
    }''')
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    editor.locator('[data-proposal]' if action=='mosaic' else '[data-event-delete="e1"]').click()
    block = editor.locator('[data-timeline-event="e1"]')
    block.wait_for(state='visible')
    handle = (block if part=='move' else block.locator(f'[data-edge="{part}"]')).bounding_box()
    rail = editor.locator('#mosaicTimelineTracks').bounding_box()
    editor.mouse.move(handle['x'] + handle['width']/2, handle['y'] + handle['height']/2)
    editor.mouse.down()
    editor.mouse.move(handle['x'] + handle['width']/2 + rail['width']/10, handle['y'] + handle['height']/2)
    editor.mouse.up()
    editor.wait_for_function("expected=>{const e=JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3')))).clipEdits.e1;return e.start===expected[0]&&e.end===expected[1]}", arg=expected)
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    block.wait_for(state='visible')
    assert block.get_attribute('data-start') == str(expected[0])
    assert block.get_attribute('data-end') == str(expected[1])
    assert not editor.locator('#mosaicTimeline input[type=range]').count()
    editor.locator('#mosaicBatchStart').click()
    editor.wait_for_function('window.timelineSubmission!==null')
    if action=='delete':
        assert editor.evaluate('timelineSubmission.ranges') == [expected]
        assert editor.evaluate('timelineSubmission.targets') == []
    else:
        assert editor.evaluate('timelineSubmission.targets.map(t=>[t.start,t.end])') == [expected]


@pytest.mark.parametrize('size', [(1512,717),(1440,900)])
def test_workspace_panels_do_not_overlap_timeline_and_actions_are_in_header(editor, size):
    editor.set_viewport_size({'width':size[0],'height':size[1]})
    scan_result(editor)
    editor.evaluate("scanJob.events=Array.from({length:8},(_,i)=>({...scanJob.events[0],id:'e'+i,start:i*30,end:(i+1)*30,seed_frame:i*30+15}))")
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e0"]').wait_for()
    timeline=editor.locator('#mosaicTimeline').bounding_box()
    for selector in ['.mosaic-library','.mosaic-side']:
        box=editor.locator(selector).bounding_box()
        assert box['y']+box['height'] <= timeline['y']
    assert timeline['y']+timeline['height'] <= size[1]
    assert editor.locator('.mosaic-header #mosaicBatchStart').is_visible()
    assert editor.locator('.mosaic-header #mosaicDiscard').is_visible()
    assert editor.locator('[data-mosaic-card]').first.bounding_box()['height'] <= 110


def test_clip_list_scrolls_without_moving_timeline(editor):
    editor.set_viewport_size({'width':1512,'height':717})
    scan_result(editor)
    editor.evaluate("scanJob.events=Array.from({length:8},(_,i)=>({...scanJob.events[0],id:'e'+i,start:i*30,end:(i+1)*30,seed_frame:i*30+15}))")
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    first=editor.locator('[data-mosaic-card="e0"]')
    first.wait_for(state='visible')
    before=editor.locator('#mosaicTimeline').bounding_box()
    first.hover()
    editor.mouse.wheel(0,1200)
    editor.wait_for_function("document.querySelector('#mosaicTargets').scrollTop>0")
    last=editor.locator('[data-mosaic-card="e7"]').bounding_box()
    assert last['y']+last['height'] <= before['y']
    assert editor.locator('#mosaicTimeline').bounding_box() == before


@pytest.fixture
def portrait_source(editor, tmp_path):
    import shutil
    import subprocess
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg required for real thumbnail decoding')
    source=tmp_path/'portrait.mp4'
    subprocess.run([ffmpeg,'-v','error','-y','-f','lavfi','-i','color=c=red:s=160x284:r=30:d=2','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(source)],check=True)
    data=source.read_bytes()
    def serve_source(route):
        match=re.fullmatch(r'bytes=(\d+)-(\d*)',route.request.headers.get('range',''))
        if match:
            start=int(match[1]);end=min(int(match[2]) if match[2] else len(data)-1,len(data)-1)
            route.fulfill(status=206,body=data[start:end+1],content_type='video/mp4',headers={'Accept-Ranges':'bytes','Content-Range':f'bytes {start}-{end}/{len(data)}'})
        else:
            route.fulfill(body=data,content_type='video/mp4',headers={'Accept-Ranges':'bytes'})
    editor.route('**/source',serve_source)
    editor.evaluate('Object.assign(session.metadata,{frame_count:60,width:160,height:284})')
    return source


def test_portrait_thumbnails_fill_the_strip_without_black_borders(editor, portrait_source):
    editor.set_viewport_size({'width':1512,'height':717})
    open_auto(editor)
    thumbs=editor.locator('#mosaicTimelineThumbs canvas')
    assert 24 <= thumbs.count() <= 30
    editor.wait_for_function("[...document.querySelectorAll('#mosaicTimelineThumbs canvas')].every(c=>c.getContext('2d').getImageData(1,1,1,1).data[0]>200)")
    first,last=thumbs.first.bounding_box(),thumbs.last.bounding_box()
    strip=editor.locator('#mosaicTimelineThumbs').bounding_box()
    assert abs(first['x']-strip['x'])<1
    assert abs(last['x']+last['width']-strip['x']-strip['width'])<1
    initial_count=thumbs.count()
    editor.set_viewport_size({'width':1912,'height':717})
    editor.wait_for_function('count=>document.querySelectorAll("#mosaicTimelineThumbs canvas").length>count',arg=initial_count)
    editor.wait_for_function("[...document.querySelectorAll('#mosaicTimelineThumbs canvas')].every(c=>c.getContext('2d').getImageData(1,1,1,1).data[0]>200)")
    editor.locator('#mosaicClose').click()
    editor.route('**/source',lambda route:route.abort())
    open_auto(editor)
    editor.wait_for_function("[...document.querySelectorAll('#mosaicTimelineThumbs canvas')].length>0 && [...document.querySelectorAll('#mosaicTimelineThumbs canvas')].every(c=>c.getContext('2d').getImageData(1,1,1,1).data[0]>200)")


@pytest.mark.parametrize('ranges,position,expected', [([[0,15]],0,.5),([[15,30],[25,40]],.5,40/30),([[15,30],[25,40]],0,40/30),([[45,60]],1.5,None),([[0,60]],0,None)])
def test_playback_skips_deleted_spans_without_render(editor, portrait_source, ranges, position, expected):
    open_auto(editor)
    editor.locator('#mosaicClose').click()
    editor.evaluate('''ranges=>{
      const key='exactcut.mosaic.workbench.v3.fixture.batch.session.'+'a'.repeat(64)+'.';
      localStorage.setItem(key,JSON.stringify({schema_version:1,targets:[],deletions:ranges.map(([start,end])=>({start,end}))}));
    }''',ranges)
    open_auto(editor)
    video=editor.locator('#mosaicSource')
    editor.wait_for_function("document.querySelector('#mosaicSource').readyState>=2")
    video.evaluate('(el,time)=>el.currentTime=time',position)
    video.evaluate("el=>{window.firstPlayingTime=null;window.jumpTime=null;el.addEventListener('playing',()=>{window.firstPlayingTime=el.currentTime},{once:true});el.addEventListener('seeking',()=>{if(el.currentTime>1)window.jumpTime=el.currentTime})}")
    editor.locator('#mosaicPlay').click()
    if expected is None:
        editor.wait_for_function("document.querySelector('#mosaicSource').paused && document.querySelector('#mosaicSource').currentTime<1.5")
    elif position<ranges[0][0]/30:
        editor.wait_for_function('window.jumpTime!==null')
        assert editor.evaluate('jumpTime')>=expected
    else:
        editor.wait_for_function('window.firstPlayingTime!==null')
        assert editor.evaluate('firstPlayingTime')>=expected
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/render')&&c.body)")


def test_deletion_overlays_thumbnails_and_cancel_restores_strip(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-delete="e1"]').click()
    deletion=editor.locator('[data-timeline-event="e1"]')
    strip=editor.locator('#mosaicTimelineThumbs').bounding_box()
    box=deletion.bounding_box()
    assert abs(box['y']-strip['y'])<1
    assert abs(box['height']-strip['height'])<1
    assert editor.locator('#mosaicTimelineBlocks [data-timeline-event]').count()==0
    assert '删除' in deletion.inner_text()
    assert deletion.locator('.mosaic-deletion-label').is_visible()
    deletion.click()
    assert editor.locator('#mosaicStart').is_visible()
    editor.locator('#mosaicEnd').fill('00:01:02')
    adopt_regions_if_needed(editor)
    assert deletion.locator('.mosaic-deletion-label').is_hidden()
    assert '删除' in deletion.get_attribute('title')
    editor.locator('[data-event-delete="e1"]').click()
    assert editor.locator('[data-timeline-event]').count()==1
    assert 'suggestion' in editor.locator('[data-timeline-event]').get_attribute('class')
    restored=editor.locator('#mosaicTimelineThumbs').bounding_box()
    assert restored['width']==strip['width'] and restored['height']==strip['height']  # Suggestion adds its own row above.


def test_simple_editor_exposes_only_fixed_mosaic_and_blur(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    assert editor.locator('#mosaicLiveStyle option').evaluate_all('(items)=>items.map(e=>e.value)')==['pixel','blur']
    assert editor.locator('#mosaicMotion').is_hidden()
    assert editor.locator('#mosaicLiveStrength').is_hidden()
    assert editor.locator('#mosaicLiveExpansion').is_hidden()
    assert editor.locator('[data-effect-target]').count()==0
    editor.locator('#mosaicLiveStyle').select_option('blur')
    assert editor.locator('#mosaicBatchStart').is_enabled()
    editor.locator('#mosaicBatchStart').click()
    body=editor.evaluate("calls.find(c=>c.url.endsWith('/render')&&c.body).body")
    assert all(t['motion']=='fixed' for t in body['targets'])
    assert body['targets'][0]['effect']=={'style':'blur','strength':'medium','expansion':'none'}


def test_all_segments_show_timeline_but_only_masks_show_mosaic_inspector(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('[data-timeline-event]').count()==1
    assert 'suggestion' in editor.locator('[data-timeline-event]').get_attribute('class')
    assert editor.locator('#mosaicLiveControls').is_hidden()
    assert editor.locator('#mosaicRegionTools').is_hidden()
    assert editor.locator('#mosaicAdjust > summary').is_hidden()
    editor.locator('[data-proposal]').click()
    assert 'mosaic' in editor.locator('[data-timeline-event="e1"]').get_attribute('class')
    assert editor.locator('#mosaicTargets [data-effect-target]').count()==0
    assert editor.locator('#mosaicLiveControls').is_visible()
    assert editor.locator('#mosaicRegionTools').is_visible()
    editor.locator('[data-event-delete="e1"]').click()
    assert 'deletion' in editor.locator('[data-timeline-event="e1"]').get_attribute('class')
    assert editor.locator('#mosaicLiveControls').is_hidden()
    assert editor.locator('#mosaicRegionTools').is_hidden()
    editor.locator('#mosaicStart').fill('00:01:10')
    editor.locator('#mosaicStart').dispatch_event('change')
    assert 'deletion' in editor.locator('[data-timeline-event="e1"]').get_attribute('class')
    assert editor.locator('[data-timeline-event="e1"]').get_attribute('data-start')=='40'
    assert active_count(editor)==0
    editor.locator('[data-event-delete="e1"]').click()
    assert editor.locator('[data-timeline-event]').count()==1
    assert 'suggestion' in editor.locator('[data-timeline-event]').get_attribute('class')


def test_fixed_region_live_preview_changes_pixels_without_render(editor):
    # Vertical 2px stripes: both blur and pixel average them, so the region must change.
    jpeg=io.BytesIO();Image.frombytes('L',(160,96),bytes((0 if (x//2)%2 else 255) for y in range(96) for x in range(160))).convert('RGB').save(jpeg,'JPEG',quality=95)
    editor.route('**/frame/*',lambda r:r.fulfill(body=jpeg.getvalue(),content_type='image/jpeg'))
    scan_result(editor)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    editor.locator('#mosaicLiveCanvas.has-suggestions').wait_for(state='visible')
    assert editor.locator('.mosaic-box.suggestion').evaluate("el=>getComputedStyle(el).borderTopStyle")=='dashed'
    assert editor.locator('.mosaic-box.suggestion').evaluate("el=>getComputedStyle(el).borderTopColor")=='rgb(255, 196, 0)'
    assert editor.evaluate('''() => {
      const canvas=$('mosaicLiveCanvas'),ctx=canvas.getContext('2d'),original=document.createElement('canvas');
      original.width=160;original.height=96;original.getContext('2d').drawImage($('mosaicFrame'),0,0);
      const a=ctx.getImageData(50,34,60,28).data,b=original.getContext('2d').getImageData(50,34,60,28).data;
      return a.every((v,i)=>v===b[i]);
    }'''), 'the suggestion interior must remain identical to the source frame'
    # The yellow dashed stroke is on the live canvas, also used when the DOM overlay is hidden.
    assert editor.evaluate('''() => {
      const c=$('mosaicLiveCanvas').getContext('2d');
      const yellow=x=>{const p=c.getImageData(x,24,1,1).data;return p[0]>200&&p[1]>150&&p[2]<120};
      return Array.from({length:60},(_,i)=>yellow(50+i)).some(Boolean)
        && Array.from({length:60},(_,i)=>yellow(50+i)).some(v=>!v);
    }''')
    editor.locator('[data-proposal]').click()
    editor.locator('[data-scan-event="e1"]').click()
    adopt_regions_if_needed(editor)
    assert 'has-suggestions' not in editor.locator('#mosaicLiveCanvas').get_attribute('class')
    pixel="(x,y)=>document.querySelector('#mosaicLiveCanvas').getContext('2d').getImageData(x,y,1,1).data[0]"
    for style in ('blur','pixel'):
        editor.locator('#mosaicLiveStyle').select_option(style)
        editor.wait_for_function(f"!document.querySelector('#mosaicLiveCanvas').hidden && Math.abs(({pixel})(80,48)-128)<70 && Math.abs(({pixel})(81,48)-128)<70")
        outside=[editor.evaluate(f"({pixel})({x},2)") for x in (2,3,4,5)]
        assert max(outside)>190 and min(outside)<65,'outside the region the original stripes stay visible'
    editor.locator('#mosaicCompare').click()
    assert editor.locator('#mosaicLiveCanvas').is_hidden()
    editor.locator('#mosaicCompare').click()
    editor.locator('#mosaicLiveCanvas').wait_for(state='visible')
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/render')&&c.body)")

    editor.locator('[data-target-remove]').click()
    editor.locator('#mosaicLiveCanvas.has-suggestions').wait_for(state='visible')
    outside=[editor.evaluate(f"({pixel})({x},48)") for x in (80,81,82,83)]
    assert max(outside)>190 and min(outside)<65, 'cancelled masks restore original pixels'


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
    editor.wait_for_function("calls.filter(c=>c.url.endsWith('/scan')).length===1")
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


def test_running_scan_keeps_existing_regions_editable(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const original=api;
      window.api=async(url,body)=>url.endsWith('/scan')
        ? {enabled:true,report:{...scanJob,status:'running',progress:'正在定位第 2 个区间'}}
        : original(url,body);
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('[data-scan-event="e1"]').wait_for()
    assert '已有片段可编辑' in editor.locator('#mosaicScanStatus').inner_text()
    assert editor.locator('#mosaicScan').is_disabled()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('[data-proposal]').is_enabled()
    assert editor.locator('#mosaicConfirm').is_hidden()
    editor.locator('[data-proposal]').click()
    assert active_count(editor)==1


def test_three_column_workspace_and_fixed_adopt_next(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:120})")
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicFrame').wait_for(state='visible')
    left=editor.locator('.mosaic-library').bounding_box()
    preview=editor.locator('#mosaicStage').bounding_box()
    right=editor.locator('.mosaic-side').bounding_box()
    assert left['x']+left['width'] < preview['x']
    assert preview['x']+preview['width'] < right['x']
    assert editor.locator('#mosaicTimeline').bounding_box()['width'] > preview['width']
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
    editor.locator('#mosaicFrame').wait_for(state='visible')
    editor.locator('[data-proposal]').click()
    adopt_regions_if_needed(editor)
    card=editor.locator('[data-mosaic-card="e1"]')
    assert '打码' in card.inner_text()
    assert card.locator('[data-target-view]').is_visible()
    assert editor.locator('.mosaic-side #mosaicAdjust').evaluate('e=>e.open')
    assert card.evaluate("e=>!!e.closest('.mosaic-library')")
    assert editor.locator('#mosaicSaved').count()==0
    card.locator('[data-target-view]').click()
    assert editor.locator('#mosaicConfirm').is_hidden()
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
    editor.locator('#mosaicBoxX').fill('20')
    editor.locator('#mosaicBoxX').dispatch_event('change')
    adopt_regions_if_needed(editor)
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
    adopt_regions_if_needed(editor)
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
    editor.locator('#mosaicFrame').wait_for(state='visible')
    editor.locator('[data-proposal]').click();adopt_regions_if_needed(editor)
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
    adopt_regions_if_needed(editor)
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
    # v1 targets carry no motion (= tracked): they open as fixed and the user is told so.
    # The third legacy target (50–60, adopted:false) remains an undecided standalone card.
    editor.wait_for_function("Object.keys(localStorage).some(k=>k.includes('workbench.v3'))")
    draft=workbench_draft(editor)
    pending=[t for t in draft['targets'] if t['adopted'] is False]
    assert len(pending)==1 and (pending[0]['start'],pending[0]['end'])==(50,60)
    pending_block=editor.locator(f'[data-timeline-event="target:{pending[0]["uid"]}"]')
    assert 'suggestion' in pending_block.get_attribute('class').split()
    assert pending_block.get_attribute('data-start')=='50'
    assert editor.locator('[data-target-remove][aria-checked="false"]').count()==1
    assert editor.locator('#mosaicBatchHint').inner_text().split('\n')==['删除 0 段 · 打码 2 段 · 待处理 1 段','旧草稿已按当前规则改为固定位置、标准强度、不扩大范围；请播放检查后再生成。']
    render_with_pending_segments(editor, 1)
    assert editor.locator('#mosaicBatchDialog').is_visible()
    assert editor.evaluate('batchBody.targets.length')==2
    assert editor.evaluate('batchBody.targets.map(t=>t.start)')==[10,30]
    assert editor.locator('#mosaicBatchDownload').is_visible()
    assert editor.locator('#mosaicBatchVideo').is_visible()
    assert editor.locator('#mosaicBatchStatus').inner_text()=='处理完成，请预览；确认替换或放弃结果后可继续编辑。'
    assert '失跟' not in editor.locator('#mosaicBatchDialog').inner_text()
    editor.locator('#mosaicBatchClose').click();editor.locator('#mosaicClose').click()
    open_auto(editor)
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
        if(url.endsWith('/discard-result'))return {id:'b'.repeat(32),status:'completed',discarded_at:'now'};
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
    editor.wait_for_function("!document.getElementById('mosaicClear').disabled")
    assert editor.locator('[data-event-delete="e1"]').get_attribute('aria-pressed')=='true'
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    assert editor.locator('[data-event-delete="e1"]').get_attribute('aria-pressed')=='true'
    assert editor.locator('#mosaicBatchStart').is_enabled()
    assert editor.locator('[data-scan-event="e1"]').count()==1


def generated_result(page,*,legacy):
    page.evaluate('''legacy => {
      // Legacy: pre-simplification targets had no motion (backend default: tracked) and the old default expansion.
      const target={...findings[0],start:60,end:90,seed_frame:70,adopted:true,...(legacy?{}:{motion:'fixed'})};
      const effect=legacy?{style:'pixel',strength:'medium',expansion:'small'}:{style:'pixel',strength:'medium',expansion:'none'};
      const {adopted,...rendered}=target;
      localStorage.setItem('exactcut.mosaic.workbench.v3.fixture.batch.session.'+'a'.repeat(64)+'.',JSON.stringify({schema_version:1,targets:[target],deletions:[{start:0,end:30}],...(legacy?{effect}:{})}));
      const old=api;window.replaced=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),status:'completed',targets:[{...rendered,effect}],effect,ranges:[[0,30]],result:{region_count:1},completed_at:'now'}};
        if(url.endsWith('/replace')){replaced=body;return {replacement:{status:'completed'}};}
        if(url==='/api/batches/batch')return current;
        return old(url,body);
      };window.renderRuntime=()=>{};window.toast=()=>{};
    }''',legacy)
    open_auto(page)


def test_generated_result_replace_and_compare_use_saved_plan(editor):
    generated_result(editor,legacy=False)
    assert editor.locator('#mosaicReplace').is_enabled()
    assert editor.locator('#mosaicBatchVersion').inner_text()==''
    editor.locator('#mosaicBatchVideo').evaluate('el=>el.currentTime=1')
    editor.locator('#mosaicBatchBefore').click()
    editor.locator('#mosaicBatchVideo').evaluate("el=>el.dispatchEvent(new Event('loadedmetadata'))")
    assert editor.locator('#mosaicBatchVideo').get_attribute('src').endswith('/source')
    assert editor.locator('#mosaicBatchVideo').evaluate('el=>el.currentTime')==2
    editor.locator('#mosaicBatchAfter').click()
    editor.locator('#mosaicBatchVideo').evaluate("el=>el.dispatchEvent(new Event('loadedmetadata'))")
    assert editor.locator('#mosaicBatchVideo').evaluate('el=>el.currentTime')==1
    editor.locator('#mosaicBatchClose').click()
    assert editor.locator('[data-target-remove]').is_disabled()
    assert editor.locator('#mosaicBatchStart').is_hidden()
    editor.locator('#mosaicBatchView').click()
    assert editor.locator('#mosaicReplace').is_enabled()
    editor.once('dialog',lambda d:d.accept())
    editor.locator('#mosaicReplace').click()
    editor.wait_for_function("!document.getElementById('mosaicDialog').open")
    assert editor.evaluate('replaced')=={'job_id':'b'*32,'reset_at':''}
    assert editor.evaluate("Object.keys(localStorage).filter(k=>k.includes('workbench.v3')).length")==0


def test_legacy_tracked_result_is_never_replaced_as_the_fixed_plan(editor):
    """The draft is shown as fixed, but the old result was rendered tracked with the old
    expansion; replacing must stay blocked with a visible reason, never silently allowed."""
    generated_result(editor,legacy=True)
    assert editor.locator('#mosaicReplace').is_disabled()
    assert '旧版处理方式' in editor.locator('#mosaicBatchVersion').inner_text()
    editor.locator('#mosaicReplace').evaluate('el=>el.click()')
    assert editor.evaluate('replaced') is None
    editor.locator('#mosaicBatchClose').click()
    editor.locator('#mosaicBatchView').click()
    assert editor.locator('#mosaicReplace').is_disabled()
    # Discarding the old result lets the user regenerate with the fixed plan.
    editor.once('dialog',lambda d:d.accept())
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{
      if(url.endsWith('/discard-result'))return {id:'b'.repeat(32),status:'completed',discarded_at:'now',targets:[],ranges:[]};
      if(url.endsWith('/render')&&body){window.newBody=body;return {id:'d'.repeat(32),status:'completed',...body,result:{},completed_at:'now'};}
      return old(url,body);};}""")
    editor.locator('#mosaicDiscard').click()
    editor.locator('#mosaicBatchStart').wait_for(state='visible')
    assert '旧草稿已按当前规则改为固定位置' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('#mosaicBatchStart').click()
    editor.wait_for_function('window.newBody')
    body=editor.evaluate('newBody')
    assert [t['motion'] for t in body['targets']]==['fixed']
    assert body['targets'][0]['effect']=={'style':'pixel','strength':'medium','expansion':'none'}
    assert editor.locator('#mosaicReplace').is_enabled()


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
    assert editor.locator('#mosaicConfirm').is_hidden()


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


def test_effect_choice_is_clip_local_without_popup(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:120})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('[data-effect-target]').count()==0
    for event in ('e1','e2'):editor.locator(f'[data-proposal="{event}:0"]').click()
    assert editor.locator('[data-effect-target]').count()==0,'the old effects popup has no entry'
    editor.locator('[data-mosaic-card="e1"] [data-scan-event]').click()
    editor.locator('#mosaicLiveStyle').select_option('blur')
    assert '效果已保存' in editor.locator('#mosaicStatus').inner_text()
    editor.locator('[data-mosaic-card="e2"] [data-scan-event]').click()
    assert editor.locator('#mosaicLiveStyle').input_value()=='pixel'
    editor.locator('[data-mosaic-card="e1"] [data-scan-event]').click()
    assert editor.locator('#mosaicLiveStyle').input_value()=='blur'


def test_generation_locks_edits_until_stop_acknowledged(editor):
    scan_result(editor)
    editor.evaluate('''() => {
      const old=api;window.stopped=false;
      window.api=async(url,body)=>{
        if(url.endsWith('/stop')){window.stopBody=body;stopped=true;return {id:'b'.repeat(32),status:'running',cancel_requested:true,targets:[]};}
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),queued_task:true,status:'running',targets:[],ranges:[[30,90]]}};
        if(url.includes('/render/'))return stopped?new Promise(r=>window.finishStop=r):{id:'b'.repeat(32),status:'running',targets:[]};
        if(url.endsWith('/scan')&&!body)return {enabled:true,report:scanJob};
        return old(url,body);
      };
    }''')
    open_auto(editor);editor.locator('#mosaicProcessing').wait_for()
    assert editor.locator('[data-event-delete]').is_disabled()
    assert editor.locator('#mosaicAddClip').is_disabled()
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
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:120})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    for event,style in [('e1','blur'),('e2','pixel')]:
        editor.locator(f'[data-proposal="{event}:0"]').click()
        editor.locator(f'[data-mosaic-card="{event}"] [data-scan-event]').click()
        editor.locator('#mosaicLiveStyle').select_option(style)
    editor.locator('#mosaicClose').click();open_auto(editor);editor.locator('#mosaicScan').click()
    for event,style in [('e1','blur'),('e2','pixel')]:
        editor.locator(f'[data-mosaic-card="{event}"] [data-scan-event]').click()
        assert editor.locator('#mosaicLiveStyle').input_value()==style
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.effectBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('effectBody.targets.map(t=>t.effect.style)')==['blur','pixel']
    assert editor.evaluate('effectBody.targets.every(t=>t.motion==="fixed"&&t.effect.strength==="medium"&&t.effect.expansion==="none")')
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/clip-preview'))")


@pytest.mark.skip(reason=CLIP_PREVIEW_REMOVED)
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
    editor.locator('[data-mosaic-card="e1"] [data-scan-event]').click()
    editor.locator('#mosaicLivePreviewAction [data-effect-target]').click()
    editor.locator('#mosaicEffectStyle').select_option('soft_blur')
    editor.locator('#mosaicEffectPreview').click()
    editor.locator('#mosaicTrackDialog').wait_for()
    assert editor.evaluate('previewBody.targets.length')==2
    assert editor.evaluate('previewBody.targets.every(t=>t.start===30&&t.effect.style==="soft_blur")')
    assert '/render/' in editor.locator('#mosaicTrackVideo').get_attribute('src')
    editor.locator('#mosaicTrackClose').click()
    assert editor.locator('#mosaicEffects').is_visible()
    editor.locator('#mosaicEffectCancel').click()
    editor.locator('[data-mosaic-card="e1"] [data-scan-event]').click()
    editor.locator('#mosaicLivePreviewAction [data-effect-target]').click()
    assert editor.locator('#mosaicEffectStyle').input_value()=='pixel'


@pytest.mark.skip(reason=CLIP_PREVIEW_REMOVED)
def test_closing_running_clip_preview_detaches_without_cancelling_or_discarding_effect_draft(editor):
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
    assert editor.locator('#mosaicEffectSave').is_enabled()
    assert editor.locator('#mosaicTrackDialog').bounding_box()['height']>=700
    editor.locator('#mosaicTrackClose').click()
    editor.wait_for_function("!document.querySelector('#mosaicEffectSave').disabled")
    assert editor.evaluate('previewStopped') is False
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
    adopt_regions_if_needed(editor)
    editor.locator('[data-target-view]').click()
    assert values()==[55,33,105,63]


def test_adjust_sidebar_does_not_shrink_preview(editor):
    open_editor(editor)
    before=editor.locator('#mosaicStage').bounding_box()
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    after=editor.locator('#mosaicStage').bounding_box()
    assert abs(before['height']-after['height'])<1
    assert editor.locator('#mosaicAdjust').bounding_box()['x']>after['x']+after['width']
    assert editor.locator('.mosaic-scan-section').is_visible()
    assert editor.locator('#mosaicConfirm').is_hidden()  # No standalone recognition candidates yet.
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
    editor.locator('#mosaicFrame').wait_for(state='visible')
    assert editor.locator('#mosaicFrameNumber').input_value()=='00:02:10'
    adopt_regions_if_needed(editor)
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
    editor.locator('#mosaicFrame').wait_for(state='visible')
    adopt_regions_if_needed(editor)
    assert '00:02:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert len(draft['targets'])==2
    assert all(t['start']==70 and t['end']==90 and 70<=t['seed_frame']<90 for t in draft['targets'])


def test_cancel_mosaic_keeps_saved_clip_time_on_card(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicStart').dispatch_event('change')
    adopt_regions_if_needed(editor)
    editor.locator('[data-target-remove]').click()
    assert '00:01:10–00:02:29' in editor.locator('[data-scan-event]').inner_text()


def test_independent_time_draft_cancel_restore_and_reopen(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:01:10')
    editor.locator('#mosaicStart').dispatch_event('change')
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
    editor.evaluate('session.capabilities.tracking=true')
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click()
    editor.locator('[data-scan-event]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicStart').fill('00:01:10');adopt_regions_if_needed(editor)
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.timeBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
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
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.fixedBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    assert editor.locator('#mosaicBatchStart').is_enabled()
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('fixedBody.targets[0].motion')=='fixed'


def test_ai_region_is_fixed_without_a_position_choice(editor):
    editor.evaluate('session.capabilities.tracking=true')
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click();editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('#mosaicMotion').input_value()=='fixed' and editor.locator('#mosaicMotion').is_hidden()
    assert editor.locator('#mosaicTrack').is_hidden()
    adopt_regions_if_needed(editor)
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.fixedBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('fixedBody.targets[0].motion')=='fixed'
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/clip-preview')||c.url.endsWith('/track'))")


def test_manual_clip_multiple_regions_add_edit_remove_and_submit(editor):
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
    adopt_regions_if_needed(editor)
    assert active_count(editor)==3
    editor.locator('#mosaicClose').click();open_auto(editor)
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    assert editor.locator('.mosaic-box').count()==3
    editor.locator('.mosaic-box').nth(1).click()
    editor.locator('#mosaicBoxX').fill('85');editor.locator('#mosaicBoxX').dispatch_event('change');adopt_regions_if_needed(editor)
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert len(draft['targets'])==3 and draft['targets'][1]['x']==85
    editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()');editor.locator('#mosaicRemoveRegion').click()
    assert active_count(editor)==2
    editor.evaluate("""() => {const old=api;window.api=async(url,body)=>{if(url.endsWith('/render')&&body){window.multiBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}return old(url,body);};}""")
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('multiBody.targets.length')==2
    assert editor.evaluate('multiBody.targets.every(t=>t.motion==="fixed")')


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


def test_unadopted_ai_box_is_not_editable_and_keeps_its_suggestion(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event]').click()
    assert editor.locator('#mosaicRegionTools').is_hidden()
    editor.locator('#mosaicClose').click();open_auto(editor);editor.locator('[data-scan-event]').click()
    editor.locator('#mosaicFrame').wait_for(state='visible')
    assert editor.locator('#mosaicRegionTools').is_hidden()
    assert editor.locator('[data-proposal]').count()==1
    assert active_count(editor)==0


def test_compact_adjustment_hides_precision_tools(editor):
    open_auto(editor)
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic')
    assert editor.locator('#mosaicRegionTools #mosaicRecognize').is_visible()
    assert editor.locator('.adjust-time #mosaicMarkStart').is_visible()
    assert editor.locator('.adjust-time #mosaicMarkEnd').is_visible()
    assert editor.locator('#mosaicDraw').is_hidden()
    assert editor.locator('#mosaicSaveTime').is_hidden()
    assert editor.locator('#mosaicSaveDelete').is_hidden()
    assert editor.locator('#mosaicAddRegion').is_visible()
    assert editor.locator('#mosaicRecognize').is_visible()
    assert editor.locator('#mosaicMarkStart').is_visible()


def test_changes_commit_time_and_both_region_edits_immediately(editor):
    editor.evaluate("findings.push({...findings[0],id:'1',x:10,w:20})")
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-adopt]').click()
    editor.locator('[data-target-view]').click();editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicBoxX').fill('45');editor.locator('#mosaicBoxX').dispatch_event('change')
    # Clicking another region blurs the input and repeats its native change event.
    editor.locator('#mosaicCandidates button').nth(1).click()
    assert editor.locator('#mosaicCandidates button').nth(1).get_attribute('aria-pressed')=='true'
    assert [t['x'] for t in workbench_draft(editor)['targets']]==[45,10]
    editor.locator('#mosaicBoxX').fill('15');editor.locator('#mosaicBoxX').dispatch_event('change')
    editor.locator('#mosaicStart').fill('00:01:10')
    editor.locator('#mosaicStart').dispatch_event('change')
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
    assert card.locator('[data-effect-target]').count()==0
    assert editor.locator('#mosaicLiveControls').is_visible()
    delete.click()
    assert delete.is_checked() and not mask.is_checked()
    assert card.locator('[data-effect-target]').count()==0
    delete.press('Space')
    assert not delete.is_checked() and not mask.is_checked()
    assert workbench_draft(editor)['deletions']==[]
    assert '待处理 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    delete.click();delete.click()
    assert not delete.is_checked() and not mask.is_checked()
    mask.click();mask.click()
    assert not mask.is_checked()


def test_card_edit_opens_clip_and_coordinates_belong_to_region(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    assert editor.locator('#mosaicAdjustToggle').is_hidden()
    editor.locator('[data-proposal]').click()
    stage=editor.locator('#mosaicStage').bounding_box()
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('#mosaicAdjust').evaluate('(el)=>el.open')
    assert editor.locator('#mosaicStart').input_value()=='00:01:00'
    assert editor.locator('#mosaicRegionTools #mosaicBoxFields').is_visible()
    assert editor.locator('#mosaicMoreTools').count()==0
    assert editor.locator('#mosaicBoxFields').bounding_box()['y']>editor.locator('#mosaicCandidates').bounding_box()['y']
    assert abs(editor.locator('#mosaicStage').bounding_box()['height']-stage['height'])<1


def test_saving_one_region_preserves_sibling_region(editor):
    editor.evaluate("findings.push({...findings[0],id:'1',x:10,w:20})")
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-adopt]').click();editor.locator('[data-scan-event]').click()
    before=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3')))).targets")
    editor.locator('.mosaic-box').first.click()
    editor.locator('#mosaicBoxX').fill('45');editor.locator('#mosaicBoxX').dispatch_event('change');adopt_regions_if_needed(editor)
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert [t['motion'] for t in draft['targets']]==['fixed','fixed']
    changed=[i for i,(a,b) in enumerate(zip(before,draft['targets'])) if (a['x'],a['w'])!=(b['x'],b['w'])]
    assert len(changed)==1,'only the edited region moves; its sibling keeps its box'


@pytest.mark.parametrize('delete_selected', [False,True])
def test_save_time_preserves_cancelled_mask_and_delete_choice(editor,delete_selected):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal]').click();editor.locator('[data-target-remove]').click()
    if delete_selected:editor.locator('[data-event-delete]').click()
    editor.locator('[data-scan-event]').click();editor.locator('#mosaicStart').fill('00:01:10');adopt_regions_if_needed(editor)
    draft=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")
    assert draft['targets'][0]['adopted'] is False
    assert len(draft['deletions'])==int(delete_selected)
    assert draft['targets'][0]['start']==40


def test_old_empty_scan_on_open_is_muted_and_header_has_no_segments(editor):
    open_auto(editor)
    editor.wait_for_function("document.getElementById('mosaicScan').textContent==='重新识别整段'")
    assert editor.locator('#mosaicScanStatus').inner_text()=='上次识别未发现片段，可手动添加片段，或重新识别。'
    assert 'error' not in (editor.locator('#mosaicScanStatus').get_attribute('class') or '').split()
    assert editor.locator('#mosaicSummary').inner_text()=='删除 0 段 · 打码 0 段 · 还没有片段'
    assert editor.locator('#mosaicBatchStart').is_disabled()
    assert editor.locator('#helpNav').is_hidden()
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/scan')&&c.body)")


def test_empty_scan_session_messages_in_node():
    import subprocess
    script=(Path(__file__).parents[1]/'exactcut/web/mosaic.js').read_text()
    # Exercise the real status renderer and scan lifecycle without Chromium.
    paint=script[script.index('  function paintScan(){'):script.index('    const targetRow=')]+ '\n}'
    scan=script[script.index('  async function scanVideo('):script.index('  function reviewHint(')]
    summary=script[script.index('  function processingSummary('):script.index('  function paintBatch(')]
    harness="""
const assert=require('node:assert/strict');
const elements={},$=id=>elements[id]??=( {classList:{toggle(){}},textContent:''} );
const EditorBusy={message:x=>x,run:fn=>fn()},context={session:{source_sha256:'source',capabilities:{region_recognition:true}}};
let scanToken=0,scanLoading=false,scanReport=null,scanSessionId=null,scanError='',scanSubmitting=false;
let scanReadPending=false,scanController=null,loading=false,epoch=1,batchLoading=false;
let aiEvents=[],deletions=[],items=[],writes=0;
const allEvents=()=>aiEvents,timelineItems=()=>items,pendingJob=r=>['queued','running'].includes(r?.status);
const active=v=>v===epoch,base=()=>'/fixture',checkedReport=r=>r,awaitingDecision=()=>false;
const mergeDiscovery=events=>{aiEvents=events},status=()=>{},paint=()=>paintScan();
let report={id:'old',status:'completed',events:[]};
const api=async(url,body)=>body?(writes++,report={id:'fresh',status:'completed',events:[]}):{enabled:true,report};
"""+paint+scan+summary+"""
(async()=>{
  await scanVideo(false);
  assert.equal(writes,0);
  assert.equal($('mosaicScanStatus').textContent,'上次识别未发现片段，可手动添加片段，或重新识别。');
  assert.equal(processingSummary(0,0),'删除 0 段 · 打码 0 段 · 还没有片段');
  await scanVideo(true);
  assert.equal(writes,1);
  assert.ok($('mosaicScanStatus').textContent.startsWith('本次整段抽样未发现疑似时间段'));
  // A new editor session resets the in-memory scan identity.
  scanSessionId=null;scanReport=null;
  await scanVideo(false);
  assert.equal($('mosaicScanStatus').textContent,'上次识别未发现片段，可手动添加片段，或重新识别。');
  report=null;
  await scanVideo(false);
  assert.equal($('mosaicScanStatus').textContent,`可手动添加片段，或点「${$('mosaicScan').textContent}」。`);
  items=[{kind:'ignored'}];
  assert.ok(processingSummary(0,0).endsWith('全部已处理'));
  assert.ok(processingSummary(0,1).endsWith('待处理 1 段'));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run(['node','-e',harness],check=True,capture_output=True,text=True)


def test_open_never_submits_scan_and_button_submits_once_then_record_is_reused(editor):
    editor.evaluate('''() => {const old=api;let report=null;window.scanStarts=0;window.scanReads=0;
      window.api=async(url,body)=>{if(!url.endsWith('/scan'))return old(url,body);
        if(body){scanStarts++;report={id:'manual',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]};return report;}
        scanReads++;return {enabled:true,report};};}''')
    open_auto(editor)
    editor.wait_for_function('scanReads>=1')
    editor.wait_for_timeout(200)
    assert editor.evaluate('scanStarts')==0
    assert editor.locator('#mosaicScan').inner_text()=='识别整段视频'
    assert '识别整段视频' in editor.locator('#mosaicScanStatus').inner_text()
    assert editor.locator('#mosaicScanStatus').inner_text()=='可手动添加片段，或点「识别整段视频」。'
    assert '还没有片段' in editor.locator('#mosaicSummary').inner_text()
    assert editor.locator('#mosaicBatchStart').is_disabled()
    assert '识别中' not in editor.locator('#mosaicScan').inner_text()
    editor.locator('#mosaicScan').click()
    editor.wait_for_function('scanStarts===1')
    editor.wait_for_function("document.getElementById('mosaicScan').textContent==='重新识别整段'")
    assert '本次整段抽样未发现疑似时间段' in editor.locator('#mosaicScanStatus').inner_text()
    assert '还没有片段' in editor.locator('#mosaicSummary').inner_text()
    assert editor.locator('#mosaicBatchStart').is_disabled()
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    editor.wait_for_function("document.getElementById('mosaicScan').textContent==='重新识别整段'")
    assert editor.evaluate('scanStarts')==1
    assert editor.locator('#mosaicScanStatus').inner_text()=='上次识别未发现片段，可手动添加片段，或重新识别。'


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


def test_scan_record_read_waits_for_first_frame_and_never_submits(editor):
    editor.route('**/frame/*',lambda r:r.fulfill(status=503,body='not ready'))
    editor.evaluate('''()=>{const old=api;window.scanWrites=0;window.scanReads=0;window.api=async(url,body)=>{
      if(url.endsWith('/scan')){if(body){scanWrites++;return {id:'auto',status:'completed',events:[]};}scanReads++;return {enabled:true,report:null};}
      return old(url,body);};}''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.locator('#mosaicFrameRetry').wait_for(state='visible')
    assert editor.evaluate('scanReads+scanWrites')==0
    jpeg=io.BytesIO();Image.new('RGB',(160,96)).save(jpeg,'JPEG')
    editor.route('**/frame/*',lambda r:r.fulfill(body=jpeg.getvalue(),content_type='image/jpeg'))
    editor.locator('#mosaicFrameRetry').click()
    editor.wait_for_function('scanReads===1')
    editor.wait_for_timeout(200)
    assert editor.evaluate('scanWrites')==0


def test_resource_busy_open_retries_same_body(editor):
    editor.clock.install()
    editor.evaluate('''() => {Math.random=()=>0; const original=api;window.opens=[];
      window.api=async(url,body)=>{if(url.endsWith('/open')){opens.push(JSON.stringify(body));if(opens.length===1)throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',resource:'compute',retryable:true});}return original(url,body)};
    }''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.wait_for_function('opens.length===1')
    assert '正在重试' in editor.locator('#mosaicStatus').inner_text()
    editor.clock.run_for(1000)
    editor.wait_for_function('opens.length===2')
    assert editor.evaluate('opens[0]===opens[1]')
    editor.wait_for_function("!document.getElementById('mosaicWorkspace').hidden")


def test_close_cancels_busy_open_retry(editor):
    editor.clock.install()
    editor.evaluate('''() => {window.opens=0;window.api=async()=>{opens++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',resource:'compute',retryable:true});};}''')
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    editor.wait_for_function('opens===1')
    editor.locator('#mosaicClose').click()
    editor.clock.run_for(20000)
    assert editor.evaluate('opens') == 1


@pytest.mark.parametrize('outcome', ['old', 'new', 'failed'])
def test_scan_lost_response_queries_once_never_reposts(editor, outcome):
    open_editor(editor)
    editor.wait_for_function("!document.getElementById('mosaicScan').disabled")
    editor.evaluate('''outcome=>{window.writes=0;window.reads=0;const original=api;window.api=async(url,body)=>{
      if(!url.endsWith('/scan'))return original(url,body);
      if(body){writes++;throw Object.assign(Error('timeout'),{outcomeUnknown:true});}
      reads++;if(outcome==='failed')throw TypeError('offline');
      return {enabled:true,report:{id:outcome==='new'?'new-task':'existing',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]}};
    }}''', outcome)
    editor.locator('#mosaicScan').evaluate('(el)=>el.click()')
    editor.wait_for_function('reads===1')
    editor.wait_for_function("!document.getElementById('mosaicScan').disabled")
    assert editor.evaluate('writes') == 1
    if outcome != 'new':
        assert '待核实' in editor.locator('#mosaicScanStatus').inner_text()
    else:
        assert '已读取当前识别任务状态' in editor.locator('#mosaicStatus').inner_text()


@pytest.mark.parametrize('failure', ['disconnect', 'timeout'])
def test_real_api_scan_accepted_body_failure_reconciles_once(editor, failure):
    open_editor(editor)
    editor.wait_for_function("!document.getElementById('mosaicScan').disabled")
    editor.clock.install()
    editor.clock.pause_at(editor.evaluate('Date.now()'))
    source=(Path(__file__).parents[1]/'exactcut/web/app.js').read_text()
    api_source=source[source.index('async function api('):source.index('function positionToast(')]
    editor.evaluate('source=>{window.batchCache=[];window.api=eval("("+source+")")}', api_source)
    editor.evaluate('''failure=>{window.posts=0;window.gets=0;window.fetch=async(url,options)=>{
      if(options.method==='POST'){
        posts++;
        const body=new ReadableStream({start(controller){
          if(failure==='disconnect')controller.error(TypeError('body disconnected'));
          else options.signal.addEventListener('abort',()=>controller.error(new DOMException('timeout','AbortError')));
        }});
        return new Response(body,{status:202});
      }
      gets++;return new Response(JSON.stringify({enabled:true,report:{id:'new-task',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]}}));
    };$('mosaicScan').click();}''',failure)
    editor.wait_for_function('posts===1')
    if failure=='timeout':
        editor.clock.run_for(120000)
    editor.wait_for_function('gets===1')
    editor.wait_for_function("!document.getElementById('mosaicScan').disabled")
    assert editor.evaluate('posts')==1
    assert '已读取当前识别任务状态' in editor.locator('#mosaicStatus').inner_text()


def workbench_draft(page):
    return page.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3'))))")


def drag_box_pixels(page, locator, dx, dy):
    box=locator.bounding_box();overlay=page.locator('#mosaicOverlay').bounding_box()
    x=box['x']+box['width']/2;y=box['y']+box['height']/2
    page.mouse.move(x,y);page.mouse.down()
    page.mouse.move(x+dx*overlay['width']/160,y+dy*overlay['height']/96,steps=3);page.mouse.up()


def draw_unlocalized_segment(page):
    scan_result(page,needs_review=True);open_auto(page);page.locator('#mosaicScan').click()
    page.locator('[data-event-draw="e1"]').click()
    page.locator('#mosaicOverlay').wait_for(state='visible')
    bounds=page.locator('#mosaicOverlay').bounding_box()
    page.mouse.move(bounds['x']+bounds['width']*.25,bounds['y']+bounds['height']*.25);page.mouse.down()
    page.mouse.move(bounds['x']+bounds['width']*.5,bounds['y']+bounds['height']*.5);page.mouse.up()


def test_pointerup_adopts_mask_steps_move_resize_and_reopens_without_confirmation(editor):
    draw_unlocalized_segment(editor)
    card=editor.locator('[data-mosaic-card="e1"]')
    assert card.get_by_role('checkbox',name='打码',exact=True).is_checked()
    assert card.get_by_role('checkbox',name='打码',exact=True).get_attribute('aria-pressed')=='true'
    assert '打码 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert editor.locator('#mosaicConfirm').is_hidden()
    assert editor.locator('#mosaicStatus').inner_text()=='已打码，可继续拖动调整。'
    before=workbench_draft(editor)['targets'][0]
    assert [before[k] for k in ('x','y','w','h')]==[40,24,40,24]
    for _ in range(5):editor.locator('#mosaicNext').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert card.locator('[data-scan-event]').get_attribute('aria-pressed')=='true'
    assert editor.locator('.mosaic-box.selected .mosaic-handle:visible').count()==8
    drag_box_pixels(editor,editor.locator('.mosaic-box.selected'),10,5)
    assert editor.locator('#mosaicStatus').inner_text()=='区域已更新。'
    moved=workbench_draft(editor)['targets'][0]
    assert moved['uid']==before['uid'] and moved['x']==50 and moved['y']==29
    drag_box_pixels(editor,editor.locator('[data-resize="se"]'),5,5)
    resized=workbench_draft(editor)['targets'][0]
    assert resized['w']==45 and resized['h']==29
    editor.locator('#mosaicClose').click();open_auto(editor)
    card.locator('[data-scan-event]').click();editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert [int(editor.locator('#mosaicBox'+k).input_value()) for k in 'XYWH']==[50,29,45,29]
    assert workbench_draft(editor)['targets'][0]==resized


@pytest.mark.parametrize('navigation',['previous','next','jump','slider','timeline'])
def test_navigation_clears_selection_only_outside_half_open_clip_range(editor,navigation):
    draw_unlocalized_segment(editor)
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('#mosaicStatus').inner_text()=='拖动框可调整位置，整段使用同一位置。'
    frame=editor.locator('#mosaicFrameNumber')
    frame.fill('00:01:00');frame.dispatch_event('change')
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('[data-scan-event="e1"]').get_attribute('aria-pressed')=='true'
    if navigation=='previous':editor.locator('#mosaicPrevious').click()
    elif navigation=='next':
        frame.fill('00:02:29');frame.dispatch_event('change')
        editor.locator('#mosaicOverlay').wait_for(state='visible');editor.locator('#mosaicNext').click()
    elif navigation=='jump':frame.fill('00:03:00');frame.dispatch_event('change')
    elif navigation=='slider':editor.locator('#mosaicSeek').evaluate("el=>{el.value=90;el.dispatchEvent(new Event('input'))}")
    else:
        timeline=editor.locator('#mosaicTimeline').bounding_box()
        editor.mouse.click(timeline['x']+timeline['width']*.8,timeline['y']+timeline['height']-2)
    assert editor.locator('[data-scan-event="e1"]').get_attribute('aria-pressed')=='false'
    assert editor.locator('#mosaicOverlay').is_hidden()
    assert editor.locator('#mosaicConfirm').is_hidden()
    assert not editor.locator('#mosaicAdjust').evaluate('el=>el.open')
    assert editor.locator('#mosaicStart').is_hidden()
    assert editor.locator('#mosaicEnd').is_hidden()
    assert editor.locator('#mosaicRegionTools').is_hidden()
    assert editor.locator('#mosaicCandidates button').count()==0
    assert editor.locator('#mosaicBoxX').input_value()==''
    assert editor.locator('#mosaicStart').input_value()=='00:00:00'
    assert editor.locator('#mosaicEnd').input_value()=='00:09:29'
    assert active_count(editor)==1
    assert editor.locator('#mosaicStatus').inner_text()=='点击片段查看画面，选择删除、打码或忽略。'


def test_time_and_every_coordinate_change_persist_without_confirmation(editor):
    draw_unlocalized_segment(editor)
    for axis,value in [('X',42),('Y',25),('W',35),('H',20)]:
        field=editor.locator('#mosaicBox'+axis);field.fill(str(value));field.dispatch_event('change')
        assert workbench_draft(editor)['targets'][0][axis.lower()]==value
    for field,value,key,expected in [('mosaicStart','00:01:10','start',40),('mosaicEnd','00:02:10','end',71)]:
        editor.locator('#'+field).fill(value);editor.locator('#'+field).dispatch_event('change')
        assert workbench_draft(editor)['targets'][0][key]==expected
        assert workbench_draft(editor)['clipEdits']['e1'][key]==expected


def test_manual_add_defers_commit_and_remove_refreshes_status(editor):
    open_auto(editor);editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic');editor.locator('#mosaicAddRegion').click()
    box=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(box['x']+20,box['y']+20);editor.mouse.down()
    editor.mouse.move(box['x']+80,box['y']+60);editor.mouse.up()
    assert active_count(editor)==0
    editor.locator('#mosaicAddSubmit').click();assert active_count(editor)==1
    editor.locator('[data-remove-clip]').click()
    assert editor.locator('#mosaicStatus').inner_text()=='已移除片段。'
    assert editor.locator('#mosaicRemoved').is_visible()


def test_pointerup_reports_region_limit_without_writing_target(editor):
    editor.evaluate("findings=Array.from({length:8},(_,i)=>({...findings[0],id:String(i)}))")
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:100,end:150,seed_frame:null,status:'needs_review',candidates:[]})")
    open_auto(editor);editor.locator('#mosaicScan').click();editor.locator('[data-event-adopt="e1"]').click()
    assert active_count(editor)==8
    before=workbench_draft(editor)
    editor.locator('[data-event-draw="e2"]').click();editor.locator('#mosaicOverlay').wait_for(state='visible')
    box=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(box['x']+20,box['y']+20);editor.mouse.down()
    editor.mouse.move(box['x']+80,box['y']+60);editor.mouse.up()
    assert '最多打码 8 个区域' in editor.locator('#mosaicStatus').inner_text()
    assert editor.locator('#mosaicStatus').evaluate("el=>el.classList.contains('error')")
    assert workbench_draft(editor)['targets']==before['targets']
    assert workbench_draft(editor)['deletions']==before['deletions']
    assert workbench_draft(editor)['drawingEvents']==['e2']
    assert editor.locator('[data-event-draw="e2"]').get_attribute('aria-pressed')=='true'


def test_play_pause_restores_handles_in_range_and_clears_selection_after_end(editor,portrait_source):
    open_auto(editor);editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicEnd').fill('00:01:24');editor.locator('#mosaicManualAction').select_option('mosaic')
    editor.locator('#mosaicAddRegion').click();box=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(box['x']+20,box['y']+20);editor.mouse.down()
    editor.mouse.move(box['x']+80,box['y']+60);editor.mouse.up();editor.locator('#mosaicAddSubmit').click()
    editor.wait_for_function("document.querySelector('#mosaicSource').readyState>=2")
    editor.locator('#mosaicPlay').click()
    editor.wait_for_function("!document.querySelector('#mosaicSource').paused && document.querySelector('#mosaicSource').currentTime>0.1")
    assert editor.locator('#mosaicOverlay').is_hidden()
    editor.locator('#mosaicPlay').click();editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert editor.locator('[data-scan-event]').get_attribute('aria-pressed')=='true'
    assert editor.locator('.mosaic-box.selected .mosaic-handle:visible').count()==8
    editor.locator('#mosaicPlay').click()
    editor.wait_for_function("document.querySelector('#mosaicSource').ended")
    assert editor.locator('[data-scan-event]').get_attribute('aria-pressed')=='false'
    assert editor.locator('#mosaicOverlay').is_hidden()
    assert editor.locator('#mosaicStatus').inner_text()=='点击片段查看画面，选择删除、打码或忽略。'


def test_adjusting_cancelled_mask_adopts_it_again_but_time_edit_does_not(editor):
    draw_unlocalized_segment(editor)
    editor.locator('[data-target-remove]').click()
    assert editor.locator('#mosaicRegionTools').is_visible()
    assert editor.locator('#mosaicBoxX').is_visible()
    editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicStart').dispatch_event('change')
    assert workbench_draft(editor)['targets'][0]['adopted'] is False
    editor.locator('#mosaicBoxX').fill('45');editor.locator('#mosaicBoxX').dispatch_event('change')
    target=workbench_draft(editor)['targets'][0]
    assert target['x']==45 and target['adopted'] is True
    assert editor.locator('[data-target-remove]').get_attribute('aria-pressed')=='true'


def test_dragging_unadopted_ai_candidate_adopts_it(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    assert active_count(editor)==0
    drag_box_pixels(editor,editor.locator('.mosaic-box.selected'),5,3)
    target=workbench_draft(editor)['targets'][0]
    assert target['adopted'] is True and target['x']==45 and target['y']==27
    assert editor.locator('[data-mosaic-card="e1"]').get_by_role('checkbox',name='打码',exact=True).is_checked()


def test_drawing_on_deleted_ai_segment_adopts_mask_and_clears_deletion(editor):
    scan_result(editor,needs_review=True);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-event-delete="e1"]').click()
    editor.locator('[data-event-draw="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    box=editor.locator('#mosaicOverlay').bounding_box()
    editor.mouse.move(box['x']+20,box['y']+20);editor.mouse.down()
    editor.mouse.move(box['x']+80,box['y']+60);editor.mouse.up()
    draft=workbench_draft(editor)
    assert draft['deletions']==[] and draft['targets'][0]['adopted'] is True
    assert 'mosaic' in editor.locator('[data-timeline-event="e1"]').get_attribute('class')


def test_time_only_edit_keeps_unprocessed_ai_segment_as_timeline_suggestion(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicStart').fill('00:01:10');editor.locator('#mosaicStart').dispatch_event('change')
    draft=workbench_draft(editor)
    assert draft['clipEdits']['e1']['start']==40
    assert draft['targets']==[] and draft['deletions']==[]
    assert editor.locator('[data-timeline-event]').count()==1
    assert 'suggestion' in editor.locator('[data-timeline-event]').get_attribute('class')
    assert editor.locator('#mosaicStatus').inner_text()=='时间已更新。'


def test_timeline_contains_all_states_and_suggestions_only_select(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events=Array.from({length:3},(_,i)=>({...scanJob.events[0],id:'e'+(i+1),start:30+i*90,end:60+i*90,seed_frame:45+i*90}))")
    open_auto(editor);editor.locator('#mosaicScan').click()
    editor.wait_for_function("document.querySelectorAll('[data-timeline-event]').length===3")
    assert editor.locator('[data-timeline-event]').count()==editor.locator('[data-mosaic-card]').count()
    suggestion=editor.locator('[data-timeline-event="e2"]')
    assert 'suggestion' in suggestion.get_attribute('class')
    assert 'AI 建议 · 未处理' in suggestion.get_attribute('aria-label')
    assert 'AI 建议 · 未处理' in suggestion.get_attribute('title')
    assert suggestion.evaluate("el=>getComputedStyle(el).borderTopStyle")=='dashed'
    assert suggestion.locator('[data-edge]').count()==0
    suggestion.click()
    assert editor.locator('[data-scan-event="e2"]').get_attribute('aria-pressed')=='true'
    assert editor.locator('#mosaicReviewInfo').inner_text()=='片段 2 / 3'
    # Even a drag across the suggestion must not write a range edit.
    bounds=suggestion.bounding_box()
    editor.mouse.move(bounds['x']+2,bounds['y']+bounds['height']/2);editor.mouse.down()
    editor.mouse.move(bounds['x']+35,bounds['y']+bounds['height']/2,steps=3);editor.mouse.up()
    assert suggestion.get_attribute('data-start')=='120'
    assert suggestion.get_attribute('data-end')=='150'
    assert workbench_draft(editor)['clipEdits']=={}
    editor.locator('[data-proposal="e1:0"]').click()
    editor.locator('[data-event-delete="e3"]').click()
    assert editor.locator('[data-timeline-event]').count()==3
    assert 'mosaic' in editor.locator('[data-timeline-event="e1"]').get_attribute('class')
    assert '打码' in editor.locator('[data-timeline-event="e1"]').get_attribute('title')
    assert 'deletion' in editor.locator('[data-timeline-event="e3"]').get_attribute('class')
    assert '删除' in editor.locator('[data-timeline-event="e3"]').get_attribute('aria-label')
    assert 'suggestion' in suggestion.get_attribute('class')


def test_manual_segment_inserts_between_ai_segments_and_navigation_uses_time_order(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:210,end:270,seed_frame:240})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    editor.wait_for_function("document.querySelectorAll('[data-mosaic-card]').length===2")
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicStart').fill('00:04:00');editor.locator('#mosaicEnd').fill('00:05:29')
    editor.locator('#mosaicAddSubmit').click()
    ids=editor.locator('[data-mosaic-card]').evaluate_all('cards=>cards.map(c=>c.dataset.mosaicCard)')
    manual=ids[1]
    assert ids[0]=='e1' and manual.startswith('manual-') and ids[2]=='e2'
    assert editor.locator('[data-timeline-event]').count()==3
    assert 'deletion' in editor.locator(f'[data-timeline-event="{manual}"]').get_attribute('class')
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('#mosaicReviewInfo').inner_text()=='片段 1 / 3'
    editor.locator('#mosaicNextEvent').click()
    assert editor.locator(f'[data-scan-event="{manual}"]').get_attribute('aria-pressed')=='true'
    assert editor.locator('#mosaicReviewInfo').inner_text()=='片段 2 / 3'
    editor.locator('#mosaicNextEvent').click()
    assert editor.locator('[data-scan-event="e2"]').get_attribute('aria-pressed')=='true'
    assert editor.locator('#mosaicReviewInfo').inner_text()=='片段 3 / 3'
    editor.locator('#mosaicPrevEvent').click()
    assert editor.locator(f'[data-scan-event="{manual}"]').get_attribute('aria-pressed')=='true'
    # Cancelling a manual action also leaves a selectable unprocessed timeline block.
    editor.locator(f'[data-event-delete="{manual}"]').click()
    assert 'suggestion' in editor.locator(f'[data-timeline-event="{manual}"]').get_attribute('class')
    assert '手动片段 · 未处理' in editor.locator(f'[data-timeline-event="{manual}"]').get_attribute('title')
    editor.locator('#mosaicClose').click();open_auto(editor)
    assert editor.locator('[data-mosaic-card]').evaluate_all('cards=>cards.map(c=>c.dataset.mosaicCard)')==ids


def test_playing_unadopted_candidate_draws_dashes_without_effect(editor):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-scan-event="e1"]').click()
    editor.locator('#mosaicOverlay').wait_for(state='visible')
    # Exercise the playing-video draw path deterministically, using the loaded frame as video pixels.
    editor.evaluate('''() => {
      const video=$('mosaicSource'),ctx=$('mosaicLiveCanvas').getContext('2d'),draw=ctx.drawImage.bind(ctx);
      ctx.drawImage=(source,...args)=>draw(source===video?$('mosaicFrame'):source,...args);
      Object.defineProperty(video,'paused',{configurable:true,value:false});
      Object.defineProperty(video,'readyState',{configurable:true,value:4});
      Object.defineProperty(video,'currentTime',{configurable:true,value:2});
      window.strokes=[];const stroke=ctx.strokeRect.bind(ctx);
      ctx.strokeRect=(...args)=>{strokes.push({dash:ctx.getLineDash(),color:ctx.strokeStyle,box:args});stroke(...args)};
      $('mosaicFrame').hidden=true;video.dispatchEvent(new Event('timeupdate'));
    }''')
    assert editor.locator('#mosaicOverlay').is_hidden()
    assert editor.locator('#mosaicLiveCanvas.has-suggestions').is_visible()
    assert editor.evaluate("strokes.some(s=>s.dash.join(',')==='6,4' && s.color==='#ffc400' && s.box.join(',')==='40,24,80,48')")
    assert editor.evaluate('''() => {
      const original=document.createElement('canvas');original.width=160;original.height=96;
      original.getContext('2d').drawImage($('mosaicFrame'),0,0);
      const a=$('mosaicLiveCanvas').getContext('2d').getImageData(50,34,60,28).data;
      const b=original.getContext('2d').getImageData(50,34,60,28).data;
      return a.every((v,i)=>v===b[i]);
    }''')
    assert active_count(editor)==0
    editor.evaluate("() => {for(const key of ['paused','readyState','currentTime'])delete $('mosaicSource')[key]}")


@pytest.mark.parametrize('kind', ['ai', 'manual'])
@pytest.mark.parametrize('commit', ['change', 'enter', 'blur'])
def test_time_formats_commit_immediately_for_both_segment_types(editor, kind, commit):
    editor.evaluate('session.metadata.frame_count=3600')
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    if kind=='manual':
        editor.locator('#mosaicAddClip').click()
        editor.locator('#mosaicStart').fill('1:00');editor.locator('#mosaicEnd').fill('1:10')
        editor.locator('#mosaicAddSubmit').click()
        segment=workbench_draft(editor)['manualClips'][0]['id']
        editor.locator(f'[data-scan-event="{segment}"]').click()
    else:
        segment='e1';editor.locator('[data-proposal="e1:0"]').click()
    end=editor.locator('#mosaicEnd');end.fill('1:05')
    if commit=='change':end.dispatch_event('change')
    elif commit=='enter':end.press('Enter')
    else:editor.locator('#mosaicTitle').click()
    assert workbench_draft(editor)['clipEdits'][segment]['end']==1951
    start=editor.locator('#mosaicStart');start.fill('00:02:05');start.press('Enter')
    assert workbench_draft(editor)['clipEdits'][segment]['start']==65
    jump=editor.locator('#mosaicFrameNumber');jump.fill('1:05');jump.press('Enter')
    assert jump.input_value()=='01:05:00'
    jump.fill('65');jump.dispatch_event('change');assert jump.input_value()=='01:05:00'
    jump.fill('0:05');jump.press('Enter');assert jump.input_value()=='00:05:00'
    jump.fill('0:9');jump.press('Enter');assert jump.input_value()=='00:09:00'
    jump.fill('1:99');jump.press('Enter')
    assert '请输入' in editor.locator('#mosaicStatus').inner_text()
    assert jump.input_value()=='00:09:00'
    start.fill('0:9');start.press('Enter')
    assert workbench_draft(editor)['clipEdits'][segment]['start']==270
    start.fill('0:99');start.press('Enter')
    assert '请输入' in editor.locator('#mosaicStatus').inner_text()
    assert start.input_value()=='00:09:00'
    end.fill('0:8');end.press('Enter')
    assert '开始帧不能晚于结束帧' in editor.locator('#mosaicStatus').inner_text()
    assert end.input_value()=='01:05:00'
    assert workbench_draft(editor)['clipEdits'][segment]=={'start':270,'end':1951}


def timeline_point(page, frame):
    rail=page.locator('#mosaicTimelineTracks').bounding_box()
    total=page.evaluate('session.metadata.frame_count')
    return rail['x']+rail['width']*frame/total, rail['y']+rail['height']-3


@pytest.mark.parametrize('edge,expected', [('start',60),('end',119)])
def test_trim_edge_follows_frame_before_release_and_stops_at_neighbour(editor, edge, expected):
    scan_result(editor)
    editor.evaluate("scanJob.events=[{...scanJob.events[0],id:'left',start:0,end:60,seed_frame:30},{...scanJob.events[0],start:70,end:100,seed_frame:80},{...scanJob.events[0],id:'right',start:120,end:160,seed_frame:140}]")
    open_auto(editor);editor.locator('#mosaicScan').click();editor.locator('[data-proposal="e1:0"]').click()
    editor.locator('#mosaicFrame').wait_for(state='visible')
    editor.clock.install();editor.clock.pause_at(editor.evaluate('Date.now()'))
    frames=[]
    editor.on('request',lambda request: frames.append(request.url) if '/frame/' in request.url else None)
    handle=editor.locator(f'[data-timeline-event="e1"] [data-edge="{edge}"]').bounding_box()
    rail=editor.locator('#mosaicTimelineTracks').bounding_box()
    x=handle['x']+handle['width']/2;y=handle['y']+handle['height']/2
    editor.mouse.move(x,y);editor.mouse.down()
    editor.mouse.move(x+(-100 if edge=='start' else 100)*rail['width']/300,y,steps=10)
    assert editor.locator('#mosaicFrameNumber').input_value()==('00:02:00' if edge=='start' else '00:03:29')
    assert abs(editor.locator('#mosaicTimelineHead').evaluate('el=>parseFloat(el.style.left)')-100*expected/300)<.01
    # No range is committed until release, and the debounced accurate frame follows the edge.
    assert workbench_draft(editor)['targets'][0]['start']==70
    assert workbench_draft(editor)['targets'][0]['end']==100
    assert frames==[]  # pointermove only schedules the shared 100ms frame reader
    editor.clock.run_for(100)
    editor.locator('#mosaicFrame').wait_for(state='visible')
    assert len(frames)==1 and frames[0].endswith('/frame/'+str(expected))
    editor.mouse.up()
    draft=workbench_draft(editor)
    assert draft['clipEdits']['e1']['start' if edge=='start' else 'end']==(60 if edge=='start' else 120)
    assert '边界' in editor.locator('#mosaicStatus').inner_text()
    assert editor.locator('#mosaicFrameNumber').input_value()==('00:02:00' if edge=='start' else '00:03:29')


@pytest.mark.parametrize('start,end', [(100,180),(180,60),(180,300),(180,0)])
def test_blank_timeline_drag_scrubs_frame_before_release_without_creating_clip(editor, start, end):
    scan_result(editor);open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('[data-timeline-event="e1"]').wait_for()
    before=workbench_draft(editor)
    editor.clock.install();editor.clock.pause_at(editor.evaluate('Date.now()'))
    frames=[]
    editor.on('request',lambda request: frames.append(request.url) if '/frame/' in request.url else None)
    x,y=timeline_point(editor,start);end_x,_=timeline_point(editor,end)
    editor.mouse.move(x,y);editor.mouse.down()
    def check(frame):
        assert editor.locator('#mosaicFrameNumber').input_value()==f'00:{frame//30:02d}:{frame%30:02d}'
        assert abs(editor.locator('#mosaicTimelineHead').evaluate('el=>parseFloat(el.style.left)')-100*frame/300)<.01
        editor.clock.run_for(100)
        editor.locator('#mosaicFrame').wait_for(state='visible')
        assert frames[-1].endswith('/frame/'+str(frame))
    check(start)  # Pointerdown jumps immediately and loads the accurate preview.
    middle=(start+end)//2;mid_x,_=timeline_point(editor,middle)
    editor.mouse.move(mid_x,y,steps=6);check(middle)
    editor.mouse.move(end_x,y,steps=6);check(min(299,end))
    # Preview and playhead follow while the pointer is still held, even across AI ranges.
    assert editor.locator('[data-timeline-event]').count()==1
    assert editor.locator('[data-manual-card]').count()==0
    assert workbench_draft(editor)==before
    editor.mouse.up()
    assert workbench_draft(editor)==before
    assert editor.locator('[data-timeline-event]').count()==1
    assert editor.locator('#mosaicTimelineSelection').count()==0
    assert editor.locator('#helpNav').is_hidden()


@pytest.mark.parametrize('width', [1440, 1600])
def test_header_summary_is_visible_and_updates_without_wrapping_buttons(editor, width):
    editor.set_viewport_size({'width':width,'height':900})
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:210,end:270,seed_frame:240})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    summary=editor.locator('#mosaicSummary')

    def check(text, pending):
        assert summary.is_visible()
        assert summary.get_attribute('role')=='status'
        assert summary.inner_text()==text
        assert editor.locator('#mosaicBatchHint').inner_text().split('\n')[0]==text
        assert ('pending' in (summary.get_attribute('class') or '').split())==pending
        # Check real layout: summary precedes discard; all controls stay on one row
        # inside the header, with enough width to render each button's text.
        assert editor.locator('.mosaic-header').evaluate('''header => {
          const s=document.getElementById('mosaicSummary').getBoundingClientRect();
          const discard=document.getElementById('mosaicDiscard').getBoundingClientRect();
          const h=header.getBoundingClientRect();
          const buttons=[...header.querySelectorAll('button')].filter(b=>b.getBoundingClientRect().width>0);
          return s.width>0 && s.height>0 && s.right<=discard.left && s.left>=h.left &&
            buttons.every(b=>{const r=b.getBoundingClientRect();return r.top>=h.top && r.bottom<=h.bottom &&
              r.right<=h.right && r.width>=b.scrollWidth && Math.abs((r.top+r.bottom-discard.top-discard.bottom)/2)<2;});
        }''')

    check('删除 0 段 · 打码 0 段 · 待处理 2 段', True)
    pending_color=summary.evaluate('el=>getComputedStyle(el).color')
    card=editor.locator('[data-mosaic-card="e1"]')
    card.get_by_role('checkbox',name='删除',exact=True).click()
    check('删除 1 段 · 打码 0 段 · 待处理 1 段', True)
    card.get_by_role('checkbox',name='打码',exact=True).click()
    check('删除 0 段 · 打码 1 段 · 待处理 1 段', True)
    editor.locator('[data-event-ignore="e2"]').click()
    check('删除 0 段 · 打码 1 段 · 全部已处理', False)
    assert summary.evaluate('el=>getComputedStyle(el).color')!=pending_color
    editor.locator('[data-event-ignore="e2"]').click()
    check('删除 0 段 · 打码 1 段 · 待处理 1 段', True)


def test_three_choices_ignore_excludes_masks_deletions_and_persists_in_time_order(editor):
    scan_result(editor)
    editor.evaluate("scanJob.events.push({...scanJob.events[0],id:'e2',start:210,end:270,seed_frame:240})")
    open_auto(editor);editor.locator('#mosaicScan').click()
    card=editor.locator('[data-mosaic-card="e1"]')
    assert '待处理 2 段' in editor.locator('#mosaicBatchHint').inner_text()
    for choice in ['打码','删除','忽略']:
        card.get_by_role('checkbox',name=choice,exact=True).click()
        assert card.locator('[aria-checked="true"]').count()==1
        assert card.get_by_role('checkbox',name=choice,exact=True).is_checked()
    draft=workbench_draft(editor)
    assert draft['ignoredEvents']==['e1'] and draft['deletions']==[]
    assert all(t['adopted'] is False for t in draft['targets'])
    assert 'ignored' in card.get_attribute('class')
    assert 'ignored' in editor.locator('[data-timeline-event="e1"]').get_attribute('class')
    assert '待处理 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.reload();install_editor(editor);scan_result(editor);open_auto(editor)
    assert card.get_by_role('checkbox',name='忽略',exact=True).is_checked()
    assert editor.locator('[data-mosaic-card]').evaluate_all('els=>els.map(e=>e.dataset.mosaicCard)')==['e1','e2']
    editor.locator('[data-event-ignore="e2"]').click()
    assert '全部已处理' in editor.locator('#mosaicBatchHint').inner_text()
    card.get_by_role('checkbox',name='忽略',exact=True).click()
    assert card.locator('[aria-checked="true"]').count()==0
    assert '待处理 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    card.get_by_role('checkbox',name='打码',exact=True).click()
    assert workbench_draft(editor)['ignoredEvents']==['e2']
    assert active_count(editor)==1


def test_mask_choice_without_box_is_pending_and_can_be_toggled_off(editor):
    scan_result(editor,needs_review=True);open_auto(editor);editor.locator('#mosaicScan').click()
    card=editor.locator('[data-mosaic-card="e1"]')
    assert '需框选' not in card.inner_text()
    for enabled in [True,False]:
        card.get_by_role('checkbox',name='打码',exact=True).click()
        assert card.get_by_role('checkbox',name='打码',exact=True).is_checked()==enabled
        assert ('需框选' in card.inner_text())==enabled
        assert '待处理 1 段' in editor.locator('#mosaicBatchHint').inner_text()
        assert '打码 0 段' in editor.locator('#mosaicBatchHint').inner_text()


@pytest.mark.parametrize('note,expected', [
    ('未获得有效区域框；时间段仍保留，不能视作无风险。','AI 没框出区域，请在画面上拖出打码区域。'),
    ('代表帧定位失败，需人工复核。','AI 没找到清晰画面，请换一帧后拖出打码区域。'),
    ('候选框仅对应自动抽样帧，不代表整个时间段覆盖。','请播放检查，确认打码区域覆盖整段。'),
    ('unknown internal diagnostic','AI 没框出区域，请在画面上拖出打码区域。'),
])
def test_old_and_unknown_review_notes_are_mapped_on_reopen(editor,note,expected):
    scan_result(editor,needs_review=True);editor.evaluate('note=>scanJob.events[0].review_note=note',note)
    open_auto(editor);editor.locator('#mosaicScan').click()
    editor.locator('#mosaicClose').click();open_auto(editor)
    editor.locator('[data-scan-event="e1"]').click()
    assert editor.locator('#mosaicStatus').inner_text()==expected


def test_keyboard_navigation_obeys_editor_focus_and_effect_dialog(editor):
    draw_unlocalized_segment(editor)
    editor.locator('#mosaicTitle').click()
    before=editor.locator('#mosaicFrameNumber').input_value()
    editor.keyboard.press('ArrowRight')
    assert editor.locator('#mosaicFrameNumber').input_value()!=before
    assert editor.locator('[data-scan-event="e1"]').get_attribute('aria-pressed')=='true'
    editor.keyboard.press('ArrowLeft');assert editor.locator('#mosaicFrameNumber').input_value()==before
    editor.locator('#mosaicStart').focus();editor.keyboard.press('ArrowRight')
    assert editor.locator('#mosaicFrameNumber').input_value()==before
    editor.evaluate("$('mosaicEffects').showModal()")
    editor.keyboard.press('ArrowRight');assert editor.locator('#mosaicFrameNumber').input_value()==before
    editor.evaluate("$('mosaicEffects').close()")
    # Count play/pause invocations without relying on a playable video fixture.
    editor.evaluate("() => {window.playClicks=0;$('mosaicPlay').onclick=()=>playClicks++}")
    editor.locator('#mosaicTitle').click();editor.keyboard.press('Space');assert editor.evaluate('playClicks')==1
    editor.locator('#mosaicStart').focus();editor.keyboard.press('Space');assert editor.evaluate('playClicks')==1
    editor.locator('#mosaicPlay').focus();editor.keyboard.press('Space');assert editor.evaluate('playClicks')==2
    editor.locator('#mosaicPlay').press('Enter');assert editor.evaluate('playClicks')==3
    editor.locator('#mosaicTitle').click()
    editor.keyboard.down('Space');assert editor.evaluate('playClicks')==4
    editor.keyboard.down('Space');assert editor.evaluate('playClicks')==4
    # Releasing a shortcut must not activate a newly focused native button.
    editor.locator('#mosaicPlay').focus();editor.keyboard.up('Space');assert editor.evaluate('playClicks')==4
    editor.locator('#mosaicTitle').click();editor.keyboard.press('Space');assert editor.evaluate('playClicks')==5
    editor.locator('#mosaicClose').click();editor.keyboard.press('Space');assert editor.evaluate('playClicks')==5


def test_short_timeline_suggestion_hides_entire_label_but_keeps_accessible_information(editor):
    scan_result(editor);editor.evaluate('scanJob.events[0].end=31;scanJob.events[0].seed_frame=30')
    open_auto(editor);editor.locator('#mosaicScan').click()
    block=editor.locator('[data-timeline-event="e1"]')
    assert block.locator('.mosaic-block-label').is_hidden()
    assert 'AI 建议 · 未处理' in block.get_attribute('title')
    assert '疑似敏感画面' in block.get_attribute('aria-label')


def test_manual_ignore_and_drafts_without_optional_decision_fields(editor):
    open_auto(editor);editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicStart').fill('00:03:00');editor.locator('#mosaicEnd').fill('00:04:29')
    editor.locator('#mosaicAddSubmit').click()
    clip=workbench_draft(editor)['manualClips'][0]['id']
    editor.locator(f'[data-event-delete="{clip}"]').click()
    editor.locator(f'[data-event-ignore="{clip}"]').click()
    editor.reload();install_editor(editor);scan_result(editor);open_auto(editor)
    assert editor.locator(f'[data-event-ignore="{clip}"]').is_checked()
    assert workbench_draft(editor)['deletions']==[] and workbench_draft(editor)['targets']==[]
    editor.locator('#mosaicClose').click()
    editor.evaluate('''() => {
      const key=Object.keys(localStorage).find(k=>k.includes('workbench.v3')),draft=JSON.parse(localStorage.getItem(key));
      delete draft.ignoredEvents;delete draft.drawingEvents;localStorage.setItem(key,JSON.stringify(draft));
    }''')
    open_auto(editor)
    assert not editor.locator(f'[data-event-ignore="{clip}"]').is_checked()
    assert '待处理 1 段' in editor.locator('#mosaicBatchHint').inner_text()


def hold_main_video(page):
    # Keep main playback out of thumbnail requests; advertise an OSS currentSrc
    # so a regression to extracting from the playing URL is observable.
    page.evaluate('''() => {
      const v=$('mosaicSource');let src='';
      Object.defineProperty(v,'src',{get:()=>src,set:value=>{src=value;}});
      Object.defineProperty(v,'currentSrc',{get:()=> 'https://media.test/main.mp4'});
      v.load=()=>{};
    }''')


def mock_filmstrip(page, *, running=False):
    jpeg=io.BytesIO();Image.new('RGB',(224,116),'#22cc44').save(jpeg,'JPEG')
    page.route('**/filmstrip/image*',lambda r:r.fulfill(body=jpeg.getvalue(),content_type='image/jpeg'))
    page.evaluate('''running => {
      window.filmstripCalls=0;window.filmstripReady=!running;
      const original=api;
      window.api=async(url,...args)=>{
        if(url.endsWith('/filmstrip')){
          filmstripCalls++;
          if(!filmstripReady)return {status:'running'};
          return {status:'ready',source_sha256:session.source_sha256,count:2,columns:2,rows:1,tile_width:112,tile_height:116,frames:[75,225],url:'/api/batches/batch/mosaics/session/filmstrip/image?v=v1'};
        }
        return original(url,...args);
      };
    }''',running)


def wait_green_thumbnails(page):
    page.wait_for_function('''() => {
      const cs=[...document.querySelectorAll('#mosaicTimelineThumbs canvas')];
      return cs.length&&cs.every(c=>{const p=c.getContext('2d').getImageData(1,1,1,1).data;return p[1]>180&&p[0]<60&&p[3]===255;});
    }''')


def test_filmstrip_ready_draws_without_video_requests_and_reuses_on_resize(editor):
    hold_main_video(editor);mock_filmstrip(editor)
    requests=[];editor.on('request',lambda r:requests.append(r.url))
    open_auto(editor);wait_green_thumbnails(editor)
    first_count=editor.locator('#mosaicTimelineThumbs canvas').count()
    editor.set_viewport_size({'width':1200,'height':900})
    editor.wait_for_function('count=>document.querySelectorAll("#mosaicTimelineThumbs canvas").length!==count',arg=first_count)
    wait_green_thumbnails(editor)
    assert editor.evaluate('filmstripCalls')==1
    assert len([u for u in requests if '/filmstrip/image' in u])==1
    assert not any(u.endswith('/source') or 'media.test' in u for u in requests)


def test_filmstrip_running_then_ready_refreshes_canvases(editor):
    hold_main_video(editor);mock_filmstrip(editor,running=True)
    open_auto(editor)
    editor.wait_for_function('window.filmstripCalls>=1')
    assert editor.locator('#mosaicTimelineThumbs canvas').first.evaluate("c=>c.getContext('2d').getImageData(1,1,1,1).data[3]")==0
    editor.evaluate('filmstripReady=true')
    wait_green_thumbnails(editor)
    assert editor.evaluate('filmstripCalls')>=2


@pytest.mark.parametrize('failure',['failed','request_error'])
def test_filmstrip_failed_falls_back_to_local_source_with_oss_main(editor,portrait_source,failure):
    hold_main_video(editor)
    if failure=='request_error':
        editor.evaluate("() => {const original=api;window.api=async(url,...args)=>{if(url.endsWith('/filmstrip'))throw Error('synthetic network failure');return original(url,...args);};}")
    requests=[];editor.on('request',lambda r:requests.append(r.url))
    open_auto(editor)
    editor.locator('#mosaicTimelineThumbs canvas').first.wait_for()
    editor.wait_for_function("[...document.querySelectorAll('#mosaicTimelineThumbs canvas')].every(c=>c.getContext('2d').getImageData(1,1,1,1).data[0]>200)")
    assert any(u.endswith('/source') for u in requests)
    assert not any('media.test' in u for u in requests)


def test_filmstrip_polling_stops_on_close(editor):
    hold_main_video(editor)
    editor.evaluate('''() => {
      window.filmstripCalls=0;const original=api;
      window.api=async(url,...args)=>{if(url.endsWith('/filmstrip')){filmstripCalls++;return {status:'running'};}return original(url,...args);};
    }''')
    open_auto(editor);editor.wait_for_function('filmstripCalls>=1')
    editor.locator('#mosaicClose').click()
    calls=editor.evaluate('filmstripCalls')
    editor.wait_for_timeout(1200)
    assert editor.evaluate('filmstripCalls')==calls
