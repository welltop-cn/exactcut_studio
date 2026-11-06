"""Existing previews do not hide ongoing cut/mosaic work on the project page."""
import pytest
from test_source_frame_ui import frame_page

pytestmark = pytest.mark.ui


@pytest.mark.parametrize('message', ['剪切/打码排队中', '剪切/打码处理中', None])
def test_project_cutting_status_and_completion(frame_page, message):
    page = frame_page
    page.evaluate('''message=>{
      current.assets=[{request_id:'R1',status:'qc_passed',preview:'/preview'}];
      current.runtime={status:'review',events:[],download_complete:true,
        message:'成片已就绪，可播放查看',cutting_active:true,cutting_message:message};
      renderRuntime();
    }''', message)
    expected = message or '剪切/打码处理中'
    assert page.locator('#batchStatus').inner_text() == '剪切/打码中'
    assert page.locator('#liveMessage').inner_text() == expected
    page.locator('[data-tab="progress"]').click()
    assert page.locator('#progressMessage').is_visible()
    assert page.locator('#progressMessage').inner_text() == expected
    assert '成片已就绪，可前往预览' not in page.locator('#progressTab').inner_text()
    assert not page.locator('#pauseButton').is_visible()
    assert page.evaluate('current.assets[0].preview') == '/preview'

    page.evaluate('''() => {
      current.runtime.cutting_active=false;current.runtime.cutting_message='';renderRuntime();
    }''')
    assert page.locator('#liveMessage').inner_text() == '成片已就绪，可播放查看'
    assert page.locator('#batchStatus').inner_text() == '待预览'
    assert not page.locator('#progressMessage').is_visible()


def test_cutting_uses_fast_polling_until_completion(frame_page):
    result = frame_page.evaluate('''() => {
      let calls=0;poll=()=>calls++;
      current.runtime={status:'review',cutting_active:true};batchCache=[];
      scheduledPoll.lastAttempt=Date.now()-4000;scheduledPoll();
      const active=calls;
      current.runtime.cutting_active=false;
      scheduledPoll.lastAttempt=Date.now()-4000;scheduledPoll();
      const idle=calls;
      scheduledPoll.lastAttempt=Date.now()-16000;scheduledPoll();
      return {active,idle,afterIdleInterval:calls};
    }''')
    assert result == {'active': 1, 'idle': 1, 'afterIdleInterval': 2}
