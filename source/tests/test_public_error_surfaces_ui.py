"""Legacy task reports and client errors never become the primary UI message."""
import pytest
from test_cutting_ui import editor as cutting_editor
from test_mosaic_ui import editor as mosaic_editor, open_editor
from test_preview_subtitles_ui import page as subtitle_page
from test_source_frame_ui import frame_page

pytestmark = pytest.mark.ui

RAW = '处理失败：1 validation error for InternalTask input_value={} /srv/private/source.mp4'


@pytest.mark.parametrize('reported', [False, True])
def test_cutting_scan_error_is_friendly(cutting_editor, reported):
    page = cutting_editor
    page.evaluate('''({raw,reported})=>{window.api=async()=>{if(!reported)throw Error(raw);return {enabled:true,report:{status:'failed',error:raw}}}}''',
                  {'raw': RAW, 'reported': reported})
    page.locator('#cutScan').click()
    page.wait_for_function("document.getElementById('cutScanStatus').textContent==='识别未完成，请重试'")


def test_mosaic_stored_scan_error_is_friendly(mosaic_editor):
    page = mosaic_editor
    page.evaluate('''raw=>{const original=api;window.api=async(url,body)=>url.endsWith('/scan')?{enabled:true,report:{id:'failed',status:'failed',events:[],error:raw}}:original(url,body)}''', RAW)
    open_editor(page)
    page.wait_for_function("!document.getElementById('mosaicScanStatus').textContent.includes('InternalTask')")
    assert '操作未完成' in page.locator('#mosaicScanStatus').inner_text()


def test_preview_subtitle_stored_error_is_friendly(subtitle_page):
    page = subtitle_page
    page.evaluate('''raw=>{window.api=async()=>({enabled:true,binding:{...binding},report:{id:'task',status:'failed',cues:[],error:raw}})}''', RAW)
    page.locator('[data-subtitle-toggle]').check()
    page.wait_for_function("document.querySelector('.subtitle-status').textContent==='字幕识别失败，请点击重试'")


def test_runtime_banner_and_toast_do_not_expose_details(frame_page):
    page = frame_page
    page.evaluate('''raw=>{current.runtime={status:'failed',message:raw,error_info:{code:'unknown',title:'制作未完成',action:'请重试制作',detail:raw},events:[]};renderRuntime();toast(raw,true)}''', RAW)
    for selector in ['#liveMessage', '#progressMessage', '#toast']:
        assert 'InternalTask' not in page.locator(selector).inner_text()
        assert '/srv/' not in page.locator(selector).inner_text()
    assert '制作未完成' in page.locator('#liveMessage').inner_text()
    assert not page.locator('#runtimeError details').evaluate('(el)=>el.open')
    assert RAW in page.locator('#runtimeError details').text_content()


def test_helper_network_error_is_friendly(frame_page):
    from pathlib import Path
    page = frame_page
    page.evaluate("()=>{api=async()=>{throw new TypeError('Failed to fetch')}}")
    page.add_script_tag(content=(Path(__file__).parents[1] / 'exactcut/web/helper.js').read_text())
    page.locator('#helpNav').click()
    page.locator('#helpQuestion').fill('当前进行到哪一步？')
    page.locator('#helpSend').click()
    page.wait_for_function("document.getElementById('helpMessages').textContent.includes('暂时无法取得回答，请稍后重试')")
    assert 'Failed to fetch' not in page.locator('#helpMessages').inner_text()


def test_login_network_error_is_friendly(frame_page):
    from pathlib import Path
    page = frame_page
    html = (Path(__file__).parents[1] / 'exactcut/web/login.html').read_text()
    page.route('**/login', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.route('**/api/login', lambda route: route.abort())
    page.goto('http://fixture.test/login')
    page.locator('#username').fill('fixture')
    page.locator('#password').fill('fixture-password')
    page.locator('#submit').click()
    page.wait_for_function("document.getElementById('error').textContent==='登录未完成，请检查网络后重试'")
