import re
from pathlib import Path

import pytest


@pytest.fixture
def page():
    playwright = pytest.importorskip('playwright.sync_api')
    root = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (root / 'index.html').read_text())
    source = '\n'.join(line for line in (root / 'app.js').read_text().splitlines() if not line.startswith('(async()=>{try{const initial='))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        page.goto('http://fixture.test/')
        page.add_script_tag(content=source)
        page.evaluate('''() => {
            currentUser={features:{material_fetch:false}};
            current={id:'fixture',filename:'网页填写',revision:1,warnings:[],errors:[],assets:[],
              runtime:{status:'draft'},draft:{drama_id:'101494',author:'test',business_date:'20261011',rows:[
                {request_id:'R1',seq:1,hook_text:'保留字幕',clips:[blankClip()],hook_options:{mode:'none',style:'default'}}]}};
            selectedId=current.id;showPage('workspace');setTab('editor');renderEditor();
        }''')
        yield page
        browser.close()


def parse(page, episode='30'):
    clip = dict(episode=episode, start_m='01', start_s='20', start_ms='012', end_m='01', end_s='47', end_ms='000')
    page.route('**/api/clip-text', lambda route: route.fulfill(json={'clips': [clip], 'errors': []}))
    page.get_by_role('button', name='粘贴文本识别', exact=True).click()
    page.get_by_label('复制的集号和时间段', exact=True).fill('第30集 1:20.012-1:47')


def test_fill_append_replace_undo(page):
    parse(page)
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.wait_for_function('current.draft.rows[0].clips[0].episode === "30"')
    assert page.evaluate('current.draft.rows[0].clips.length') == 1
    assert page.get_by_role('textbox', name='片段 1 开始 毫秒', exact=True).input_value() == '012'
    parse(page, '31')
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.wait_for_function('current.draft.rows[0].clips.length === 2')
    parse(page, '32')
    page.get_by_label('填入方式', exact=True).select_option('replace')
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.wait_for_function('current.draft.rows[0].clips[0].episode === "32"')
    page.get_by_role('button', name='撤销本次填入', exact=True).click()
    assert page.evaluate('current.draft.rows[0].clips.map(c=>c.episode)') == ['30', '31']
    assert page.evaluate('current.draft.rows[0].hook_text') == '保留字幕'


def test_errors_keep_original_and_input(page):
    parse(page)
    page.route('**/api/clip-text', lambda route: route.fulfill(json={'clips': [], 'errors': ['第1行：时间不明确']}))
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.get_by_text('第1行：时间不明确', exact=True).wait_for()
    assert page.evaluate('current.draft.rows[0].clips[0].episode') == ''
    assert page.get_by_label('复制的集号和时间段', exact=True).input_value()


def test_late_response_after_cancel_does_not_fill(page):
    parse(page)
    page.evaluate('() => {api=()=>new Promise(resolve=>{window.finishClipParse=resolve})}')
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.get_by_role('button', name='取消识别', exact=True).click()
    page.evaluate('finishClipParse({clips:[{...blankClip(),episode:"30"}],errors:[]})')
    assert page.evaluate('current.draft.rows[0].clips[0].episode') == ''


def test_late_response_after_edit_does_not_overwrite(page):
    parse(page)
    page.evaluate('() => {api=()=>new Promise(resolve=>{window.finishClipParse=resolve})}')
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.evaluate('current.draft.rows[0].clips[0].episode="7";finishClipParse({clips:[{...blankClip(),episode:"30"}],errors:[]})')
    page.get_by_text('片段已被修改，请关闭后重新识别', exact=True).wait_for()
    assert page.evaluate('current.draft.rows[0].clips[0].episode') == '7'


def test_composition_and_limit(page):
    page.evaluate('current.draft.rows[0].composition=blankComposition();renderEditor()')
    parse(page)
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.wait_for_function('current.draft.rows[0].composition.plots[0].episode === "30"')
    assert page.evaluate('current.draft.rows[0].composition.plots[0].start') == '01:20.012'
    page.evaluate('current.draft.rows[0].composition.plots=Array.from({length:12},()=>({episode:"1",start:"00:00",end:"00:01"}));renderEditor()')
    parse(page)
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.get_by_text('一条成片最多 12 段，请减少片段或选择替换', exact=True).wait_for()
    assert page.evaluate('current.draft.rows[0].composition.plots.length') == 12


def test_transport_failure_preserves_input(page):
    parse(page)
    page.route('**/api/clip-text', lambda route: route.fulfill(status=503, json={'error': 'AI 暂时不可用'}))
    page.get_by_role('button', name='识别并填入', exact=True).click()
    page.get_by_text('AI 暂时不可用', exact=True).wait_for()
    assert page.get_by_label('复制的集号和时间段', exact=True).input_value()
    assert page.get_by_role('button', name='识别并填入', exact=True).is_enabled()
