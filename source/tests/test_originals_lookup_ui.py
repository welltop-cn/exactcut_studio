from test_clip_text_ui import page
import pytest

pytestmark = pytest.mark.ui


def pending_lookup(page):
    page.evaluate("clearTimeout(originalsLookupTimer);window.lookups=[];api=()=>new Promise((resolve,reject)=>lookups.push({resolve,reject}));scheduleOriginalsLookup(true)")


def test_available_catalog_enables_download_without_second_lookup(page):
    pending_lookup(page)
    assert page.locator('#originalsOpen').is_disabled()
    page.evaluate("lookups[0].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10},{episode:'EP2',listed:false,bytes:20}]})")
    page.wait_for_function("!document.querySelector('#originalsOpen').disabled")
    assert 'EP1–EP2' in page.locator('#originalsStatus').inner_text()
    page.locator('#originalsOpen').click()
    assert page.locator('[data-original-pick]').count() == 2
    assert page.evaluate('lookups.length') == 1


def test_empty_and_failed_catalog_are_distinct_and_retryable(page):
    pending_lookup(page)
    page.evaluate("lookups[0].resolve({drama_id:'101494',episodes:[]})")
    page.wait_for_function("originalsLookup.status==='missing'")
    assert page.locator('#originalsOpen').is_disabled()
    page.evaluate("scheduleOriginalsLookup(true);lookups[1].resolve({drama_id:'101494',episodes:[],notice:'源桶暂时读不到，列表只包含内容桶里的原片。'})")
    page.wait_for_function("originalsLookup.status==='error'")
    assert '没有' not in page.locator('#originalsStatus').inner_text()
    page.locator('#originalsLookupRetry').click()
    page.evaluate("lookups[2].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10}]})")
    page.wait_for_function("!document.querySelector('#originalsOpen').disabled")


def test_late_catalog_cannot_enable_another_drama(page):
    pending_lookup(page)
    page.locator('#meta-drama_id').fill('101606')
    page.evaluate("scheduleOriginalsLookup(true);lookups[1].resolve({drama_id:'101606',episodes:[]});lookups[0].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10}]})")
    page.wait_for_function("originalsLookup.status==='missing'")
    assert page.evaluate('originalsLookup.drama') == '101606'
    assert page.locator('#originalsOpen').is_disabled()


def test_incomplete_catalog_does_not_claim_episode_absence(page):
    pending_lookup(page)
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'2'}];lookups[0].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10}],notice:'源桶暂时读不到，列表只包含内容桶里的原片。'})")
    page.wait_for_function("originalsLookup.status==='ready'")
    assert '不在物料库' not in page.locator('#originalsStatus').inner_text()
    assert '源桶暂时读不到' in page.locator('#originalsStatus').inner_text()


def test_missing_episode_warning_updates_with_draft(page):
    pending_lookup(page)
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'2'}];lookups[0].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10}]})")
    page.wait_for_function("originalsLookup.status==='ready'")
    assert 'EP2 不在物料库' in page.locator('#originalsStatus').inner_text()
    page.evaluate("current.draft.rows[0].clips[0].episode='1';changed()")
    assert '不在物料库' not in page.locator('#originalsStatus').inner_text()


def test_late_dialog_catalog_cannot_replace_new_project_list(page):
    pending_lookup(page)
    page.evaluate("loadOriginals(true);current.id='second';current.draft.drama_id='101606';renderEditor();document.querySelector('#originalsBox').innerHTML='new project'")
    page.evaluate("lookups[1].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10}]})")
    page.wait_for_timeout(50)
    assert page.locator('#originalsBox').inner_text() == 'new project'


def test_local_catalog_preserves_unknown_listing_status_through_cache_and_dialog(page):
    pending_lookup(page)
    page.evaluate("lookups[0].resolve({drama_id:'101494',episodes:[{episode:'EP1',listed:true,bytes:10},{episode:'EP2',listed:false,bytes:20},{episode:'EP3',listed:null,bytes:30}]})")
    page.wait_for_function("!document.querySelector('#originalsOpen').disabled")
    assert page.evaluate('originalsLookup.catalog.episodes.map(ep=>ep.listed)') == [True, False, None]
    assert page.locator('#originalsStatus').inner_text() == '有 3 集原片（EP1–EP3）'
    page.locator('#originalsOpen').click()
    rows = page.locator('.original-row')
    assert rows.count() == 3
    assert rows.nth(0).locator('.unlisted').count() == 0
    assert rows.nth(1).locator('.unlisted').inner_text() == '未上架'
    assert rows.nth(2).locator('.unlisted').count() == 0
    assert '上架状态未查询' not in page.locator('#originalsBox').inner_text()
