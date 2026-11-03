import re
from pathlib import Path

import pytest
from browser_support import shared_playwright

pytestmark = pytest.mark.ui


def test_long_sidebar_scroll_stays_in_list_and_right_content_can_scroll():
    playwright = pytest.importorskip('playwright.sync_api')
    html = (Path(__file__).parents[1] / 'exactcut/web/index.html').read_text()
    html = re.sub(r'<script src="[^"]+"></script>', '', html)

    with shared_playwright() as p:
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


def test_sidebar_items_are_compact_without_hiding_details_or_actions():
    playwright = pytest.importorskip('playwright.sync_api')
    html = (Path(__file__).parents[1] / 'exactcut/web/index.html').read_text()
    html = re.sub(r'<script src="[^"]+"></script>', '', html)
    cards = '''
    <div class="batch-card">
      <button class="batch selected" data-batch="one">
        <strong>101494 · fastqa200539 并发验收 4</strong>
        <small>hookqa212220b</small>
        <small><span class="batch-meta"><span>2 条素材</span><span>2026/10/11</span></span></small>
        <span class="badge good">待预览</span>
      </button>
      <div class="batch-actions"><button class="secondary">归档</button></div>
    </div>
    <div class="batch-card">
      <button class="batch" data-batch="two">
        <strong>101494 · fastqa200539 并发验收 3</strong>
        <small>hookqa212220a</small>
        <small><span class="batch-meta"><span>2 条素材</span><span>2026/10/11</span></span></small>
        <span class="badge good">待预览</span>
      </button>
      <div class="batch-actions"><button class="secondary">复原</button><button class="secondary danger">删除</button></div>
    </div>'''

    with shared_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except playwright.Error:
            pytest.skip('Chromium unavailable')
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 720})
            page.set_content(html)
            page.locator('#batchList').evaluate('(el, cards) => el.innerHTML = cards', cards)
            assert page.locator('.batch-card').first.bounding_box()['height'] <= 150
            for width in (1280, 900, 390):
                page.set_viewport_size({'width': width, 'height': 720})
                for card in page.locator('.batch-card').all():
                    assert card.locator('strong').is_visible()
                    assert card.locator('small').count() == 2
                    assert card.locator('.badge').is_visible()
                    badge = card.locator('.badge').bounding_box()
                    actions = card.locator('.batch-actions').bounding_box()
                    assert badge['x'] + badge['width'] <= actions['x'] or badge['y'] + badge['height'] <= actions['y']
        finally:
            browser.close()
