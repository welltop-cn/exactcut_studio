"""Ending errors explain the required action and focus its actual control."""
from test_source_frame_ui import frame_page
from test_validation_feedback_ui import revalidate_editor


def missing_image(page):
    page.evaluate('''()=>{current.revision=2;current.draft.rows[0].seq=2;current.draft.rows[0].ending={mode:'image',image:'',duration_ms:3000};}''')
    revalidate_editor(page)


def test_missing_ending_image_links_to_upload_and_can_be_cancelled(frame_page):
    page = frame_page
    missing_image(page)
    text = '素材 2 · 结尾画面：请上传结尾图片，或将结尾画面改为“不添加”'
    assert text in page.locator('#validation').inner_text()
    field = page.locator('[data-ending-file]')
    assert field.get_attribute('aria-invalid') == 'true'
    assert '请上传结尾图片' in page.locator('.ending-settings .validation-field-message').inner_text()
    page.get_by_role('button', name=text, exact=True).click()
    assert field.evaluate('(el)=>el===document.activeElement')
    page.locator('[data-ending-field="mode"]').select_option('none')
    assert page.locator('#validation').is_hidden()
    assert page.locator('#requests .validation-error').count() == 0
    page.locator('[data-ending-field="mode"]').select_option('image')
    assert page.locator('[data-ending-file]').get_attribute('aria-invalid') == 'true'
    assert text in page.locator('#validation').inner_text()


def test_ending_duration_error_stays_located_after_mode_change(frame_page):
    page = frame_page
    page.evaluate('''()=>{current.revision=2;current.draft.rows[0].ending={mode:'last_frame',duration_ms:500};}''')
    revalidate_editor(page)
    assert page.locator('[data-ending-field="duration_ms"]').get_attribute('aria-invalid') == 'true'
    page.locator('[data-ending-field="mode"]').select_option('image')
    assert page.locator('[data-ending-field="duration_ms"]').get_attribute('aria-invalid') == 'true'
    page.locator('[data-ending-field="duration_ms"]').fill('3')
    assert page.locator('[data-ending-field="duration_ms"]').get_attribute('aria-invalid') is None
