"""暂停执行 is hidden while cut/mosaic runs; control toasts prefer the server message."""
import pytest
from test_source_frame_ui import frame_page

pytestmark = pytest.mark.ui


@pytest.mark.parametrize('runtime,visible', [
    ({'status': 'review', 'cutting_active': True}, False),
    ({'status': 'producing', 'cutting_active': True}, False),
    ({'status': 'producing'}, True),
    ({'status': 'review'}, False),
])
def test_pause_button_visibility(frame_page, runtime, visible):
    page = frame_page
    page.evaluate('r=>{current.runtime={events:[],...r};controls()}', runtime)
    assert page.locator('#pauseButton').is_visible() is visible


@pytest.mark.parametrize('events,expected', [
    ([{'at': '1', 'message': '已请求暂停：制作进度保留'}],
     '已请求暂停：制作进度保留'),
    ([{'at': '1', 'message': '制作完成'}], '已请求暂停'),
    ([], '已请求暂停'),
])
def test_pause_toast_uses_server_message(frame_page, events, expected):
    page = frame_page
    page.evaluate('''events=>{current.runtime={status:'producing',events:[]};controls();
      window.api=async url=>url.endsWith('/pause')?{...current,runtime:{status:'paused',paused:true,events}}:url.endsWith('/sidebar')?{batches:[]}:{}}''', events)
    page.locator('#pauseButton').click()
    page.wait_for_function('t=>document.getElementById("toast").textContent===t', arg=expected)


ARCHIVED = '已归档：制作任务暂停并保留进度；剪切/打码任务已取消，不保留进度'


@pytest.mark.parametrize('button', ['#archiveButton', '#batchList [data-batch-action="archive"]'])
@pytest.mark.parametrize('events,expected', [([{'at': '1', 'message': ARCHIVED}], ARCHIVED), ([], None)])
def test_archive_toast_uses_server_message(frame_page, button, events, expected):
    page = frame_page
    page.evaluate('''events=>{current.runtime={status:'producing',events:[]};controls();
      renderBatches([{id:current.id,count:1,created_at:'2026-11-03',status:'producing'}]);
      window.api=async url=>url.endsWith('/archive')?{...current,runtime:{status:'paused',paused:true,archived:true,events}}:url.endsWith('/sidebar')?{batches:[]}:{}}''', events)
    page.locator(button).dispatch_event('click')
    page.wait_for_function('t=>t?document.getElementById("toast").textContent===t:document.getElementById("toast").textContent.startsWith("项目已归档")||document.getElementById("toast").textContent.startsWith("已归档，")', arg=expected)
