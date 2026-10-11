import re
from pathlib import Path

import pytest


def test_long_sidebar_scroll_stays_in_list_and_right_content_can_scroll():
    playwright = pytest.importorskip('playwright.sync_api')
    html = (Path(__file__).parents[1] / 'exactcut/web/index.html').read_text()
    html = re.sub(r'<script src="[^"]+"></script>', '', html)

    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except playwright.Error:
            pytest.skip('Chromium unavailable')
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 720})
            page.set_content(html)
            page.locator('#batchList').evaluate("el => el.innerHTML = '<div class=\"batch-card\"><button class=\"batch\">项目</button></div>'.repeat(60)")
            page.locator('main').evaluate("el => el.innerHTML = '<div style=\"height:200px\">右侧内容</div>'")

            assert page.evaluate('document.documentElement.scrollHeight <= innerHeight')
            box = page.locator('#batchList').bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(100)
            assert page.locator('#batchList').evaluate('el => el.scrollTop') > 0
            assert page.evaluate('scrollY') == 0

            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(100)
            assert page.evaluate('scrollY') == 0

            page.locator('main').evaluate("el => el.innerHTML = '<div style=\"height:1600px\">右侧长内容</div>'")
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(100)
            assert page.evaluate('scrollY') == 0

            page.mouse.move(800, 300)
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(100)
            assert page.evaluate('scrollY') > 0
        finally:
            browser.close()
