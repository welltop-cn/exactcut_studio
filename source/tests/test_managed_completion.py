"""Completion follows current, non-deleted material versions."""
import copy
import re
from pathlib import Path
import pytest
from exactcut import workflow as w
from test_workflow import workspace
from test_material_versions import enrolled, token, enable_fixture_delivery, candidate_job, rendered
from browser_support import shared_playwright


def delivered():
    m, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    vid = state['assets'][rid]['current_version']
    state = m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(),
        'request_id': rid, 'version_id': vid, 'sha256': state['versions'][vid]['record']['output_sha256']})
    next(iter(state['deliveries'].values())).update(status='fbbm_ready', publication={'fbbm': {'video_id': 'fixture-video'}})
    m._write(path, state)
    return m, path, state


def assert_status(m, path, expected):
    detail = w.detail(path.name)
    assert detail['runtime']['status'] == expected
    assert detail['runtime']['underlying_status'] == expected
    assert next(item for item in w.summaries(compact=True) if item['id'] == path.name)['status'] == expected
    return detail


@pytest.mark.integration
def test_all_current_versions_delivered_complete_detail_and_sidebar(workspace):
    m, path, _ = delivered()
    detail = assert_status(m, path, 'complete')
    assert detail['runtime']['message'] == '全部素材已入库，可在 FBBM 使用'
    assert detail['assets'][0]['video_id'] == 'fixture-video'


@pytest.mark.integration
@pytest.mark.parametrize('delivery_status,expected', [
    ('pending', 'publishing'), ('upload_queued', 'publishing'), ('uploading', 'publishing'),
    ('fbbm_queued', 'publishing'), ('publish_failed', 'publish_failed'), ('unverified', 'unverified')])
def test_incomplete_delivery_keeps_existing_status(workspace, delivery_status, expected):
    m, path, state = delivered()
    next(iter(state['deliveries'].values()))['status'] = delivery_status
    m._write(path, state)
    assert_status(m, path, expected)


@pytest.mark.integration
@pytest.mark.parametrize('candidate_status,expected', [(None, 'review'), ('queued', 'queued'),
    ('processing', 'producing'), ('failed', 'partial'), ('promoted', 'review')])
def test_old_delivery_does_not_complete_new_work(workspace, candidate_status, expected):
    m, path, state = delivered()
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] += ' changed'
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    if candidate_status:
        state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
        if candidate_status == 'promoted':
            worker, child, event, job = candidate_job(path, state)
            worker.stage_enter(job); rendered(child, event); worker.stage_finish(job, 0)
        else:
            vid = next(iter(state['assets'].values()))['candidate_version']
            state['versions'][vid]['status'] = candidate_status
            m._write(path, state)
    detail = assert_status(m, path, expected)
    if candidate_status is None:
        assert '待确认制作' in detail['runtime']['message']


@pytest.mark.integration
def test_partial_delivery_deleted_material_and_empty_project(workspace):
    m, path, state = delivered()
    draft = copy.deepcopy(state['draft'])
    extra = copy.deepcopy(draft['rows'][0]); extra.pop('request_id'); extra['seq'] = 2
    draft['rows'].append(extra)
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert_status(m, path, 'review')
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job); rendered(child, event); worker.stage_finish(job, 0)
    assert_status(m, path, 'review')
    state = m.read(path)
    rid = event['asset_id']; vid = state['assets'][rid]['current_version']
    state = m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(),
        'request_id': rid, 'version_id': vid, 'sha256': state['versions'][vid]['record']['output_sha256']})
    state['deliveries'][next(reversed(state['deliveries']))].update(
        status='fbbm_ready', publication={'fbbm': {'video_id': 'fixture-video-2'}})
    m._write(path, state)
    assert_status(m, path, 'complete')
    draft = copy.deepcopy(state['draft']); draft['rows'] = draft['rows'][:1]
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert_status(m, path, 'complete')
    draft['rows'] = []
    m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert_status(m, path, 'draft')


@pytest.mark.ui
def test_review_checkbox_tracks_delivery_without_rebuilding_card():
    playwright = pytest.importorskip('playwright.sync_api')
    web = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (web / 'index.html').read_text())
    source = (web / 'app.js').read_text()
    render = source[source.index('function renderAssets()'):source.index('async function refreshBatches()')]
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            page.evaluate("""() => {
                window.$=id=>document.getElementById(id);
                document.body.appendChild($('assets'));
                window.current={id:'fixture',assets:[{request_id:'R1',title:'Fixture',filename:'fixture.mp4',
                    preview:'/fixture',status:'qc_passed'}],runtime:{status:'review'}};
                window.assetKeys=''; window.busy=new Set(); window.activeStatuses=[];
                window.stopPreviewLoads=window.preparePreviewLoads=window.setErrorHtml=()=>{};
                window.esc=window.appUrl=window.statusLabel=s=>s; window.badgeClass=()=>'';
            }""")
            page.add_script_tag(content=render)
            page.evaluate('renderAssets()')
            assert page.locator('.approve').is_visible()
            page.locator('[data-reviewed]').check()
            page.evaluate("current.assets[0].status='fbbm_ready';renderAssets()")
            assert not page.locator('.approve').is_visible()
            assert page.locator('[data-publish]').inner_text() == '已入库'
            assert page.locator('[data-publish]').is_disabled()
            page.evaluate("current.assets[0].status='qc_passed';renderAssets()")
            assert page.locator('.approve').is_visible()
            assert not page.locator('[data-reviewed]').is_checked()
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.parametrize('media', [False, True])
@pytest.mark.parametrize('control', ['active', 'paused', 'archived', 'deleted'])
def test_lightweight_sidebar_matches_detail_snapshot(workspace, media, control):
    m, path, state = enrolled(media=media)
    if control == 'deleted':
        state['deleted_at'] = '2026-10-21T00:00:00Z'
    elif control != 'active':
        state['control'][control] = True
    m._write(path, state)
    full = m.detail(path)
    light = m.sidebar(path)
    for key in ('id', 'filename', 'display_name', 'created_at', 'draft', 'revision', 'media_managed', 'deleted_at'):
        assert light.get(key) == full.get(key)
    assert light['runtime'] == {key: full['runtime'][key] for key in ('status', 'archived')}


@pytest.mark.parametrize('candidate_status,expected', [('queued', 'queued'), ('processing', 'producing'), ('failed', 'partial')])
@pytest.mark.parametrize('paused', [False, True])
def test_sidebar_candidate_without_current_media(workspace, candidate_status, expected, paused):
    m, path, state = enrolled()
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    vid = next(iter(state['assets'].values()))['candidate_version']
    state['versions'][vid]['status'] = candidate_status
    state['control']['paused'] = paused
    m._write(path, state)
    if paused and expected in w.ACTIVE:
        expected = 'paused'
    assert m.detail(path)['runtime']['status'] == expected
    assert m.sidebar(path)['runtime']['status'] == expected
