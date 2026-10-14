"""Browser contract for frame selections and deletion-only editing."""
import re
from pathlib import Path

import pytest


@pytest.fixture
def editor():
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root/'index.html').read_text())
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.set_default_timeout(2500)
        page.on('dialog', lambda dialog: dialog.accept())
        page.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
        page.goto('http://fixture.test/')
        page.evaluate('''() => {
          window.$ = id => document.getElementById(id);
          window.esc = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          window.appUrl = url => url;
          window.currentUser = {username:'fixture', features:{cutting:true}};
          window.current = {id:'batch', assets:[{title:'测试',can_edit_video:true,request_id:'R1',sha256:'a'.repeat(64),preview:'/video'}]};
          window.calls=[];
          window.session={id:'session',source_sha256:'a'.repeat(64),source_filename:'source.mp4',metadata:{frame_count:300,fps_num:30,fps_den:1,width:160,height:96},jobs:[]};
          window.api=async (url, body) => {
            calls.push({url,body});
            if(url.endsWith('/scan/standard')) return window.scanState||{enabled:true,report:{status:'completed',risk_events:[]}};
            if(url.endsWith('/render')) {const job={id:'job',status:'ready',...body,expected_frames:290};session.jobs=[job];return job;}
            return session;
          };
          $('assets').innerHTML='<article data-asset="0"><label class="approve">确认</label></article>';
          $('assets').closest('[hidden]')?.removeAttribute('hidden');
        }''')
        page.add_script_tag(content=(root/'cutting.js').read_text())
        page.locator('[data-open-cut]').evaluate('(el)=>el.click()')
        page.locator('#cutEditor').wait_for(state='visible')
        yield page
        browser.close()


def clock(one_based):
    second,frame=divmod(one_based-1,30)
    minute,second=divmod(second,60)
    return f'{minute:02}:{second:02}:{frame:02}'


def select(page, start, end):
    page.locator('#cutManual').click()
    page.locator('#cutStart').fill(clock(start))
    page.locator('#cutStart').dispatch_event('change')
    page.locator('#cutEnd').fill(clock(end))
    page.locator('#cutEnd').dispatch_event('change')


def test_mosaic_only_toast(editor):
    editor.locator('#cutManual').click()
    editor.get_by_role('button', name='添加打码片段', exact=True).click()
    assert '暂未开放' in editor.locator('#cutToast').inner_text()
    assert editor.locator('#cutRanges li').count() == 0
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/render'))")


def test_independent_endpoints_and_inclusive_render(editor):
    select(editor, 11, 20)
    editor.get_by_role('button', name='开始帧下一帧', exact=True).click()
    assert editor.locator('#cutStart').input_value() == '00:00:11'
    assert editor.locator('#cutEnd').input_value() == '00:00:19'
    editor.get_by_role('button', name='开始帧上一帧', exact=True).click()
    editor.get_by_role('button', name='添加片段', exact=True).click()
    editor.get_by_role('button', name='生成编辑后视频', exact=True).click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/render'))")
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/render')).body") == {'ranges': [[10, 20]], 'mosaics': [], 'reset_at': ''}


def test_merge_remove_undo_and_all_frames_rejected(editor):
    select(editor, 11, 20);editor.locator('#cutAdd').click()
    select(editor, 20, 25);editor.locator('#cutAdd').click()
    assert editor.locator('#cutRanges li').count()==2
    editor.locator('#cutRender').click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/render'))")
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/render')).body.ranges")==[[10,25]]
    editor.locator('#cutContinue').click()
    editor.locator('#cutUndo').click()
    assert editor.locator('#cutRanges li').count()==1
    select(editor, 1, 300);editor.locator('#cutAdd').click()
    assert '至少保留一帧' in editor.locator('#cutStatus').inner_text()
    assert editor.locator('#cutRanges li').count()==1


def test_list_height_is_bounded(editor):
    for start in range(1, 81, 10):
        select(editor, start, start+2)
        editor.locator('#cutAdd').click()
    assert editor.locator('#cutRanges li').count() == 8
    size = editor.locator('#cutRanges').evaluate('(el)=>[el.clientHeight,el.scrollHeight,getComputedStyle(el).overflowY]')
    assert size[0] <= editor.locator('.cut-side').bounding_box()['height']
    assert size[1] >= size[0]
    assert size[2] == 'auto'


def test_delivered_card_loses_edit_entry(editor):
    editor.locator('#cutClose').click()
    editor.evaluate('current.assets[0].can_edit_video=false;Cutting.mount()')
    assert editor.locator('[data-open-cut]').count()==0


def test_replace_requires_confirmation_then_refreshes_card(editor):
    select(editor,11,20);editor.locator('#cutAdd').click();editor.locator('#cutRender').click()
    editor.locator('#cutReplace').wait_for(state='visible')
    editor.evaluate('''() => {
      const original=api;
      window.renderRuntime=()=>{window.refreshed=true}; window.toast=()=>{};
      api=async (url,body)=>url.endsWith('/replace')?{...session.jobs[0],replacement:{status:'completed',storage:'local'}}:url==='/api/batches/batch'?{...current,assets:[{...current.assets[0],sha256:'b'.repeat(64)}]}:original(url,body);
    }''')
    editor.locator('#cutReplace').click()
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/replace'))")
    editor.locator('#cutReplaceCancel').click()
    assert editor.locator('#cutDialog').evaluate('(el)=>el.open')
    editor.locator('#cutReplace').click();editor.locator('#cutReplaceNow').click()
    editor.wait_for_function('window.refreshed===true')
    assert not editor.locator('#cutDialog').evaluate('(el)=>el.open')
    assert editor.evaluate('current.assets[0].sha256')=='b'*64


def test_timecode_jump_and_second_boundary(editor):
    assert editor.locator('#cutPosition').inner_text()=='00:00:00 / 00:10:00'
    select(editor,30,32)
    editor.get_by_role('button',name='开始帧下一帧',exact=True).click()
    assert editor.locator('#cutStart').input_value()=='00:01:00'
    editor.locator('#cutFrameNumber').fill('00:02:05')
    editor.locator('#cutFrameNumber').dispatch_event('change')
    assert editor.locator('#cutSeek').input_value()=='65'
    assert editor.locator('#cutPosition').inner_text()=='00:02:05 / 00:10:00'
    editor.locator('#cutFrameNumber').fill('00:02:30')
    editor.locator('#cutFrameNumber').dispatch_event('change')
    assert '帧' in editor.locator('#cutStatus').inner_text()
    assert editor.locator('#cutSeek').input_value()=='65'


@pytest.mark.parametrize('num,den,before,after,expected',[
    (30,1,'00:59:29','01:00:00',1800),
    (24,1,'00:00:23','00:01:00',24),
    (60,1,'00:00:59','00:01:00',60),
    (30000,1001,'00:00:29','00:01:00',30),
])
def test_timecode_uses_video_rate(editor,num,den,before,after,expected):
    editor.evaluate('([num,den])=>Object.assign(session.metadata,{fps_num:num,fps_den:den,frame_count:6000})',[num,den])
    editor.locator('#cutSeek').evaluate('(el)=>el.max=5999')
    editor.locator('#cutFrameNumber').fill(before)
    editor.locator('#cutFrameNumber').dispatch_event('change')
    editor.locator('#cutNext').click()
    assert editor.locator('#cutFrameNumber').input_value()==after
    assert editor.locator('#cutSeek').input_value()==str(expected)
    editor.locator('#cutManual').click()
    editor.locator('#cutMarkStart').click()
    assert editor.locator('#cutStart').input_value()==after


def test_all_ai_suggestions_are_added_as_one_undoable_change(editor):
    assert editor.locator('#cutRiskAddAll').is_disabled()
    select(editor,11,20);editor.locator('#cutAdd').click()
    editor.evaluate("window.scanState={enabled:true,report:{status:'completed',risk_events:[{start_frame:15,end_frame:30,category:'建议一'},{start_frame:60,end_frame:90,category:'建议二'}]}}")
    editor.locator('#cutScan').click()
    assert editor.locator('#cutRanges li').count()==3
    assert editor.locator('[data-clip-check]:checked').count()==1
    editor.locator('#cutRiskAddAll').click()
    assert editor.locator('[data-clip-check]:checked').count()==3
    assert editor.locator('#cutRiskMarkers button').count()==0
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/render'))")
    editor.locator('#cutUndo').click()
    assert editor.locator('[data-clip-check]:checked').count()==1
    assert editor.locator('#cutRiskMarkers button').count()==2


def test_all_ai_cannot_delete_the_entire_video(editor):
    editor.evaluate("window.scanState={enabled:true,report:{status:'completed',risk_events:[{start_frame:0,end_frame:300,category:'建议'}]}}")
    editor.locator('#cutScan').click();editor.locator('#cutRiskAddAll').click()
    assert '至少保留一帧' in editor.locator('#cutStatus').inner_text()
    assert editor.locator('[data-clip-check]:checked').count()==0


def test_ai_check_and_inline_adjust_need_no_add_step(editor):
    editor.evaluate("window.scanState={enabled:true,report:{status:'completed',risk_events:[{start_frame:30,end_frame:60,category:'建议'}]}}")
    editor.locator('#cutScan').click()
    assert editor.locator('#cutRangeEditor').is_hidden()
    assert editor.locator('#cutRender').is_disabled()
    editor.locator('[data-clip-check]').check()
    editor.locator('[data-clip-edit]').first.click()
    assert editor.locator('#cutStart').input_value()=='00:01:00'
    editor.get_by_role('button',name='开始帧下一帧',exact=True).click()
    assert editor.locator('#cutAdd').is_hidden()
    assert not editor.locator('#cutRender').is_disabled()
    editor.locator('#cutRender').click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/render'))")
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/render')).body.ranges")==[[31,60]]


def test_unchecking_preserves_overlapping_selected_clip(editor):
    select(editor,11,30);editor.locator('#cutAdd').click()
    select(editor,21,40);editor.locator('#cutAdd').click()
    editor.locator('[data-clip-check]').first.uncheck()
    editor.locator('#cutRender').click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/render'))")
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/render')).body.ranges")==[[20,40]]


def test_old_recipe_wins_over_stale_unified_draft(editor):
    editor.evaluate('''()=>{
      const key=Object.keys(localStorage).find(k=>k.startsWith('exactcut.cuts.delete.v1.')&&!k.endsWith('.clips'));
      localStorage.setItem(key,JSON.stringify([[5,10]]));
      localStorage.setItem(key+'.clips',JSON.stringify([]));
    }''')
    editor.locator('#cutClose').click()
    editor.wait_for_function("!document.getElementById('cutSource').getAttribute('src')")
    editor.locator('[data-open-cut]').evaluate('(el)=>el.click()')
    editor.locator('#cutEditor').wait_for(state='visible')
    assert editor.locator('[data-clip-check]:checked').count()==1
    assert '00:00:05–00:00:09' in editor.locator('#cutRanges').inner_text()
    editor.locator('#cutRender').click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/render'))")
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/render')).body.ranges")==[[5,10]]


def test_generated_result_hides_source_editor_and_can_return(editor):
    select(editor,11,20);editor.locator('#cutAdd').click();editor.locator('#cutRender').click()
    editor.locator('#cutOutput').wait_for(state='visible')
    assert editor.locator('.cut-side').is_hidden()
    assert editor.locator('.cut-timeline').is_hidden()
    assert editor.locator('.cut-transport').is_hidden()
    assert editor.locator('#cutRender').is_hidden()
    assert '已删除' in editor.locator('#cutSummary').inner_text()
    assert '9.67 秒' in editor.locator('#cutSourceInfo').inner_text()
    editor.locator('#cutContinue').click()
    assert editor.locator('.cut-side').is_visible()
    assert editor.locator('[data-clip-check]:checked').count()==1
    assert '10.00 秒' in editor.locator('#cutSourceInfo').inner_text()


def test_discard_then_reopen_starts_on_original_without_old_selections(editor):
    select(editor,11,20);editor.locator('#cutAdd').click();editor.locator('#cutRender').click()
    editor.locator('#cutDiscard').wait_for(state='visible')
    editor.evaluate('''()=>{
      const original=api;window.toast=()=>{};
      api=async (url,body)=>{
        if(url.endsWith('/discard')){
          session={...session,reset_at:'new-edit-session',jobs:session.jobs.map(j=>({...j,discarded:true}))};
          window.scanState={enabled:false,report:null};return session;
        }
        return original(url,body);
      };
    }''')
    editor.locator('#cutDiscard').click()
    editor.wait_for_function("!document.getElementById('cutSource').getAttribute('src')")
    editor.locator('[data-open-cut]').evaluate('(el)=>el.click()')
    editor.locator('#cutEditor').wait_for(state='visible')
    assert editor.locator('#cutOutput').is_hidden()
    assert editor.locator('#cutSource').is_visible()
    assert editor.locator('#cutRanges li').count()==0
    assert editor.locator('#cutRender').is_disabled()


def test_normal_close_keeps_generated_result_for_later(editor):
    select(editor,11,20);editor.locator('#cutAdd').click();editor.locator('#cutRender').click()
    editor.locator('#cutOutput').wait_for(state='visible')
    editor.locator('#cutClose').click()
    editor.wait_for_function("!document.getElementById('cutSource').getAttribute('src')")
    editor.locator('[data-open-cut]').evaluate('(el)=>el.click()')
    editor.locator('#cutOutput').wait_for(state='visible')
    assert editor.locator('.cut-side').is_hidden()
    assert editor.locator('#cutReplace').is_visible()


def test_rescan_resets_ai_edits_but_preserves_manual(editor):
    select(editor,11,20);editor.locator('#cutAdd').click()
    editor.evaluate("window.scanState={enabled:true,report:{status:'completed',risk_events:[{start_frame:30,end_frame:60,category:'建议'}]}}")
    editor.locator('#cutScan').click()
    editor.locator('[data-clip-check]').nth(1).check()
    editor.locator('#cutRanges [data-clip-edit]').nth(1).click()
    editor.get_by_role('button',name='开始帧下一帧',exact=True).click()
    editor.locator('#cutScan').click()
    assert editor.locator('#cutRanges li').count()==2
    assert editor.locator('[data-clip-check]:checked').count()==1
    assert editor.locator('#cutRangeEditor').is_hidden()
    assert editor.locator('#cutUndo').is_disabled()
    editor.locator('#cutRanges [data-clip-edit]').nth(1).click()
    assert editor.locator('#cutStart').input_value()=='00:01:00'
    assert editor.evaluate("calls.filter(c=>c.url.endsWith('/scan')).every(c=>c.body.force===true)")


def test_cancel_rescan_keeps_current_plan(editor):
    select(editor,11,20);editor.locator('#cutAdd').click()
    editor.evaluate('window.confirm=()=>false')
    editor.locator('#cutScan').click()
    assert editor.locator('[data-clip-check]:checked').count()==1
    assert not editor.evaluate("calls.some(c=>c.url.endsWith('/scan'))")


def test_new_window_sends_current_reset_marker(editor):
    editor.locator('#cutClose').click()
    editor.wait_for_function("!document.querySelector('#cutSource').hasAttribute('src')")
    editor.evaluate("session.reset_at='new-session';calls=[]")
    editor.locator('[data-open-cut]').evaluate('(el)=>el.click()')
    editor.locator('#cutManual').wait_for()
    select(editor,11,20);editor.locator('#cutAdd').click();editor.locator('#cutRender').click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/render'))")
    assert editor.evaluate("calls.find(c=>c.url.endsWith('/render')).body.reset_at")=='new-session'
