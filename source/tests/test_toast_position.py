import re
from pathlib import Path

import pytest


def test_save_toast_stays_clear_of_editor_actions_on_desktop_and_mobile():
    playwright = pytest.importorskip('playwright.sync_api')
    web = Path(__file__).parents[1] / 'exactcut/web'
    html = re.sub(r'<script src="[^"]+"></script>', '', (web / 'index.html').read_text())
    script = (web / 'app.js').read_text()
    position = next((line for line in script.splitlines() if line.startswith('function positionToast(')), '')
    toast = next(line for line in script.splitlines() if line.startswith('function toast('))

    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except playwright.Error:
            pytest.skip('Chromium unavailable')
        try:
            for width in (1280, 390):
                page = browser.new_page(viewport={'width': width, 'height': 720})
                page.set_content(html)
                page.locator('#emptyWorkspace').evaluate('el => el.hidden = true')
                page.locator('#workspace').evaluate('el => el.hidden = false')
                page.locator('#requests').evaluate("el => el.innerHTML = '<div style=\"height:1200px\"></div>'")
                page.locator('#saveButton').scroll_into_view_if_needed()
                page.evaluate("source => { const $ = id => document.getElementById(id); let toastTimer; eval(source + '\\ntoast(\\\"已保存，校验通过\\\");'); }", position + '\n' + toast)

                notice = page.locator('#toast').bounding_box()
                toolbar = page.locator('#editorToolbar').bounding_box()
                assert notice['y'] + notice['height'] <= toolbar['y'] - 8

                page.evaluate("source => { const $ = id => document.getElementById(id); let toastTimer; eval(source + '\\ntoast(' + JSON.stringify('校验错误\\n'.repeat(40)) + ', true);'); }", position + '\n' + toast)
                notice = page.locator('#toast').bounding_box()
                assert notice['y'] >= 12
                assert notice['y'] + notice['height'] <= toolbar['y'] - 8
                assert page.locator('#toast').evaluate('el => getComputedStyle(el).overflowY') == 'auto'
                page.close()
        finally:
            browser.close()
