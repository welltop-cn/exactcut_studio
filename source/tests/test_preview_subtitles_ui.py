"""Native browser track and asynchronous preview-card lifecycle contract."""
import re
from pathlib import Path
import pytest


@pytest.fixture
def page():
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1]/'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root/'index.html').read_text())
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':1000})
        page.set_default_timeout(3500)
        page.route('**/*',lambda r:r.fulfill(body=html,content_type='text/html'))
        page.goto('http://fixture.test/')
        page.evaluate('''() => {
          window.$=id=>document.getElementById(id);window.esc=String;window.appUrl=p=>p;
          window.currentUser={features:{preview_subtitles:true}};window.activeTab='review';window.selectedId='a';
          window.current={id:'a',assets:[{preview:'/video',request_id:'R1',sha256:'a'.repeat(64),version_id:null,title:'素材一'}]};
          window.calls=[];window.toast=()=>{};window.delayPost=false;window.postReply=null;
          window.binding={batch_id:'a',request_id:'R1',source_sha256:'a'.repeat(64),version_id:null};
          window.report={id:'task',status:'idle',cues:[],progress:'未生成'};
          window.api=async (url,body)=>{
            calls.push({url,body});
            if(body!==undefined){
              report={id:'task',status:'running',cues:[],progress:'正在识别'};
              if(delayPost) await new Promise(r=>postReply=r);
            }
            return {enabled:true,binding:{...binding},report:{...report}};
          };
          $('workspace').hidden=false;$('reviewTab').hidden=false;
          $('assets').innerHTML='<article class="asset" data-asset="0"><div class="videoarea"><video controls></video></div><div class="assetbody"></div></article>';
          window.finish=()=>{report={id:'task',status:'completed',model:'fixture',elapsed_seconds:1,cues:[
            {start_ms:100,end_ms:900,original_text:'Hello',translation_zh:'你好'},
            {start_ms:2000,end_ms:3000,original_text:'Again',translation_zh:'再见'}]}};
        }''')
        page.add_script_tag(content=(root/'editor-busy.js').read_text())
        page.add_script_tag(content=(root/'preview-subtitles.js').read_text())
        page.evaluate('PreviewSubtitles.mount()')
        yield page
        browser.close()


def test_one_track_completion_waits_for_manual_dialog_and_preserves_native_fullscreen(page):
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function("calls.some(c=>c.body!==undefined)")
    page.locator('#assets video').focus()
    page.evaluate('finish()')
    page.wait_for_function("document.querySelector('[data-subtitle-timeline]').disabled===false")
    assert not page.locator('#subtitleTimeline').is_visible()
    assert page.evaluate("document.activeElement===document.querySelector('#assets video')")
    page.locator('[data-subtitle-timeline]').click()
    page.locator('#subtitleTimeline').wait_for(state='visible')
    assert page.evaluate("document.querySelector('video').textTracks.length")==1
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='showing'
    assert page.evaluate("document.querySelector('video').textTracks[0].cues[0].line")==2
    page.locator('#subtitleTimeline [data-subtitle-cue="1"]').click()
    assert not page.locator('#subtitleTimeline').is_visible()
    assert page.evaluate("document.querySelector('video').currentTime")==2
    page.locator('[data-subtitle-toggle]').uncheck()
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='disabled'
    page.locator('[data-subtitle-toggle]').check()
    for _ in range(4):page.evaluate('PreviewSubtitles.mount()')
    assert page.evaluate("calls.filter(c=>c.body!==undefined).length")==1
    assert page.evaluate("document.querySelector('video').textTracks.length")==1
    assert page.evaluate("document.querySelector('video').currentTime")==2
    assert not page.locator('#subtitleTimeline').is_visible()
    # Fullscreen relies on the very same browser-owned showing track, no HTML overlay.
    page.locator('#assets video').evaluate('(v)=>v.requestFullscreen()')
    assert page.evaluate('document.fullscreenElement.tagName')=='VIDEO'
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='showing'
    page.evaluate('document.exitFullscreen()')


def test_loading_can_be_disabled_and_completion_never_steals_focus(page):
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function("calls.some(c=>c.body!==undefined)")
    page.locator('[data-subtitle-toggle]').uncheck()
    page.evaluate('finish()')
    page.wait_for_function("document.querySelector('[data-subtitle-timeline]').disabled===false")
    assert not page.locator('#subtitleTimeline').is_visible()
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='disabled'
    page.locator('[data-subtitle-timeline]').click()
    page.locator('#subtitleAutoClose').uncheck()
    page.locator('[data-subtitle-cue="0"]').click()
    assert page.locator('#subtitleTimeline').is_visible()
    page.evaluate("document.querySelector('video').currentTime=1.5;document.querySelector('video').dispatchEvent(new Event('timeupdate'))")
    assert page.locator('#subtitleTimeline .active').count()==0
    page.locator('#subtitleClose').click()
    assert not page.locator('[data-subtitle-toggle]').is_checked()


def test_dispose_rejects_late_project_response_and_reuses_native_track(page):
    page.evaluate('delayPost=true')
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function('postReply!==null')
    page.evaluate("PreviewSubtitles.dispose();selectedId='b';current={id:'b',assets:[]};finish();postReply()")
    page.wait_for_timeout(80)
    assert not page.locator('#subtitleTimeline').is_visible()
    assert page.evaluate("document.querySelector('video').textTracks.length")==0


def test_new_sha_invalidates_old_track_and_preview_reload_preserves_one_track(page):
    page.locator('[data-subtitle-toggle]').check();page.wait_for_function("calls.some(c=>c.body!==undefined)")
    page.evaluate('finish()');page.wait_for_function("document.querySelector('[data-subtitle-timeline]').disabled===false")
    assert not page.locator('#subtitleTimeline').is_visible()
    page.evaluate("const v=document.querySelector('video');v.dispatchEvent(new Event('emptied'));v.dispatchEvent(new Event('loadedmetadata'));PreviewSubtitles.mount()")
    assert page.evaluate("document.querySelector('video').textTracks.length")==1
    assert page.evaluate("document.querySelector('video').textTracks[0].cues.length")==2
    page.evaluate("current.assets[0].sha256='b'.repeat(64);PreviewSubtitles.mount()")
    assert not page.locator('[data-subtitle-toggle]').is_checked()
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='disabled'
    assert page.evaluate("(()=>{const t=document.querySelector('video').textTracks[0];t.mode='hidden';const n=t.cues.length;t.mode='disabled';return n})()")==0


def test_queued_completion_enables_manual_timeline_and_the_same_native_track(page):
    page.evaluate('''() => {
      window.phase=0;
      window.api=async (url,body)=>{
        calls.push({url,body});
        const done=()=>({enabled:true,binding:{...binding},report:{id:'task',status:'completed',model:'fixture',elapsed_seconds:1,cues:[
          {start_ms:100,end_ms:900,original_text:'Hello',translation_zh:'你好'}]}});
        if(body!==undefined){phase=1;return {enabled:true,binding:{...binding},report:{id:'task',status:'queued',cues:[],progress:'排队中',queue_position:2}};}
        if(phase===0)return {enabled:true,binding:{...binding},report:{id:'task',status:'idle',cues:[],progress:'未生成'}};
        if(phase===1){phase=2;return {enabled:true,binding:{...binding},report:{id:'task',status:'queued',cues:[],progress:'排队中',queue_position:1}};}
        return done();
      };
    }''')
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function("document.querySelector('.subtitle-status').textContent.includes('排队中')")
    assert '前面还有 1 条' in page.locator('.subtitle-status').inner_text()
    assert page.locator('[data-subtitle-timeline]').is_disabled()
    # Nothing to display while queued, so no browser track exists yet.
    assert page.evaluate("document.querySelector('video').textTracks.length")==0
    page.wait_for_function("document.querySelector('[data-subtitle-timeline]').disabled===false")
    assert not page.locator('#subtitleTimeline').is_visible()
    assert page.locator('[data-subtitle-timeline]').is_enabled()
    assert page.evaluate("document.querySelector('video').textTracks[0].cues.length")==1
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='showing'
    page.locator('[data-subtitle-timeline]').click()
    assert page.locator('#subtitleTimeline').is_visible()


def test_queued_task_keeps_running_when_switched_off_without_stealing_focus(page):
    page.evaluate('''() => {
      window.phase=0;
      window.api=async (url,body)=>{
        calls.push({url,body});
        if(body!==undefined){phase=1;return {enabled:true,binding:{...binding},report:{id:'task',status:'queued',cues:[],progress:'排队中',queue_position:1}};}
        if(phase===0)return {enabled:true,binding:{...binding},report:{id:'task',status:'idle',cues:[],progress:'未生成'}};
        const rep=phase<2
          ?{id:'task',status:'queued',cues:[],progress:'排队中',queue_position:1}
          :{id:'task',status:'completed',model:'fixture',elapsed_seconds:1,cues:[
             {start_ms:100,end_ms:900,original_text:'Hello',translation_zh:'你好'}]};
        return {enabled:true,binding:{...binding},report:rep};
      };
    }''')
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function("document.querySelector('.subtitle-status').textContent.includes('排队中')")
    page.evaluate('phase=2')
    page.locator('[data-subtitle-toggle]').uncheck()
    page.wait_for_function("document.querySelector('[data-subtitle-timeline]').disabled===false")
    assert not page.locator('#subtitleTimeline').is_visible()
    assert page.evaluate("document.querySelector('video').textTracks[0].mode")=='disabled'
    assert page.evaluate("calls.filter(c=>c.body!==undefined).length")==1
    # Reopening reuses the finished task's cached result; no second paid submission.
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function("document.querySelector('video').textTracks[0].mode==='showing'")
    assert page.evaluate("calls.filter(c=>c.body!==undefined).length")==1
