"""Preview receipts and failures belong to one operation, never the next editor."""
import pytest
from test_mosaic_ui import editor,open_auto
from test_mosaic_tracking_capability_ui import add_manual_mosaic

pytestmark = pytest.mark.ui


def prepare_preview(page):
    open_auto(page);add_manual_mosaic(page)
    page.locator('[data-effect-target]').click();page.locator('#mosaicEffectPreview').click()
    page.locator('#mosaicTrackDialog').wait_for(state='visible')


def close_and_reopen(page):
    page.locator('#mosaicTrackClose').click();page.locator('#mosaicEffectCancel').click();page.locator('#mosaicClose').click()
    page.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    page.locator('#mosaicWorkspace').wait_for(state='visible')


def test_late_closed_preview_receipt_does_not_cancel_or_relabel_new_preview(editor):
    editor.evaluate("""() => {const old=api;window.previewReceipts=[];window.previewStops=[];
      window.api=async(url,body)=>{
        if(url.endsWith('/clip-preview')&&body)return await new Promise(resolve=>previewReceipts.push(resolve));
        if(url.endsWith('/stop')&&body){previewStops.push(body.job_id);return {id:body.job_id,status:'cancelled'};}
        if(/\\/render\\/[12]{32}$/.test(url))return {id:url.split('/').at(-1),status:'completed',result:{}};
        return old(url,body);
      };}""")
    prepare_preview(editor);close_and_reopen(editor)
    assert editor.locator('#mosaicAddClip').is_enabled(),'closed preview leaked its busy flag into the next editor'
    editor.locator('[data-effect-target]').click();editor.locator('#mosaicEffectPreview').click()
    editor.wait_for_function('previewReceipts.length===2')
    editor.evaluate("previewReceipts[0]({id:'1'.repeat(32),status:'running'})")
    editor.wait_for_timeout(50)
    assert editor.evaluate('previewStops')==[]
    assert editor.locator('#mosaicTrackDialog').evaluate("el=>el.classList.contains('is-loading')")
    assert editor.locator('#mosaicTrackVideo').is_hidden()
    assert editor.locator('#mosaicAddClip').is_enabled()
    editor.evaluate("previewReceipts[1]({id:'2'.repeat(32),status:'completed',result:{}})")
    editor.wait_for_function("document.getElementById('mosaicTrackStatus').textContent==='预览已生成'")


def test_old_stop_failure_cannot_overwrite_next_editor_message(editor):
    editor.evaluate("""() => {const old=api;window.rejectOldStop=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/clip-preview')&&body)return {id:'1'.repeat(32),status:'running'};
        if(url.endsWith('/stop')&&body)return await new Promise((resolve,reject)=>window.rejectOldStop=reject);
        if(url.endsWith('/render/'+'1'.repeat(32)))return await new Promise(()=>{});
        return old(url,body);
      };}""")
    prepare_preview(editor)
    editor.locator('#mosaicTrackStop').click()
    editor.locator('#mosaicEffectCancel').click();editor.locator('#mosaicClose').click()
    editor.locator('[data-open-mosaic]').evaluate('(el)=>el.click()');editor.locator('#mosaicWorkspace').wait_for(state='visible')
    editor.wait_for_function('rejectOldStop!==null')
    message=editor.locator('#mosaicStatus').inner_text()
    editor.evaluate("rejectOldStop(new Error('old stop failed'))")
    editor.wait_for_timeout(50)
    assert editor.locator('#mosaicStatus').inner_text()==message
    assert editor.locator('#mosaicAddClip').is_enabled()


def test_closed_preview_failure_cannot_unlock_or_relabel_queued_final(editor):
    editor.evaluate("""() => {const old=api;window.rejectPreview=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/clip-preview')&&body)return await new Promise((resolve,reject)=>window.rejectPreview=reject);
        if(url.endsWith('/render')&&body)return {id:'f'.repeat(32),status:'queued',queued_task:true,source_sha256:session.source_sha256,...body};
        if(url.endsWith('/render/'+'f'.repeat(32)))return await new Promise(()=>{});
        return old(url,body);
      };}""")
    prepare_preview(editor)
    editor.locator('#mosaicTrackClose').click();editor.locator('#mosaicEffectCancel').click()
    editor.locator('#mosaicBatchStart').click()
    editor.wait_for_function("document.getElementById('mosaicStop').textContent==='取消排队'")
    before=editor.locator('#mosaicProcessingText').inner_text()
    editor.evaluate("rejectPreview(new Error('late preview failed'))")
    editor.wait_for_timeout(50)
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.locator('#mosaicStop').inner_text()=='取消排队'
    assert editor.locator('#mosaicProcessingText').inner_text()==before
    assert 'late preview failed' not in editor.locator('#mosaicBatchHint').inner_text()


def test_late_closed_receipt_cannot_replace_the_newer_preview_resume_pointer(editor):
    editor.evaluate("""() => {const old=api;window.receipts=[];
      window.api=async(url,body)=>url.endsWith('/clip-preview')&&body
        ? await new Promise(resolve=>receipts.push(resolve)):old(url,body);
    }""")
    prepare_preview(editor);close_and_reopen(editor)
    editor.locator('[data-effect-target]').click();editor.locator('#mosaicEffectPreview').click()
    editor.wait_for_function('receipts.length===2')
    editor.evaluate("receipts[1]({id:'2'.repeat(32),status:'completed',result:{}})")
    editor.wait_for_function("document.getElementById('mosaicTrackStatus').textContent==='预览已生成'")
    editor.evaluate("receipts[0]({id:'1'.repeat(32),status:'running'})")
    editor.wait_for_timeout(50)
    saved=editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('preview-job.v1.')&&!k.endsWith('.request'))))")
    assert saved['id']=='2'*32
    assert editor.locator('#mosaicTrackStatus').inner_text()=='预览已生成'


def test_preview_receipt_two_tabs_refresh_and_material_switch_preserve_the_draft(editor):
    import io,re,json
    from pathlib import Path
    from PIL import Image
    root=Path(__file__).parents[1]/'exactcut/web'
    html=re.sub(r'<script src="[^"]+"></script>','',(root/'index.html').read_text())
    jpeg=io.BytesIO();Image.new('RGB',(160,96),'#555555').save(jpeg,'JPEG')
    editor.evaluate("() => {const old=api;window.resolveOld=null;window.api=async(u,b)=>u.endsWith('/clip-preview')&&b?await new Promise(r=>resolveOld=r):old(u,b);}")
    prepare_preview(editor)
    editor.locator('#mosaicTrackClose').click();editor.locator('#mosaicEffectCancel').click();editor.locator('#mosaicClose').click()
    seed=editor.evaluate('({session,current,currentUser,findings})')
    draft_key=editor.evaluate("Object.keys(localStorage).find(k=>k.includes('workbench.v3.'))")
    draft=json.loads(editor.evaluate('(k)=>localStorage.getItem(k)',draft_key))
    other=editor.context.new_page();other.set_default_timeout(3000)
    other.route('**/*',lambda route:route.fulfill(body=html,content_type='text/html'))
    other.route('**/frame/*',lambda route:route.fulfill(body=jpeg.getvalue(),content_type='image/jpeg'))
    def initialize():
        other.evaluate("""seed=>{Object.assign(window,seed);window.$=id=>document.getElementById(id);window.esc=String;window.appUrl=u=>u;window.calls=[];
          window.api=async(u,b)=>{if(u.endsWith('/scan'))return {enabled:true,report:{id:'existing',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]}};
          if(u.endsWith('/clip-preview')||u.includes('/render/'))return {id:'2'.repeat(32),status:'completed',result:{}};return session;};
          $('assets').innerHTML='<article data-asset="0"><div class="cut-tools"><button data-open-cut>剪切</button></div></article>';
          $('assets').closest('[hidden]')?.removeAttribute('hidden');}""",seed)
        for name in ('editor-busy.js','editor-source.js','mosaic.js'):other.add_script_tag(content=(root/name).read_text())
    try:
        other.goto('http://fixture.test/');initialize();open_auto(other)
        other.locator('[data-effect-target]').click();other.locator('#mosaicEffectPreview').click()
        other.wait_for_function("document.getElementById('mosaicTrackStatus').textContent==='预览已生成'")
        editor.evaluate("resolveOld({id:'1'.repeat(32),status:'running'})");editor.wait_for_timeout(50)
        saved=other.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('preview-job.v1.')&&!k.endsWith('.request'))))")
        assert saved['id']=='2'*32
        assert json.loads(other.evaluate('(k)=>localStorage.getItem(k)',draft_key))==draft
        other.reload();initialize();open_auto(other)
        other.locator('#mosaicResumePreview').wait_for(state='visible')
        assert json.loads(other.evaluate('(k)=>localStorage.getItem(k)',draft_key))==draft
        other.locator('#mosaicClose').click()
        other.evaluate("session.id='other-session';current.assets[0].request_id='R2'")
        open_auto(other)
        assert other.locator('#mosaicResumePreview').is_hidden()
        assert json.loads(other.evaluate('(k)=>localStorage.getItem(k)',draft_key))==draft
    finally:other.close()
