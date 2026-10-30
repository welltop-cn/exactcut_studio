"""UI regressions use the backend's real validation projection."""
from exactcut.intake import validate_draft
import pytest
from test_source_frame_ui import frame_page


def invalid_editor(page):
    page.evaluate('''() => {
      current.revision=2;
      const good=current.draft.rows[0];
      current.draft.rows=Array.from({length:6},(_,i)=>({...structuredClone(good),request_id:'R'+i,seq:i+1}));
      Object.assign(current.draft.rows[5],{seq:8,clips:[{episode:'',time_format:'source_frames_v1',start_m:'04',start_s:'00',start_f:'000',end_m:'01',end_s:'42',end_f:'016'}]});
    }''')
    draft = page.evaluate('current.draft')
    issues = []
    _, errors = validate_draft(draft, validation_issues=issues)
    page.evaluate('''({errors,issues}) => {current.errors=errors;current.validation_issues=issues;dirty=false;renderEditor();}''',
                  {'errors': errors, 'issues': issues})


def test_highlights_correct_card_and_fields_and_links_to_episode(frame_page):
    page = frame_page
    invalid_editor(page)
    cards = page.locator('#requests .request')
    assert 'validation-error' in cards.nth(5).get_attribute('class')
    assert cards.nth(4).locator('[aria-invalid=true]').count() == 0
    assert cards.nth(5).locator('[aria-invalid=true]').count() == 7
    assert '素材 8 · 片段 1：请填写集号' in page.locator('#validation').inner_text()
    assert '素材 6：' not in page.locator('#validation').inner_text()
    page.get_by_role('button', name='素材 8 · 片段 1：请填写集号', exact=True).click()
    assert page.locator('[data-row="5"][data-clip-part="episode"]').evaluate('(el)=>el===document.activeElement')
    assert '2 处需要修改' in cards.nth(5).inner_text()


def test_correcting_one_field_keeps_other_errors_visible(frame_page):
    page = frame_page
    invalid_editor(page)
    episode = page.locator('[data-row="5"][data-clip-part="episode"]')
    episode.fill('4')
    assert episode.get_attribute('aria-invalid') is None
    assert page.locator('[data-row="5"][data-clip-part="start_m"]').get_attribute('aria-invalid') == 'true'
    assert page.locator('#validation').is_visible()
    page.locator('[data-row="5"][data-clip-part="start_m"]').fill('00')
    assert page.locator('#requests .validation-error').count() == 0
    assert page.locator('#requests [aria-invalid=true]').count() == 0
    assert page.locator('#validation').is_hidden()
    assert '未保存' in page.locator('#saveState').inner_text()


@pytest.mark.parametrize('managed', [False, True])
def test_saving_invalid_draft_focuses_first_error(frame_page, managed):
    page = frame_page
    invalid_editor(page)
    page.evaluate('(managed)=>{current.media_managed=managed;controls.editingId=current.id;controls();}', managed)
    page.evaluate('''() => {api=async()=>structuredClone(current);renderRuntime=()=>{};}''')
    page.locator('#saveButton').click()
    page.wait_for_function('document.activeElement?.dataset.clipPart === "episode"', timeout=5000)
    assert page.evaluate('document.activeElement.dataset.row') == '5'


def test_deleting_previous_material_keeps_highlight_on_material_8(frame_page):
    page = frame_page
    invalid_editor(page)
    page.locator('[data-delete="0"]').click()
    assert page.locator('#requests .request').count() == 5
    assert page.locator('#requests .validation-error').count() == 1
    assert page.locator('#requests .request').nth(4).locator('[aria-invalid=true]').count() == 7
    page.get_by_role('button', name='素材 8 · 片段 1：请填写集号', exact=True).click()
    assert page.evaluate('document.activeElement.dataset.row') == '4'


def test_dismiss_summary_preserves_inline_errors_and_escapes_messages(frame_page):
    page = frame_page
    invalid_editor(page)
    page.evaluate('''() => {current.validation_issues[0].message='<img src=x onerror=alert(1)>';renderValidation();}''')
    assert page.locator('#validation img, .validation-field-message img').count() == 0
    page.get_by_role('button', name='关闭提示', exact=True).click()
    assert page.locator('#validation').is_hidden()
    assert page.locator('#requests .validation-error').count() == 1
    assert page.locator('#requests [aria-invalid=true]').count() == 7
