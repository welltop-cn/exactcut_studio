"""Tracking capability must explain blocking and preserve the user's position mode."""
import pytest
from test_mosaic_ui import editor, open_auto

pytestmark = pytest.mark.ui


def saved_targets(page):
    return page.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3')))).targets")


def draw_region(page):
    page.locator('#mosaicAddRegion').click()
    box=page.locator('#mosaicOverlay').bounding_box()
    page.mouse.move(box['x']+box['width']*.2,box['y']+box['height']*.2);page.mouse.down()
    page.mouse.move(box['x']+box['width']*.6,box['y']+box['height']*.6);page.mouse.up()


def add_manual_mosaic(page,motion='fixed',initial_delete=False):
    page.locator('#mosaicAddClip').click()
    page.locator('#mosaicStart').fill('00:01:07');page.locator('#mosaicEnd').fill('00:02:28')
    if initial_delete:
        page.locator('#mosaicAddSubmit').click()
        page.locator('[data-event-draw]').click()
    else:page.locator('#mosaicManualAction').select_option('mosaic')
    page.locator('#mosaicMotion').select_option(motion)
    draw_region(page)
    page.locator('#mosaicConfirm' if initial_delete else '#mosaicAddSubmit').click()


def save_effect(page):
    page.locator('[data-scan-event]').first.click()
    page.locator('#mosaicLiveStyle').select_option('solid')
    assert '效果已保存' in page.locator('#mosaicStatus').inner_text()


@pytest.mark.parametrize('initial_delete',[False,True])
def test_manual_fixed_draw_save_effect_and_submit_without_tracker(editor,initial_delete):
    open_auto(editor);add_manual_mosaic(editor,initial_delete=initial_delete);save_effect(editor)
    assert editor.locator('[data-event-delete]').get_attribute('aria-checked')=='false'
    assert editor.locator('[data-target-remove]').get_attribute('aria-checked')=='true'
    assert '删除 0 段 · 打码 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert saved_targets(editor)[0]['motion']=='fixed'
    assert editor.locator('#mosaicBatchStart').is_enabled()
    assert editor.locator('#mosaicBatchStart').get_attribute('data-generation-lock') is None
    editor.evaluate("""() => {const previous=api;window.api=async(url,body)=>{
      if(url.endsWith('/render')&&body){window.manualBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}
      return previous(url,body);
    };}""")
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('manualBody.targets[0].motion')=='fixed'
    assert editor.evaluate('manualBody.targets[0].effect.style')=='solid'


def test_new_manual_clip_cannot_select_unavailable_tracking(editor):
    open_auto(editor);editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic')
    assert editor.locator('#mosaicMotion option[value="tracked"]').evaluate('el=>el.disabled')
    editor.locator('#mosaicMotion').focus();editor.locator('#mosaicMotion').press('ArrowDown')
    assert editor.locator('#mosaicMotion').input_value()=='fixed'
    # A stale/programmatic change must not bypass the capability restriction.
    editor.locator('#mosaicMotion').evaluate("el=>{el.value='tracked';el.dispatchEvent(new Event('change',{bubbles:true}));}")
    assert editor.locator('#mosaicMotion').input_value()=='fixed'
    assert '跟随目标当前不可用' in editor.locator('#mosaicStatus').inner_text()


@pytest.mark.parametrize('legacy_missing_motion',[False,True])
def test_saved_tracking_blocks_with_visible_reason_until_explicit_mode_change(editor,legacy_missing_motion):
    editor.evaluate('session.capabilities.tracking=true')
    open_auto(editor);add_manual_mosaic(editor,motion='tracked')
    editor.locator('#mosaicClose').click()
    if legacy_missing_motion:
        editor.evaluate("""() => {const key=Object.keys(localStorage).find(k=>k.includes('workbench.v3'));
          const draft=JSON.parse(localStorage.getItem(key));delete draft.targets[0].motion;localStorage.setItem(key,JSON.stringify(draft));}""")
    before=saved_targets(editor)
    editor.evaluate('session.capabilities.tracking=false')
    open_auto(editor);save_effect(editor)
    assert saved_targets(editor)==before
    assert editor.locator('#mosaicBatchStart').is_disabled()
    assert editor.locator('#mosaicBatchStart').get_attribute('data-generation-lock') is None
    assert editor.locator('#mosaicScan').is_enabled()
    assert '跟随目标当前不可用' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('[data-scan-event]').click()
    assert editor.locator('#mosaicMotion').input_value()=='tracked'
    assert editor.locator('#mosaicMotion option[value="tracked"]').evaluate('el=>el.disabled')
    editor.locator('#mosaicMotion').select_option('fixed')
    assert saved_targets(editor)==before
    editor.locator('#mosaicConfirm').click()
    assert saved_targets(editor)[0]['motion']=='fixed'
    assert editor.locator('#mosaicBatchStart').is_enabled()
    assert '跟随目标当前不可用' not in editor.locator('#mosaicBatchHint').inner_text()
