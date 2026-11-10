"""Hook receipts are display state; only user edits gate publishing."""
import pytest
from test_source_frame_ui import frame_page

pytestmark = pytest.mark.ui


def ready_project(page):
    page.evaluate('''() => {
      current.media_managed=true;current.media_delivery_enabled=true;
      current.runtime={status:'review',events:[]};
      current.draft.rows[0].hook_text='First Hook';
      current.draft.rows.push({...structuredClone(current.draft.rows[0]),request_id:'R2',seq:2});
      current.assets=current.draft.rows.map(r=>({request_id:r.request_id,status:'qc_passed',
        preview:'/preview',sha256:'a'.repeat(64),version_id:'v1',filename:'fixture.mp4'}));
      renderEditor();renderRuntime();rememberSavedDraft(current);
      window.beforeDraft=JSON.stringify(current.draft);
    }''')
    jobs = [{"id": str(i) * 32, "request_id": f"R{i}", "status": "completed", "input_current": True,
             "result": {"preview_key": str(i) * 64, "image": "data:image/png;base64,",
                        "decision": {"duration_ms": 1000}, "sources_complete": True}}
            for i in (1, 2)]
    page.route('**/hook-previews', lambda route: route.fulfill(json={'jobs': jobs}))
    for job in jobs:
        page.route('**/hook-previews/' + job['id'], lambda route, request, job=job: route.fulfill(json=job))
    return jobs


def test_restored_previews_leave_draft_clean_and_allow_publish(frame_page):
    page = frame_page
    ready_project(page)
    published = []
    response = page.evaluate('current')
    page.route('**/publish', lambda route: (published.append(route.request.post_data_json), route.fulfill(json=response)))
    page.evaluate('restoreHookPreviews(current.id)')
    page.wait_for_function('hookWatchers.size===0')
    assert page.evaluate('dirty') is False
    assert page.evaluate('JSON.stringify(current.draft)===beforeDraft')
    assert page.evaluate('current.revision') == 1
    assert page.locator('.hook-preview-image').count() == 2
    assert page.locator('[data-hook-preview="0"]').is_disabled()
    assert page.locator('[data-hook-preview="1"]').is_disabled()
    page.evaluate("setTab('review')")
    page.locator('[data-reviewed="0"]').check()
    page.locator('[data-publish="0"]').click()
    page.wait_for_function('busy.size===0')
    assert len(published) == 1


def test_late_preview_preserves_real_edits_and_publish_guard(frame_page):
    page = frame_page
    jobs = ready_project(page)
    published = []
    page.route('**/publish', lambda route: (published.append(route.request.post_data_json), route.fulfill(json={})))
    page.evaluate("controls.editingId=current.id;controls();setTab('editor')")
    page.locator('#material-hook-0').fill('Actually edited')
    edited = page.evaluate('JSON.stringify(current.draft)')
    page.evaluate('job=>watchHookJob(current.id,job)', jobs[0])
    assert page.evaluate('dirty') is True
    assert page.evaluate('JSON.stringify(current.draft)') == edited
    assert page.locator('.hook-preview-image').count() == 0
    page.evaluate("setTab('review')")
    page.locator('[data-reviewed="0"]').check()
    page.locator('[data-publish="0"]').click()
    assert '请先保存当前修改，再确认上传' in page.locator('#toast').inner_text()
    assert not published


def test_completed_job_cannot_reenable_readonly_editor(frame_page):
    page = frame_page
    jobs = ready_project(page)
    page.evaluate('job=>watchHookJob(current.id,job)', jobs[0])
    assert page.locator('[data-hook-preview="0"]').is_disabled()


@pytest.mark.parametrize('edit', ['hook', 'clip', 'revert', 'other'])
def test_saved_edit_checks_preview_input_identity(frame_page, edit):
    from copy import deepcopy
    page = frame_page
    job = ready_project(page)[0]
    page.evaluate('current.draft.rows[0].hook_options.manual_ranges=[]')
    held = []
    page.route('**/hook-previews/' + job['id'], lambda route: held.append(route))
    saved = page.evaluate('current')

    def save(route):
        response = deepcopy(saved)
        response['draft'] = route.request.post_data_json['draft']
        response['revision'] += 1
        route.fulfill(json=response)

    page.route('**/save', save)
    page.evaluate("controls.editingId=current.id;controls();setTab('editor')")
    page.evaluate('job=>{watchHookJob(current.id,job)}', job)
    page.wait_for_function('hookWatchers.size===1')
    if edit == 'clip':
        page.locator('[data-row="0"][data-clip="0"][data-clip-part="end_ms"]').fill('500')
    else:
        page.locator('#material-hook-' + ('1' if edit == 'other' else '0')).fill('Edited and saved Hook')
        if edit == 'revert':
            page.locator('#material-hook-0').fill('First Hook')
    page.locator('#saveButton').click()
    page.wait_for_function('!dirty && current.revision===2')
    assert len(held) == 1
    held[0].fulfill(json=job)
    page.wait_for_function('hookWatchers.size===0')
    assert page.locator('#material-hook-0').input_value() == ('Edited and saved Hook' if edit == 'hook' else 'First Hook')
    assert page.locator('.hook-preview-image').count() == (1 if edit == 'other' else 0)


@pytest.mark.parametrize(('part', 'value'), [('episode', '2'), ('start_s', '01'), ('end_ms', '500')])
def test_clip_input_invalidates_existing_preview(frame_page, part, value):
    page = frame_page
    job = ready_project(page)[0]
    page.evaluate("controls.editingId=current.id;controls();setTab('editor')")
    page.evaluate('job=>watchHookJob(current.id,job)', job)
    assert page.locator('.hook-preview-image').count() == 1
    page.locator(f'[data-row="0"][data-clip="0"][data-clip-part="{part}"]').fill(value)
    assert page.locator('.hook-preview-image').count() == 0
    assert '设置已变化，请重新预览' in page.locator('[data-hook-result="0"]').inner_text()
    assert page.evaluate('dirty') is True


def test_newer_hook_task_wins_over_late_older_task(frame_page):
    from copy import deepcopy
    page = frame_page
    old = ready_project(page)[0]
    held = []
    page.route('**/hook-previews/' + old['id'], lambda route: held.append(route))
    page.evaluate('job=>{watchHookJob(current.id,job)}', old)
    page.wait_for_function('hookWatchers.size===1')
    newer = deepcopy(old)
    newer['id'] = '3' * 32
    newer['result']['preview_key'] = '3' * 64
    newer['result']['image'] = 'data:image/png;base64,bmV3'
    page.route('**/hook-previews/' + newer['id'], lambda route: route.fulfill(json=newer))
    page.evaluate('job=>watchHookJob(current.id,job)', newer)
    assert page.locator('.hook-preview-image').get_attribute('src') == newer['result']['image']
    held[0].fulfill(json=old)
    page.wait_for_function('hookWatchers.size===0')
    assert page.locator('.hook-preview-image').get_attribute('src') == newer['result']['image']
