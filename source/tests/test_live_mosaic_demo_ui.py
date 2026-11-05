"""Public UI checks for the isolated live mosaic experiment."""
from pathlib import Path

import pytest
from browser_support import shared_playwright

pytestmark = pytest.mark.ui
DEMO = Path(__file__).parents[1] / 'devtools/live_mosaic/index.html'


def test_live_effect_changes_pixels_without_render_request():
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(DEMO.as_uri())
        page.locator('#synthetic').click()
        page.wait_for_function("document.querySelector('#stats').dataset.frames > 0")
        # The source is colored test bars. A black region must replace its center.
        page.locator('#style').select_option('solid')
        page.locator('#strength').fill('50')
        page.locator('#strength').dispatch_event('input')
        assert page.locator('#output').evaluate("c => Array.from(c.getContext('2d').getImageData(480,270,1,1).data)") == [0, 0, 0, 255]
        page.locator('#enabled').uncheck()
        assert page.locator('#output').evaluate("c => Array.from(c.getContext('2d').getImageData(480,270,1,1).data)") != [0, 0, 0, 255]
        assert page.locator('#stats').get_attribute('data-errors') == '0'


def test_timeline_steps_frames_and_trims_mosaic_span():
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':1000})
        page.set_default_timeout(2000)
        page.goto(DEMO.as_uri())
        page.locator('#synthetic').click()
        page.locator('#nextFrame').click()
        page.locator('#nextFrame').click()
        assert page.locator('#frameNumber').input_value() == '2'
        page.locator('#prevFrame').click()
        assert page.locator('#frameNumber').input_value() == '1'
        page.locator('#nextFrame').focus()
        page.keyboard.press('ArrowRight')
        assert page.locator('#frameNumber').input_value() == '2'
        page.locator('#zoom').fill('32')
        page.locator('#zoom').dispatch_event('input')
        assert page.locator('#timelineContent').evaluate('(e)=>e.offsetWidth') == 9600
        # Scroll to the beginning after zooming around the viewport center.
        page.locator('#timelineScroll').evaluate('(e)=>e.scrollLeft=0')
        # Two frames at this zoom correspond to 64px. Drag the start handle.
        handle = page.locator('#rangeStart').bounding_box()
        page.mouse.move(handle['x']+handle['width']/2, handle['y']+10)
        page.mouse.down()
        page.mouse.move(handle['x']+handle['width']/2+64, handle['y']+10)
        page.mouse.up()
        assert float(page.locator('#start').input_value()) == pytest.approx(2/30,abs=.0001)
        # Range end is exclusive: frame immediately before it is the last affected frame.
        page.locator('#frameNumber').fill('299')
        page.locator('#frameNumber').dispatch_event('change')
        page.locator('#nextFrame').click()
        assert page.locator('#frameNumber').input_value() == '299'
        assert page.locator('#stats').get_attribute('data-errors') == '0'
        browser.close()
