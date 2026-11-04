"""Fixed-only mode: no tracking entry, legacy tracked drafts open as fixed without a tracker."""
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


def add_manual_mosaic(page,initial_delete=False):
    page.locator('#mosaicAddClip').click()
    page.locator('#mosaicStart').fill('00:01:07');page.locator('#mosaicEnd').fill('00:02:28')
    if initial_delete:
        page.locator('#mosaicAddSubmit').click()
        page.locator('[data-event-draw]').click()
    else:page.locator('#mosaicManualAction').select_option('mosaic')
    draw_region(page)
    if not initial_delete:page.locator('#mosaicAddSubmit').click()


def save_effect(page,style='blur'):
    page.locator('[data-scan-event]').first.click()
    page.locator('#mosaicLiveStyle').select_option(style)
    assert '效果已保存' in page.locator('#mosaicStatus').inner_text()


def capture_render(page):
    page.evaluate("""() => {const previous=api;window.api=async(url,body)=>{
      if(url.endsWith('/render')&&body){window.manualBody=body;return {id:'b'.repeat(32),status:'completed',...body,result:{}};}
      return previous(url,body);
    };}""")


@pytest.mark.parametrize('initial_delete',[False,True])
def test_manual_fixed_draw_save_effect_and_submit_without_tracker(editor,initial_delete):
    open_auto(editor);add_manual_mosaic(editor,initial_delete=initial_delete);save_effect(editor)
    assert editor.locator('[data-event-delete]').get_attribute('aria-checked')=='false'
    assert editor.locator('[data-target-remove]').get_attribute('aria-checked')=='true'
    assert '删除 0 段 · 打码 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert saved_targets(editor)[0]['motion']=='fixed'
    assert editor.locator('#mosaicBatchStart').is_enabled()
    assert editor.locator('#mosaicBatchStart').get_attribute('data-generation-lock') is None
    capture_render(editor)
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('manualBody.targets[0].motion')=='fixed'
    assert editor.evaluate('manualBody.targets[0].effect')=={'style':'blur','strength':'medium','expansion':'none'}


def test_new_manual_clip_has_no_tracking_choice(editor):
    editor.evaluate('session.capabilities.tracking=true')
    open_auto(editor);editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicManualAction').select_option('mosaic')
    assert editor.locator('#mosaicMotion').is_hidden()
    assert editor.locator('#mosaicMotion').input_value()=='fixed'
    assert editor.locator('option[value="tracked"]').count()==0
    assert editor.locator('#mosaicTrack').is_hidden()


@pytest.mark.parametrize('legacy_missing_motion',[False,True])
def test_legacy_tracked_draft_opens_as_fixed_with_notice_without_tracker(editor,legacy_missing_motion):
    open_auto(editor);add_manual_mosaic(editor)
    editor.locator('#mosaicClose').click()
    editor.evaluate("""missing => {const key=Object.keys(localStorage).find(k=>k.includes('workbench.v3'));
      const draft=JSON.parse(localStorage.getItem(key));
      if(missing)delete draft.targets[0].motion;else draft.targets[0].motion='tracked';
      draft.effect={style:'soft_blur',strength:'heavy',expansion:'large'};
      localStorage.setItem(key,JSON.stringify(draft));}""",legacy_missing_motion)
    open_auto(editor)
    assert editor.locator('#mosaicBatchStart').is_enabled()
    assert '旧草稿已按当前规则改为固定位置' in editor.locator('#mosaicBatchHint').inner_text()
    assert '跟随目标当前不可用' not in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('[data-scan-event]').click()
    assert editor.locator('#mosaicMotion').input_value()=='fixed'
    assert editor.locator('#mosaicLiveStyle').input_value()=='blur','legacy soft blur maps to blur'
    assert all(t['motion']=='fixed' for t in saved_targets(editor))
    capture_render(editor)
    editor.locator('#mosaicBatchStart').click()
    assert editor.evaluate('manualBody.targets[0].motion')=='fixed'
    assert editor.evaluate('manualBody.targets[0].effect')=={'style':'blur','strength':'medium','expansion':'none'}
