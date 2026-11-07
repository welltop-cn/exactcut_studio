"""Playhead scrubbing shows the video's own frame and spends no server frame job."""
import pytest

from test_mosaic_ui import editor, open_auto, scan_result

pytestmark = pytest.mark.ui


def stub_seekable_video(page, *, complete=True, snap_to_frame=False):
    """Give the stage the decoded, pausable video the editor seeks on scrub.

    snap_to_frame reproduces browsers that report the frame's own start time
    instead of the requested time.
    """
    page.evaluate('''({complete,snap}) => {
      const video=$('mosaicSource'),state={time:0,seeking:false};
      const fps=session.metadata.fps_num/session.metadata.fps_den;
      Object.defineProperty(video,'duration',{get:()=>10,configurable:true});
      Object.defineProperty(video,'readyState',{get:()=>4,configurable:true});
      // A healthy source: the fixture's placeholder never loads real media.
      Object.defineProperty(video,'error',{get:()=>null,configurable:true});
      Object.defineProperty(video,'seeking',{get:()=>state.seeking,configurable:true});
      Object.defineProperty(video,'currentTime',{configurable:true,get:()=>state.time,set:value=>{
        state.seeking=true;state.time=snap?Math.floor(value*fps)/fps:value;
        if(complete){state.seeking=false;video.dispatchEvent(new Event('seeked'));}
      }});
      window.videoState=state;
    }''', {'complete': complete, 'snap': snap_to_frame})


def count_frame_requests(page):
    page.evaluate('''() => {
      window.realFrame=EditorBusy.frame;window.frameCalls=[];window.pendingFrames=[];
      EditorBusy.frame=(...args)=>{
        if(window.holdFrames)return new Promise((resolve,reject)=>pendingFrames.push({resolve,reject}));
        frameCalls.push(1);return realFrame(...args);
      };
      window.holdFrames=false;
    }''')


def hold_frame_requests(page):
    page.evaluate('() => {window.holdFrames=true;}')


def release_frame_requests(page):
    page.evaluate('() => {window.holdFrames=false;}')


def open_event(page):
    scan_result(page)
    open_auto(page)
    page.locator('#mosaicScan').click()
    page.locator('[data-scan-event="e1"]').click()
    page.locator('#mosaicOverlay').wait_for(state='visible')
    if not page.locator('#mosaicNext').is_visible():
        page.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')


def test_scrubbing_shows_the_video_frame_without_spending_a_server_frame(editor):
    open_event(editor)
    stub_seekable_video(editor)
    count_frame_requests(editor)
    hold_frame_requests(editor)
    editor.locator('#mosaicNext').click()
    editor.wait_for_timeout(300)
    assert editor.evaluate('pendingFrames.length') == 0, 'scrubbing must not queue a server frame'
    assert editor.locator('#mosaicOverlay').is_visible()
    hint = editor.locator('#mosaicFrameHint').inner_text()
    assert '正在读取' not in hint, hint
    assert editor.locator('#mosaicDraw').is_enabled()
    assert editor.locator('#mosaicConfirm').is_enabled()
    release_frame_requests(editor)


def test_playhead_preview_survives_a_browser_that_reports_the_frame_start(editor):
    open_event(editor)
    stub_seekable_video(editor, snap_to_frame=True)
    count_frame_requests(editor)
    hold_frame_requests(editor)
    # 30 fps: (123/30)*30 floors to 122 in floating point, the common browser rounding trap.
    editor.evaluate("() => {const s=$('mosaicSeek');s.value=123;s.dispatchEvent(new Event('input'));}")
    editor.wait_for_timeout(300)
    assert editor.evaluate('videoState.time') == 123/30
    assert editor.locator('#mosaicOverlay').is_visible(), 'frame-start timestamps must still count as the wanted frame'
    assert editor.evaluate('pendingFrames.length') == 0
    release_frame_requests(editor)


def test_scrubbing_after_the_startup_calibration_requests_nothing(editor):
    scan_result(editor)
    count_frame_requests(editor)
    open_auto(editor)
    editor.locator('#mosaicFrame').wait_for(state='visible', timeout=5000)
    startup = editor.evaluate('frameCalls.length')
    assert startup == 1, 'opening the editor calibrates with exactly one server frame'
    stub_seekable_video(editor)
    for _ in range(3):
        editor.locator('#mosaicNext').click()
        editor.wait_for_timeout(250)
    assert editor.evaluate('frameCalls.length') == startup
    assert editor.locator('#mosaicFrame').is_hidden(), 'the video frame replaced the calibrated image'


def test_missing_video_frame_falls_back_to_the_server_frame(editor):
    scan_result(editor)
    count_frame_requests(editor)
    open_auto(editor)
    editor.locator('#mosaicFrame').wait_for(state='visible', timeout=5000)
    baseline = editor.evaluate('frameCalls.length')
    # No playable video in this fixture: the exact-frame path is the only preview.
    editor.locator('#mosaicNext').click()
    editor.wait_for_function(f'frameCalls.length>{baseline}')
    assert editor.locator('#mosaicFrame').is_visible()


def test_unfinished_video_seek_waits_before_asking_the_server(editor):
    open_event(editor)
    stub_seekable_video(editor, complete=False)
    count_frame_requests(editor)
    hold_frame_requests(editor)
    editor.locator('#mosaicNext').click()
    editor.wait_for_timeout(300)
    assert editor.evaluate('videoState.seeking') is True
    assert editor.locator('#mosaicOverlay').is_hidden()
    assert editor.locator('#mosaicDraw').is_disabled()
    assert editor.evaluate('pendingFrames.length') == 0, 'a seek in flight is waited out, not answered by ffmpeg'
    editor.wait_for_function('pendingFrames.length===1', timeout=5000)
    release_frame_requests(editor)


def test_dragging_before_the_startup_frame_landed_still_reads_scan_state(editor):
    scan_result(editor)
    count_frame_requests(editor)
    hold_frame_requests(editor)
    open_auto(editor)
    stub_seekable_video(editor)
    if not editor.locator('#mosaicNext').is_visible():
        editor.locator('#mosaicAdjustToggle').evaluate('(el)=>el.click()')
    editor.locator('#mosaicNext').click()
    editor.wait_for_function("calls.some(c=>c.url.endsWith('/scan')&&!c.body)")
    assert editor.evaluate('pendingFrames.length') == 1, 'only the startup calibration was requested'
    release_frame_requests(editor)


def test_retry_button_disappears_once_the_video_shows_the_frame(editor):
    open_event(editor)
    stub_seekable_video(editor)
    editor.evaluate("$('mosaicFrameRetry').hidden=false")
    editor.locator('#mosaicNext').click()
    editor.wait_for_timeout(300)
    assert editor.locator('#mosaicFrameRetry').is_hidden()
