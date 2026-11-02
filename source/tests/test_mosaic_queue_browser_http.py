"""End-to-end synthetic media, real HTTP, real Chromium and real FFmpeg."""
import pytest
from exactcut import accounts, cutting, workflow, mosaic_queue as queue, material_versions
from exactcut.util import file_sha256
from test_cutting_api import ready
from test_phase2_preview_api import fixture, serve
from test_mosaic_queue_integration import second_asset, wait


def test_browser_close_relogin_discard_and_confirm_real_media(ready):
    playwright=pytest.importorskip('playwright.sync_api')
    bid,rid,sha,source=ready;rid2=second_asset(bid,source,sha)
    material_versions.enroll(workflow.folder(bid))
    app=serve();app.auth_db=workflow.BATCHES.parent/'accounts.sqlite3'
    accounts.initialize(app.auth_db,app.password)
    base=f'http://127.0.0.1:{app.server_port}'
    dispatcher=queue.Dispatcher(.01);dispatcher.start()
    lease=cutting.lock_file(cutting.lock_root()/'compute.lock');errors=[]
    def login(browser):
        context=browser.new_context(viewport={'width':1440,'height':1000})
        response=context.request.post(base+'/api/login',data={'username':'admin','password':app.password},headers={'X-Exactcut-Request':'1'})
        assert response.ok
        page=context.new_page();page.set_default_timeout(10000);page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/#'+bid);page.locator('[data-tab="review"]').click();page.locator('[data-open-mosaic]').first.wait_for()
        return context,page
    def open_editor(page,index):
        page.locator('[data-open-mosaic]').nth(index).click()
        page.wait_for_function("!document.getElementById('mosaicWorkspace').hidden || !document.getElementById('mosaicBatchDialog').hidden")
    def submit(page):
        page.locator('#mosaicAddClip').click()
        page.locator('#mosaicStart').fill('00:00:03');page.locator('#mosaicEnd').fill('00:00:08')
        page.locator('#mosaicAddSubmit').click();page.locator('#mosaicBatchStart').click()
        page.locator('#mosaicProcessing').wait_for()
    try:
        with playwright.sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                context,page=login(browser)
                open_editor(page,0);submit(page)
                assert page.locator('#mosaicAddClip').is_disabled()
                page.locator('#mosaicClose').click()
                open_editor(page,1);submit(page)
                page.wait_for_function("document.getElementById('mosaicStop').textContent==='取消排队'")
                page.locator('#mosaicClose').click()
                open_editor(page,0)
                assert '删除 1 段' in page.locator('#mosaicBatchHint').inner_text()
                page.locator('#mosaicStop').click()
                page.wait_for_function("document.getElementById('mosaicStop').hidden")
                assert page.locator('#mosaicAddClip').is_enabled()
                context.close();browser.close()
                tasks=queue.load()['tasks'];assert len(tasks)==2
                assert queue.report(tasks[0])['status']=='cancelled'
                assert queue.report(tasks[1])['status']=='queued'
                lease.close()
                done=wait(bid,tasks[1]['session_id'],tasks[1]['id']);assert done['status']=='completed'
                # No browser is alive when the worker produces its completed report.
                assert file_sha256(workflow.preview_file(bid,rid2)[0])==sha
                browser=p.chromium.launch(headless=True);context,page=login(browser)
                open_editor(page,1);page.locator('#mosaicBatchDialog').wait_for()
                assert page.locator('#mosaicReplace').is_enabled()
                assert page.locator('#mosaicBatchStart').is_hidden()
                page.once('dialog',lambda dialog:dialog.accept());page.locator('#mosaicDiscard').click()
                page.wait_for_function("!document.getElementById('mosaicClear').disabled")
                assert '删除 1 段' in page.locator('#mosaicBatchHint').inner_text()
                assert file_sha256(workflow.preview_file(bid,rid2)[0])==sha
                # Local adjustment survives another close/reopen after abandonment.
                page.locator('#mosaicClose').click();open_editor(page,1)
                assert '删除 1 段' in page.locator('#mosaicBatchHint').inner_text()
                page.locator('#mosaicBatchStart').click();page.locator('#mosaicBatchDialog').wait_for()
                page.once('dialog',lambda dialog:dialog.accept());page.locator('#mosaicReplace').click()
                page.wait_for_function("!document.getElementById('mosaicDialog').open")
                assert file_sha256(workflow.preview_file(bid,rid2)[0])!=sha
                open_editor(page,1)
                assert page.locator('#mosaicSeek').get_attribute('max')=='83'
                assert file_sha256(source)==sha
                assert errors==[]
                context.close()
            finally:browser.close()
    finally:
        lease.close();dispatcher.stop(timeout=None);app.shutdown();app.server_close()

from interactive_test_support import interactive_dispatcher
