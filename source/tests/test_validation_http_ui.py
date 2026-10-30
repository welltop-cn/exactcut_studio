"""Exercise real authenticated browser saves against an isolated HTTP server."""
from copy import deepcopy
import json

import pytest

from exactcut import material_versions, source_frames, workflow as w
from test_source_frames import source
from test_web_base_path import app, request


@pytest.mark.parametrize('managed', [False, True])
def test_real_browser_invalid_save_correction_and_reload(source, managed):
    playwright = pytest.importorskip('playwright.sync_api')
    server, item, headers, _ = source
    data = deepcopy(item['draft'])
    data.update(drama_id='101573', author='测试', editor_time_format='source_frames_v1')
    rows = []
    for seq in [1, 2, 3, 4, 5, 8]:
        row = deepcopy(data['rows'][0])
        row.update(request_id=f'R{seq}', seq=seq, hook_text='', hook_options={'mode': 'none'},
                   clips=[{'episode': '1', 'time_format': 'source_frames_v1',
                           'start_m': '00', 'start_s': '00', 'start_f': '000',
                           'end_m': '00', 'end_s': '01', 'end_f': '012'}])
        rows.append(row)
    rows[-1]['clips'][0].update(episode='', start_m='04')
    data['rows'] = rows
    path = w.folder(item['id'])
    source_frames.prepare(path, data, 'EP1')
    w.save_draft(item['id'], {'revision': item['revision'], 'draft': data})
    if managed:
        material_versions.enroll(path)
    base = f'http://127.0.0.1:{server.server_address[1]}/home/'
    endpoint = f'/home/api/batches/{item["id"]}'
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        response = context.request.post(base + 'api/login',
            data={'username': 'admin', 'password': server.password},
            headers={'X-Exactcut-Request': '1'})
        assert response.ok
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '#' + item['id'])
        page.wait_for_selector('#requests .validation-error')
        assert page.locator('#requests .request').count() == 6
        page.locator('#saveButton').click()
        page.wait_for_function('document.activeElement?.dataset.clipPart === "episode"', timeout=5000)
        assert page.evaluate('document.activeElement.dataset.row') == '5'
        status, _, body = request(server, 'GET', endpoint, headers=headers)
        assert status == 200
        saved = json.loads(body)
        assert len(saved['errors']) == 2
        assert saved['validation_issues'][0]['text'] == '素材 8 · 片段 1：请填写集号'
        page.locator('[data-row="5"][data-clip-part="episode"]').fill('1')
        assert page.locator('#requests [aria-invalid=true]').count() == 6
        page.locator('[data-row="5"][data-clip-part="start_m"]').fill('00')
        assert page.locator('#requests .validation-error').count() == 0
        page.locator('#saveButton').click()
        page.wait_for_function('!dirty && current.errors.length===0', timeout=5000)
        assert page.locator('#validation').is_hidden()
        status, _, body = request(server, 'GET', endpoint, headers=headers)
        assert status == 200
        saved = json.loads(body)
        assert saved['errors'] == saved['validation_issues'] == []
        assert saved['draft']['rows'][5]['seq'] in [8, '8']
        assert saved['draft']['rows'][5]['clips'][0]['time_format'] == 'source_frames_v1'
        assert 'start_ms' not in saved['draft']['rows'][5]['clips'][0]
        page.reload()
        page.wait_for_selector('#requests .request')
        assert page.locator('#requests .validation-error').count() == 0
        assert page.locator('#confirmButton').is_enabled()
        assert not errors, errors
        browser.close()
