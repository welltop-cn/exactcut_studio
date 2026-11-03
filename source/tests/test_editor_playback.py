"""Controlled media promises exercise actual editor handlers and status ownership."""
import pytest
from test_cutting_ui import editor as cut_editor, select as select_cut
from test_mosaic_ui import editor as mosaic_editor, open_editor, confirm_region

pytestmark = pytest.mark.ui


@pytest.fixture(params=['cut', 'mosaic'])
def playback(request):
    kind = request.param
    page = request.getfixturevalue(kind + '_editor')
    if kind == 'mosaic':
        open_editor(page)
    page.evaluate('''kind => {
      window.attempts=[]; window.failures=[];
      window.reportMediaPlayFailure=(video,error,context)=>failures.push({name:error.name,batch:context.batch});
      const video=document.getElementById(kind+'Source');
      let paused=true;
      Object.defineProperty(video,'paused',{get:()=>paused,configurable:true});
      video.play=()=>{paused=false;video.dispatchEvent(new Event('play'));return new Promise((resolve,reject)=>attempts.push({resolve,reject}));};
      video.pause=()=>{paused=true;};
      window.rejectPlay=(index,name)=>{paused=true;attempts[index].reject(new DOMException('fixture',name));};
      window.finishPlay=index=>{paused=false;attempts[index].resolve();video.dispatchEvent(new Event('playing'));};
    }''', kind)
    return page, kind


def click_play(page, kind):
    page.locator('#'+kind+'Play').evaluate('(el)=>el.click()')


@pytest.mark.parametrize('cancel', ['seek', 'pause', 'close', 'reopen'])
def test_cancelled_pending_play_does_not_show_failure(playback, cancel):
    page, kind = playback
    click_play(page, kind)
    if cancel == 'seek':
        page.locator('#'+kind+'Seek').evaluate("el=>{el.value=120;el.dispatchEvent(new Event('input'));}")
    elif cancel == 'pause':
        click_play(page, kind)
    else:
        page.locator('#'+kind+'Close').click()
        if cancel == 'reopen':
            page.locator('[data-open-'+('mosaic' if kind == 'mosaic' else 'cut')+']').evaluate('(el)=>el.click()')
            page.locator('#'+kind+('Workspace' if kind=='mosaic' else 'Editor')).wait_for(state='visible')
    page.evaluate("rejectPlay(0,'AbortError')")
    assert '暂时无法播放' not in page.locator('#'+kind+'Status').inner_text()
    assert page.evaluate('failures[0].name') == 'AbortError'


@pytest.mark.parametrize('name', ['AbortError', 'NotAllowedError', 'NotSupportedError'])
def test_active_failure_is_visible_and_cleared_on_playing(playback, name):
    page, kind = playback
    before = page.locator('#'+kind+'Status').inner_text()
    click_play(page, kind)
    page.evaluate('name=>rejectPlay(0,name)', name)
    assert ('点击播放以继续' if name=='NotAllowedError' else '暂时无法播放') in page.locator('#'+kind+'Status').inner_text()
    click_play(page, kind)
    page.evaluate('finishPlay(1)')
    assert page.locator('#'+kind+'Status').inner_text() == before
    assert 'error' not in (page.locator('#'+kind+'Status').get_attribute('class') or '')


def test_playing_does_not_erase_newer_validation_error(playback):
    page, kind = playback
    click_play(page, kind)
    page.evaluate("rejectPlay(0,'NotSupportedError')")
    page.locator('#'+kind+'FrameNumber').evaluate("el=>{el.value='bad-time';el.dispatchEvent(new Event('change'));}")
    message = page.locator('#'+kind+'Status').inner_text()
    assert '分:秒:帧' in message
    click_play(page, kind)
    page.evaluate('finishPlay(1)')
    assert page.locator('#'+kind+'Status').inner_text() == message.replace('暂时无法播放，请重试', '').strip()


def test_old_rejection_does_not_overwrite_new_playback(playback):
    page, kind = playback
    click_play(page, kind)
    click_play(page, kind)
    click_play(page, kind)
    page.evaluate('finishPlay(1)')
    page.evaluate("attempts[0].reject(new DOMException('fixture','AbortError'))")
    assert '暂时无法播放' not in page.locator('#'+kind+'Status').inner_text()


def test_pending_failure_and_newer_validation_error_both_remain_visible(playback):
    page, kind = playback
    click_play(page, kind)
    page.locator('#'+kind+'FrameNumber').evaluate("el=>{el.value='bad-time';el.dispatchEvent(new Event('change'));}")
    message = page.locator('#'+kind+'Status').inner_text()
    page.evaluate("rejectPlay(0,'NotSupportedError')")
    assert message in page.locator('#'+kind+'Status').inner_text()
    assert '暂时无法播放' in page.locator('#'+kind+'Status').inner_text()
    click_play(page, kind)
    page.evaluate('finishPlay(1)')
    assert page.locator('#'+kind+'Status').inner_text() == message
    assert page.evaluate('failures[0].name') == 'NotSupportedError'


@pytest.mark.parametrize('action', ['pause', 'reopen'])
def test_explicit_pause_resets_button_without_waiting_for_media_event(playback, action):
    page, kind = playback
    click_play(page, kind)
    page.evaluate('finishPlay(0)')
    assert page.locator('#'+kind+'Play').inner_text() == '暂停'
    if action == 'pause':
        click_play(page, kind)
    else:
        page.locator('#'+kind+'Close').click()
        page.locator('[data-open-'+('mosaic' if kind=='mosaic' else 'cut')+']').evaluate('(el)=>el.click()')
        page.locator('#'+kind+('Workspace' if kind=='mosaic' else 'Editor')).wait_for(state='visible')
    assert page.locator('#'+kind+'Source').evaluate('v=>v.paused')
    assert page.locator('#'+kind+'Play').inner_text() == '播放'


def test_delayed_pause_event_does_not_reset_active_play_button(playback):
    page, kind = playback
    click_play(page, kind)
    page.locator('#'+kind+'Close').click()
    page.locator('[data-open-'+('mosaic' if kind=='mosaic' else 'cut')+']').evaluate('(el)=>el.click()')
    page.locator('#'+kind+('Workspace' if kind=='mosaic' else 'Editor')).wait_for(state='visible')
    click_play(page, kind)
    page.evaluate('finishPlay(1)')
    page.locator('#'+kind+'Source').dispatch_event('pause')
    assert page.locator('#'+kind+'Play').inner_text() == '暂停'


def test_mosaic_generation_start_and_result_arrival_cancel_pending_play(mosaic_editor):
    page = mosaic_editor
    open_editor(page)
    page.locator('#mosaicRecognize').click()
    page.locator('[data-candidate="0"]').wait_for()
    confirm_region(page)
    page.evaluate("""() => {
      session.capabilities.tracking=true;
      const old=api;
      window.api=async(url,body)=>{
        if(url.endsWith('/render')&&body){window.renderJob={id:'b'.repeat(32),status:'running',...body};return renderJob;}
        if(url.includes('/render/'))return new Promise(resolve=>window.finishRender=resolve);
        return old(url,body);
      };
      const video=$('mosaicSource');let paused=true;window.pending=[];
      Object.defineProperty(video,'paused',{get:()=>paused});
      video.pause=()=>{paused=true;};
      video.play=()=>{paused=false;return new Promise((resolve,reject)=>pending.push({resolve,reject}));};
    }""")
    page.locator('#mosaicFrameNumber').evaluate("el=>el.dispatchEvent(new Event('change'))")
    click_play(page, 'mosaic')
    page.locator('#mosaicBatchStart').click()
    assert page.locator('#mosaicSource').evaluate('v=>v.paused')
    page.evaluate("pending[0].reject(new DOMException('fixture','AbortError'))")
    page.wait_for_function('!!window.finishRender')
    click_play(page, 'mosaic')
    page.evaluate("finishRender({...renderJob,status:'completed',result:{},completed_at:'now'})")
    page.locator('#mosaicBatchDialog').wait_for(state='visible')
    assert page.locator('#mosaicSource').evaluate('v=>v.paused')
    page.evaluate("pending[1].reject(new DOMException('fixture','AbortError'))")
    page.locator('#mosaicBatchClose').click()
    assert '暂时无法播放' not in page.locator('#mosaicStatus').inner_text()


def test_cut_render_cancels_pending_play(cut_editor):
    page=cut_editor
    select_cut(page,11,20)
    page.locator('#cutAdd').click()
    page.evaluate("""()=>{
      const video=$('cutSource');let paused=true;
      Object.defineProperty(video,'paused',{get:()=>paused});
      video.pause=()=>{paused=true;};
      video.play=()=>{paused=false;return new Promise((resolve,reject)=>window.failPending=reject);};
    }""")
    click_play(page,'cut')
    page.locator('#cutRender').click()
    page.locator('#cutOutput').wait_for(state='visible')
    assert page.locator('#cutSource').evaluate('v=>v.paused')
    page.evaluate("failPending(new DOMException('fixture','AbortError'))")
    assert '暂时无法播放' not in page.locator('#cutStatus').inner_text()
