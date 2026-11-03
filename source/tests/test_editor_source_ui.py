"""Deterministic media events, real browser promise and lifecycle scheduling."""
from pathlib import Path
import pytest
from browser_support import shared_playwright

pytestmark = pytest.mark.ui


@pytest.fixture
def player():
    pw=pytest.importorskip('playwright.sync_api')
    with shared_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page()
        page.goto('about:blank')
        page.set_content('<div id="v" data-media></div><div id="status"></div><div id="other" data-media></div>')
        script=Path(__file__).parents[1]/'exactcut/web/editor-source.js'
        if script.exists():page.add_script_tag(content=script.read_text())
        page.evaluate('''() => {
          window.appUrl=u=>u;window.requests=0;window.plays=0;window.errors=[];
          window.fetch=async()=>{requests++;return {ok:true,json:async()=>({source:'oss',url:'https://oss.test/v'+requests,expires_in:900,reason:'verified'})};};
          for(const v of document.querySelectorAll('[data-media]')){
            let paused=true,time=0;
            Object.defineProperties(v,{src:{get:()=>v.getAttribute('src'),set:x=>v.setAttribute('src',x)},paused:{get:()=>paused},currentTime:{get:()=>time,set:x=>time=x},duration:{get:()=>60},readyState:{get:()=>1}});
            v.load=()=>{};v.pause=()=>{paused=true};v.play=()=>{paused=false;plays++;return Promise.resolve()};
          }
          window.source=EditorSource.create({endpoint:'/source-url',local:'/source',enabled:true,timeout:30});
          window.binding=source.attach(v,{valid:()=>window.active!==false,beforeChange:()=>{},play:()=>v.play(),failed:e=>errors.push(e.message),anchor:document.getElementById("status")});
        }''')
        yield page
        browser.close()


def test_initial_shared_source_and_bounded_recovery(player):
    p=player
    p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.locator('#v').get_attribute('src')=='https://oss.test/v1'
    p.evaluate('window.second=source.attach(other,{valid:()=>true,play:()=>other.play()});second.open()')
    assert p.locator('#other').get_attribute('src')==p.locator('#v').get_attribute('src')
    assert p.evaluate('requests')==1
    p.evaluate("v.currentTime=12;v.volume=.3;v.playbackRate=1.5;binding.play();v.dispatchEvent(new Event('error'))")
    p.wait_for_function("v.getAttribute('src')==='https://oss.test/v2'")
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.evaluate('v.currentTime')==12
    assert p.evaluate('other.paused') is True
    p.evaluate("v.dispatchEvent(new Event('error'))")
    p.wait_for_function("v.getAttribute('src')==='/source'")
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));v.dispatchEvent(new Event('error'))")
    assert p.evaluate('requests')==2
    assert p.evaluate('errors.length')==1
    assert p.evaluate('v.volume')==.3
    assert p.evaluate('v.playbackRate')==1.5


@pytest.mark.parametrize('cancel',['pause','seek','dispose'])
def test_late_recovery_cannot_restore_old_intent(player,cancel):
    p=player;p.evaluate('binding.open()')
    p.evaluate("window.release=null;fetch=()=>new Promise(r=>release=r);binding.play();v.dispatchEvent(new Event('error'))")
    p.evaluate({'pause':'binding.pause()','seek':'binding.seek(23)','dispose':'binding.dispose();v.src="/after"'}[cancel])
    before=p.evaluate('plays')
    p.evaluate("release({ok:true,json:async()=>({source:'oss',url:'https://oss.test/late',expires_in:900})})")
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.evaluate('plays')==before
    if cancel=='dispose':assert p.locator('#v').get_attribute('src')=='/after'
    if cancel=='seek':assert p.evaluate('v.currentTime')==23


def test_timeout_latches_local_and_late_result_ignored(player):
    p=player
    p.evaluate('() => {fetch=()=>new Promise(r=>window.release=r);}')
    p.evaluate('Promise.race([binding.open(),new Promise(r=>setTimeout(r,100))])')
    assert p.locator('#v').get_attribute('src')=='/source'
    p.evaluate("release({ok:true,json:async()=>({source:'oss',url:'https://oss.test/late',expires_in:900})})")
    assert p.locator('#v').get_attribute('src')=='/source'


@pytest.mark.parametrize('code',[401,403,404,409])
def test_access_failure_never_falls_back(player,code):
    p=player;p.evaluate('code=>{fetch=async()=>({ok:false,status:code});}',code)
    p.evaluate('binding.open()')
    assert p.locator('#v').get_attribute('src') is None
    assert p.evaluate('errors.length')==1


def test_manual_fallback_after_foreground_stall_is_one_way(player):
    p=player;p.clock.install();p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));binding.play()")
    p.clock.run_for(11000)
    p.get_by_role('button',name='切换服务器').click()
    assert p.locator('#v').get_attribute('src')=='/source'
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));v.dispatchEvent(new Event('error'))")
    assert p.evaluate('requests')==1
    assert p.get_by_role('button',name='重试播放').is_visible()


def test_expiry_refreshes_only_on_next_action(player):
    p=player;p.clock.install();p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));binding.play()")
    p.clock.run_for(850000)
    assert p.evaluate('requests')==1
    p.evaluate('binding.pause();binding.seek(17)')
    assert p.evaluate('requests')==2
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.evaluate('v.currentTime')==17


def test_same_signed_url_still_reloads_on_media_error(player):
    p=player
    p.evaluate("() => {window.loads=0;v.load=()=>loads++;fetch=async()=>({ok:true,json:async()=>({source:'oss',url:'https://oss.test/fixed',expires_in:900})});}")
    p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));v.dispatchEvent(new Event('error'))")
    p.wait_for_function('loads===2')


def test_response_body_timeout_also_falls_back(player):
    p=player
    p.evaluate('() => {fetch=async()=>({ok:true,json:()=>new Promise(()=>{})});}')
    p.evaluate('Promise.race([binding.open(),new Promise(r=>setTimeout(r,100))])')
    assert p.locator('#v').get_attribute('src')=='/source'


def test_native_seek_during_recovery_keeps_new_position(player):
    p=player;p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));binding.play();fetch=()=>new Promise(r=>window.release=r);v.dispatchEvent(new Event('error'))")
    p.evaluate("v.currentTime=23;v.dispatchEvent(new Event('seeking'));v.dispatchEvent(new Event('seeked'))")
    p.evaluate("release({ok:true,json:async()=>({source:'oss',url:'https://oss.test/renew',expires_in:900})})")
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.evaluate('v.currentTime')==23
    assert p.locator('#v').get_attribute('src')=='https://oss.test/renew'


def test_native_pause_during_load_does_not_resume(player):
    p=player;p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));binding.play();v.dispatchEvent(new Event('error'))")
    p.wait_for_function("v.getAttribute('src')==='https://oss.test/v2'")
    p.evaluate("v.play();v.dispatchEvent(new Event('play'));v.pause();v.dispatchEvent(new Event('pause'))")
    plays=p.evaluate('plays')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.evaluate('plays')==plays


def test_source_reload_preserves_media_controls(player):
    p=player
    p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'));v.volume=.4;v.muted=true;v.playbackRate=1.5;v.load=()=>{v.volume=1;v.muted=false;v.playbackRate=1};v.dispatchEvent(new Event('error'))")
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    assert p.evaluate('[v.volume,v.muted,v.playbackRate]')==[.4,True,1.5]


def test_explicit_retry_reloads_failed_local_source(player):
    p=player
    p.evaluate("() => {window.loads=0;v.load=()=>loads++;fetch=async()=>({ok:true,json:async()=>({source:'local',url:'/source',expires_in:0})});}")
    p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('error'))")
    p.get_by_role('button',name='重试播放').click()
    assert p.evaluate('loads')==2


def test_returning_to_failed_player_keeps_explicit_retry(player):
    p=player;p.clock.install()
    p.evaluate("() => {fetch=async()=>({ok:true,json:async()=>({source:'local',url:'/source',expires_in:0})});}")
    p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('error'));window.active=false")
    p.clock.run_for(1000)
    p.evaluate('window.active=true;binding.open()')
    assert p.get_by_role('button',name='重试播放').is_visible()


def test_initial_local_source_does_not_claim_a_switch(player):
    p=player
    p.evaluate("() => {fetch=async()=>({ok:true,json:async()=>({source:'local',url:'/source',expires_in:0})});}")
    p.evaluate('binding.open()')
    assert p.locator('#v').get_attribute('src')=='/source'
    assert not p.get_by_text('已切换服务器',exact=True).is_visible()


def test_native_play_renewal_does_not_interrupt_advancing_video(player):
    p=player;p.clock.install();p.evaluate('binding.open()')
    p.evaluate("v.dispatchEvent(new Event('loadedmetadata'))")
    p.clock.run_for(850000)
    p.evaluate("v.play();v.dispatchEvent(new Event('play'));v.currentTime=1;v.dispatchEvent(new Event('playing'))")
    assert p.locator('#v').get_attribute('src')=='https://oss.test/v1'
    assert p.evaluate('requests')==2
