"""A second click cannot start another download while directory selection is pending."""
import pytest
from test_clip_text_ui import page

pytestmark = pytest.mark.ui


@pytest.mark.parametrize('page', ['http://localhost/'], indirect=True)
def test_directory_selection_reserves_download_and_cancel_allows_retry(page):
    page.evaluate('''()=>{
      window.pickers=[];
      window.showDirectoryPicker=()=>new Promise((resolve,reject)=>pickers.push({resolve,reject}));
      api=async()=>({drama_id:'101494',episodes:[{episode:'EP1',bytes:3},{episode:'EP2',bytes:3}]});
      scheduleOriginalsLookup(true);
    }''')
    page.wait_for_function('!document.querySelector("#originalsOpen").disabled')
    page.locator('#originalsOpen').click()
    page.locator('[data-original-download="EP1"]').click()
    page.wait_for_function('pickers.length===1')
    page.locator('[data-original-download="EP2"]').click()
    page.wait_for_function('pickers.length>1||!document.querySelector("#toast").hidden')
    assert page.evaluate('pickers.length') == 1
    page.evaluate("pickers[0].reject(new DOMException('cancelled','AbortError'))")
    page.locator('[data-original-download="EP2"]').click()
    page.wait_for_function('pickers.length===2')
    page.evaluate("pickers[1].reject(new DOMException('cancelled','AbortError'))")
    assert not page.evaluate('originalsDownload.running')
