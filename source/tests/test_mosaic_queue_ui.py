"""Browser lifecycle contracts; real Chromium, deterministic HTTP adapter fixture."""
from test_mosaic_ui import editor, open_auto


def test_queued_restore_uses_server_parameters_before_enabling_controls(editor):
    editor.evaluate('''() => {
      const old=api;window.scanWrites=0;
      localStorage.setItem('exactcut.mosaic.workbench.v3.fixture.batch.session.'+'a'.repeat(64)+'.','invalid local draft');
      window.api=async(url,body)=>{
        if(url.endsWith('/render'))return {job:{id:'b'.repeat(32),queued_task:true,status:'queued',stage:'queued',progress:'等待后台处理',targets:[],ranges:[[3,9]],effect:{style:'solid'}}};
        if(url.includes('/render/'))return new Promise(()=>{});
        if(url.endsWith('/scan')&&body)scanWrites++;
        return old(url,body);
      };
    }''')
    open_auto(editor)
    assert editor.locator('#mosaicProcessingText').inner_text()=='等待后台处理'
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.locator('#mosaicStop').is_enabled()
    assert editor.locator('#mosaicClose').is_enabled()
    assert editor.evaluate('scanWrites')==0
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.evaluate('scanWrites')==0


def test_poll_disconnect_keeps_lock_and_recovers_to_pending_decision(editor):
    editor.evaluate('''() => {
      const old=api;window.reads=0;window.writes=0;
      window.job={id:'b'.repeat(32),queued_task:true,status:'running',targets:[],ranges:[[3,9]],effect:{style:'pixel'},result:{}};
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){if(body)writes++;return {job};}
        if(url.includes('/render/')){
          reads++;
          if(reads===1)throw Error('offline');
          if(reads===2)return new Promise(resolve=>window.reconnect=()=>resolve({...job,status:'completed'}));
          return {...job,status:'completed'};
        }
        return old(url,body);
      };
    }''')
    open_auto(editor)
    editor.wait_for_function('reads>=1')
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.locator('#mosaicStop').is_visible()
    editor.wait_for_function('!!window.reconnect')
    editor.evaluate('reconnect()')
    editor.locator('#mosaicBatchDialog').wait_for()
    assert editor.locator('#mosaicReplace').is_enabled()
    editor.locator('#mosaicBatchClose').click()
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.locator('#mosaicBatchStart').is_hidden()
    assert editor.locator('#mosaicDiscard').is_visible()
    assert editor.evaluate('writes')==0


def test_cancelled_snapshot_does_not_overwrite_later_local_adjustments(editor):
    editor.evaluate('''() => {
      const old=api;
      window.api=async(url,body)=>url.endsWith('/render')?{job:{id:'b'.repeat(32),queued_task:true,status:'cancelled',targets:[],ranges:[[3,9]],effect:{style:'pixel'}}}:old(url,body);
    }''')
    open_auto(editor)
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('[data-event-delete]').click()
    assert '删除 0 段' in editor.locator('#mosaicBatchHint').inner_text()
    editor.locator('#mosaicClose').click()
    open_auto(editor)
    assert '删除 0 段' in editor.locator('#mosaicBatchHint').inner_text()
