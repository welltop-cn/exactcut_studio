from pathlib import Path

import pytest


def test_unchanged_timing_keeps_dom_focus_and_expanded_details():
    playwright = pytest.importorskip('playwright.sync_api')
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = '\n'.join(line for line in source.splitlines() if line.startswith(('function refreshClipTiming(', 'function refreshTiming(')))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<div data-timing-index="0"></div>')
            page.evaluate("globalThis.value='old';function timingMarkup(i){return `<div data-timing-index=\"${i}\"><details><summary>时长</summary><input value=\"${value}\"></details></div>`;}" + helpers)
            page.evaluate('refreshTiming()')
            page.locator('summary').click()
            page.locator('input').focus()
            page.evaluate('window.originalInput=document.querySelector("input");refreshTiming();refreshTiming()')
            assert page.evaluate('document.activeElement===originalInput && originalInput.isConnected')
            assert page.locator('details').get_attribute('open') is not None
            page.evaluate("value='new';refreshTiming()")
            assert page.locator('input').input_value() == 'new'
            assert page.locator('details').get_attribute('open') is not None
        finally:
            browser.close()


def test_text_timeline_opens_as_editable_clip_fields_without_implicit_draft_changes():
    import re
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root / 'index.html').read_text())
    source = '\n'.join(line for line in (root / 'app.js').read_text().splitlines() if not line.startswith('(async()=>{try{const initial='))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
            page.goto('http://fixture.test/')
            page.add_script_tag(content=source)
            page.evaluate('''() => {
                currentUser={features:{material_fetch:false}};
                current={id:'fixture',media_managed:true,filename:'网页填写',revision:1,warnings:[],errors:[],assets:[],
                  runtime:{status:'draft'},draft:{drama_id:'101494',author:'test',business_date:'20261011',rows:[
                    {request_id:'R1',seq:1,hook_text:'test',timeline_text:'EP1 7s-37s',hook_options:{mode:'none',style:'default'}}]},
                  clip_editor:{R1:[{episode:'1',start_m:'00',start_s:'07',start_ms:'000',end_m:'00',end_s:'37',end_ms:'000'}]}};
                selectedId=current.id;showPage('workspace');setTab('editor');renderEditor();
            }''')
            assert page.locator('[data-field="timeline_text"]').count() == 0
            field = page.get_by_role('textbox', name='片段 1 开始 秒', exact=True)
            assert field.input_value() == '07'
            assert page.evaluate('current.draft.rows[0].clips === undefined && !dirty')
            page.evaluate("current.runtime.status='review';controls();setTab('editor')")
            assert field.is_disabled()
            assert page.locator('#editorToolbar').is_hidden()
            page.evaluate("controls.editingId=current.id;controls()")
            assert field.is_enabled()
            assert page.locator('#editorToolbar').is_visible()
            field.fill('08')
            assert page.evaluate("dirty && current.draft.rows[0].clips[0].start_s === '08'")
            assert page.evaluate("current.clip_editor.R1[0].start_s === '07'")
            page.get_by_role('button', name='＋ 添加片段', exact=True).click()
            assert page.get_by_role('textbox', name='片段 2 集号', exact=True).is_visible()
            page.evaluate("current.source_prefetch={episodes:[{episode:'EP1',status:'ready'}]};renderSourcePrefetch();renderSourcePrefetch()")
            assert page.locator("#sourcePrefetch").is_hidden()
            page.evaluate("current.runtime.status='review';renderSourcePrefetch()")
            assert page.locator("#sourcePrefetch").is_hidden()
            page.evaluate("current.source_prefetch={episodes:[{episode:'EP1',status:'failed'}]};renderSourcePrefetch()")
            assert page.get_by_role('button', name='重试正片下载', exact=True).is_visible()
            page.evaluate("current.source_prefetch={episodes:[{episode:'EP1',status:'ready'}]};renderSourcePrefetch()")
            assert page.locator("#sourcePrefetch").is_hidden()
        finally:
            browser.close()


@pytest.mark.parametrize('failure', ['error', 'download'])
def test_new_timing_error_opens_previously_collapsed_review(failure):
    playwright = pytest.importorskip('playwright.sync_api')
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = '\n'.join(line for line in source.splitlines() if line.startswith((
        'function refreshClipTiming(', 'function timingTable(', 'function seconds(',
        'function clock(', 'function esc(')))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<div data-timing-index="0"></div>')
            page.evaluate("globalThis.esc=String;\n" + helpers + '''
                globalThis.timing={segments:[{segment:1,type:'episode',episode:'EP1',
                  start_ms:0,end_ms:2000,duration_ms:2000,error:'',source_status:'missing'}],
                  planned_duration_ms:2000};
                function timingMarkup(i){return `<div data-timing-index="${i}">${timingTable(timing,'')}</div>`;}
                refreshClipTiming(0);
            ''')
            assert page.locator('details').get_attribute('open') is not None
            assert '待核对' in page.locator('summary').inner_text()
            page.locator('summary').click()
            page.evaluate("timing.planned_duration_ms=3000;refreshClipTiming(0)")
            assert page.locator('details').get_attribute('open') is None
            if failure == 'error':
                page.evaluate("timing.segments[0].error='终点超出原片时长';refreshClipTiming(0)")
                assert page.get_by_role('cell', name='终点超出原片时长').is_visible()
            else:
                page.evaluate("timing.segments[0].source_status='failed';refreshClipTiming(0)")
                assert page.get_by_role('cell', name='下载失败，可重试').is_visible()
                assert '下载失败' in page.locator('summary').inner_text()
        finally:
            browser.close()
