from test_clip_text_ui import page, parse


def test_clip_effect_controls_and_invalidation(page):
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'1',end_s:'02'},{...blankClip(),episode:'2',end_s:'03'}];hookOptions(current.draft.rows[0]).preview_key='old';renderEditor()")
    assert page.get_by_label('进入本段时闪白', exact=True).count()==1
    captions=page.get_by_label('本段字幕（选填）', exact=True)
    assert captions.count()==2
    captions.nth(1).fill('第二段字幕')
    assert page.get_by_label('本段字幕时长（秒）', exact=True).input_value()=='1.5'
    page.get_by_label('本段字幕时长（秒）', exact=True).fill('2.3')
    page.get_by_label('进入本段时闪白', exact=True).check()
    assert page.evaluate('current.draft.rows[0].clips[1].segment_caption')=={'text':'第二段字幕','duration_ms':2300}
    assert page.evaluate('current.draft.rows[0].clips[1].transition_in')=='flash_white'
    duration=page.get_by_label('闪白时长（秒）',exact=True)
    assert duration.input_value()=='0.5'
    duration.fill('0.8')
    assert page.evaluate('current.draft.rows[0].clips[1].transition_duration_ms')==800
    page.get_by_label('进入本段时闪白',exact=True).uncheck()
    assert duration.count()==0
    page.get_by_label('进入本段时闪白',exact=True).check()
    assert duration.input_value()=='0.8'
    assert not page.evaluate('current.draft.rows[0].hook_options.preview_key')
    captions.nth(1).fill('')
    assert page.get_by_label('本段字幕时长（秒）',exact=True).count()==0


def test_composition_effect_controls(page):
    page.evaluate("delete current.draft.rows[0].clips;current.draft.rows[0].composition=blankComposition();current.draft.rows[0].composition.plots.push({episode:'2',start:'00:00',end:'00:02'});renderEditor()")
    page.get_by_label('本段字幕（选填）',exact=True).nth(1).fill('剧情字幕')
    page.get_by_label('进入本段时闪白',exact=True).check()
    assert page.evaluate('current.draft.rows[0].composition.plots[1].segment_caption.text')=='剧情字幕'
    assert page.evaluate('current.draft.rows[0].composition.plots[1].transition_in')=='flash_white'
    page.get_by_label('闪白时长（秒）',exact=True).fill('1.2')
    assert page.evaluate('current.draft.rows[0].composition.plots[1].transition_duration_ms')==1200


def test_text_replace_warns_and_undo_restores_effects(page):
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'1',end_s:'02',segment_caption:{text:'保留',duration_ms:1200}}];renderEditor()")
    parse(page)
    page.get_by_role('button',name='识别并填入',exact=True).click()
    page.wait_for_function('current.draft.rows[0].clips.length===2')
    assert page.evaluate('current.draft.rows[0].clips[0].segment_caption.text')=='保留'
    parse(page,'31')
    page.get_by_label('填入方式',exact=True).select_option('replace')
    messages=[]
    page.on('dialog',lambda dialog:(messages.append(dialog.message),dialog.accept()))
    page.get_by_role('button',name='识别并填入',exact=True).click()
    page.wait_for_function('current.draft.rows[0].clips[0].episode==="31"')
    assert messages and '字幕' in messages[0] and '闪白' in messages[0]
    page.get_by_role('button',name='撤销本次填入',exact=True).click()
    assert page.evaluate('current.draft.rows[0].clips[0].segment_caption')=={'text':'保留','duration_ms':1200}


def test_append_ignores_blank_rows_with_disabled_effects(page):
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),transition_in:'none',segment_caption:null}];renderEditor()")
    parse(page)
    page.get_by_role('button',name='识别并填入',exact=True).click()
    page.wait_for_function('!document.getElementById("clipTextDialog").open')
    assert page.evaluate('current.draft.rows[0].clips.length')==1


def test_effect_fields_read_only_and_three_line_feedback(page):
    captions=page.get_by_label('本段字幕（选填）',exact=True)
    captions.fill('a\nb\nc')
    assert not page.get_by_text('本段字幕最多三行，请减少换行',exact=True).is_visible()
    captions.fill('a\nb\nc\nd')
    assert page.get_by_text('本段字幕最多三行，请减少换行',exact=True).is_visible()
    page.evaluate("current.runtime.status='done';renderEditor()")
    assert page.get_by_label('本段字幕（选填）',exact=True).is_disabled()


def test_caption_duration_clamp_notice_tracks_time_edits(page):
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'1',end_s:'01'}];renderEditor()")
    page.get_by_label('本段字幕（选填）',exact=True).fill('短段')
    assert page.get_by_text('字幕显示到本段结束',exact=False).is_visible()
    page.get_by_role('textbox',name='片段 1 结束 秒',exact=True).fill('03')
    assert not page.get_by_text('字幕显示到本段结束',exact=False).count()
    page.get_by_label('本段字幕时长（秒）',exact=True).fill('4')
    assert page.get_by_text('字幕显示到本段结束',exact=False).is_visible()


def test_remove_first_clears_transition_on_new_first(page):
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'1',end_s:'01'},{...blankClip(),episode:'2',end_s:'02',transition_in:'flash_white',segment_caption:{text:'跟随',duration_ms:1500}}];renderEditor()")
    assert page.locator('[data-remove-clip="0"]').count()==1
    page.locator('[data-remove-clip="0"]').click()
    assert page.evaluate('current.draft.rows[0].clips[0].transition_in')=='none'
    assert page.evaluate('current.draft.rows[0].clips[0].segment_caption.text')=='跟随'
    assert page.get_by_text('已清除新首段的进入闪白',exact=True).is_visible()


def test_remove_middle_preserves_following_flash_with_notice(page):
    page.evaluate("current.draft.rows[0].clips=[{...blankClip(),episode:'1',end_s:'01'},{...blankClip(),episode:'2',end_s:'02'},{...blankClip(),episode:'3',end_s:'03',transition_in:'flash_white'}];renderEditor()")
    page.locator('[data-remove-clip="1"]').click()
    assert page.evaluate('current.draft.rows[0].clips[1].transition_in')=='flash_white'
    assert page.get_by_text('衔接已改为进入新的下一段，请预览核对',exact=True).is_visible()
