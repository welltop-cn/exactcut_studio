import re
import pytest
from pathlib import Path
root=Path(__file__).parents[1]/'exactcut/web'
html=re.sub(r'<script src="[^"]+"></script>','',(root/'index.html').read_text())
js='\n'.join(l for l in (root/'app.js').read_text().splitlines() if not l.startswith('(async()=>{try{const initial='))

@pytest.mark.parametrize('mode', ['clips','composition','text'])
def test_grouped_editor_preserves_fields_and_delegated_events(mode):
    with pytest.importorskip('playwright.sync_api').sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1280,'height':1000}); errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/*',lambda route:route.fulfill(body=html,content_type='text/html'));page.goto('http://fixture.test');page.add_script_tag(content=js)
        row=dict(request_id='R1',seq=1,hook_text='Hello world',timeline_text='字卡|Story|1.5s',hook_options=dict(mode='none',style='default',position='top',duration_ms=3000,manual_ranges=[]))
        if mode=='clips':row['clips']=[dict(episode='1',start_s='0',end_s='2')]
        if mode=='composition':row['composition']=dict(opening=dict(text='Hello world',style='default',position='top',duration_s=3),plots=[dict(episode='1',start='00:00',end='00:02')])
        page.evaluate('''row=>{currentUser={features:{material_fetch:false}};current={id:'fixture',filename:'网页填写',revision:1,warnings:[],errors:[],assets:[],runtime:{status:'draft'},draft:{drama_id:'101500',author:'test',business_date:'20261012',rows:[row]}};selectedId=current.id;showPage('workspace');setTab('editor');renderEditor();api=async(path,payload)=>{globalThis.savedPayload=structuredClone(payload);return structuredClone(current)};renderRuntime=()=>{};}''',row)
        assert page.locator('.material-section').count()==3
        if mode!='composition':
            page.evaluate("hookOptions(current.draft.rows[0]).mode='manual';hookOptions(current.draft.rows[0]).manual_ranges=[{start:0,end:5}];renderEditor()")
            assert page.locator('.hook-display').get_attribute('open') is not None
            page.locator('.hook-display>summary').click()
            page.get_by_label('字幕文案（Hook）').fill('Hello changed')
            assert page.locator('.hook-display').get_attribute('open') is not None
            assert page.evaluate('current.draft.rows[0].hook_options.manual_ranges.length===0')
            assert page.get_by_text('文案已修改，请重新选择重点词并预览。',exact=True).is_visible()
            page.locator('.hook-display>summary').click()
        assert page.locator('.request-extra').get_by_text('时间线写法：',exact=False).count()==0
        assert page.locator('.story-section').get_by_text('时间线写法：',exact=False).count()==(1 if mode=='text' else 0)
        page.locator('.hook-display>summary').click()
        page.locator('.hook-typography>summary').click()
        page.get_by_label('字体',exact=True).select_option('serif')
        page.get_by_label('字号（32～96，留空为自动）').fill('60')
        page.get_by_label('显示时长（秒）').fill('5')
        page.locator('.hook-display>summary').click()
        page.get_by_label('结尾画面',exact=True).select_option('last_frame')
        page.get_by_label('停留时长（秒）').fill('4')
        page.get_by_label('引导文案（选填）').fill('Watch more!')
        before=page.evaluate('structuredClone(current.draft)')
        assert page.evaluate('saveCurrent()')
        assert page.evaluate('savedPayload.draft')==before
        assert page.evaluate('current.draft')==before
        r=before['rows'][0];assert r['hook_options']['font']=='serif' and r['hook_options']['font_size']==60
        assert (r['composition']['opening']['duration_s'] if mode=='composition' else r['hook_options']['duration_ms'])==(5 if mode=='composition' else 5000)
        ending=r['composition']['ending'] if mode=='composition' else r['ending'];assert ending['duration_ms']==4000 and ending['guide']=='Watch more!'
        page.evaluate("current.runtime.status='review';controls()")
        assert page.get_by_label('结尾画面',exact=True).is_disabled()
        assert page.locator('#hook-size-0').is_disabled()
        assert not errors,errors

        page.close()
        browser.close()
