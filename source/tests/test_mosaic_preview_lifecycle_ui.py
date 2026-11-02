"""Preview receipts and failures belong to one operation, never the next editor."""
from test_mosaic_ui import editor,open_auto
from test_mosaic_tracking_capability_ui import add_manual_mosaic


def prepare_preview(page):
    open_auto(page);add_manual_mosaic(page)
    page.locator('[data-effect-target]').click();page.locator('#mosaicEffectPreview').click()
    page.locator('#mosaicTrackDialog').wait_for(state='visible')


def close_and_reopen(page):
    page.locator('#mosaicTrackClose').click();page.locator('#mosaicEffectCancel').click();page.locator('#mosaicClose').click()
    page.locator('[data-open-mosaic]').evaluate('(el)=>el.click()')
    page.locator('#mosaicWorkspace').wait_for(state='visible')


def test_late_closed_preview_receipt_is_stopped_without_unlocking_new_preview(editor):
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
    editor.wait_for_function("previewStops.includes('1'.repeat(32))")
    assert editor.locator('#mosaicTrackDialog').evaluate("el=>el.classList.contains('is-loading')")
    assert editor.locator('#mosaicTrackVideo').is_hidden()
    assert editor.locator('#mosaicAddClip').is_disabled()
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
    prepare_preview(editor);close_and_reopen(editor)
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
