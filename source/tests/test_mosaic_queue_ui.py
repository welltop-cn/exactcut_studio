"""Browser lifecycle contracts; real Chromium, deterministic HTTP adapter fixture."""
from test_mosaic_ui import editor, open_auto
import pytest

pytestmark = pytest.mark.ui


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


@pytest.mark.parametrize('previous',['cancelled','failed','discarded'])
@pytest.mark.parametrize('http_status',[None,409,503])
def test_rejected_submission_keeps_new_edits_and_displays_error(editor,previous,http_status):
    editor.evaluate('''({previous,httpStatus}) => {
      const old=api;window.posts=0;window.latestReads=0;
      window.job={id:'b'.repeat(32),queued_task:true,status:previous==='discarded'?'completed':previous,
        discarded_at:previous==='discarded'?'earlier':undefined,targets:[],ranges:[[3,9],[12,18]],effect:{style:'pixel'},result:{}};
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){
          if(body){posts++;throw Object.assign(Error('系统正在维护，提交未受理'),httpStatus===null?{}:{status:httpStatus});}
          latestReads++;return {job};
        }
        return old(url,body);
      };
    }''',{'previous':previous,'httpStatus':http_status})
    open_auto(editor)
    editor.locator('[data-event-delete]').first.click()
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    reads_before_submit=editor.evaluate('latestReads')
    editor.locator('#mosaicBatchStart').click()
    editor.wait_for_function("document.getElementById('mosaicStatus').textContent.includes('提交未受理')")
    editor.wait_for_timeout(1500)
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert '提交未受理' in editor.locator('#mosaicStatus').inner_text()
    assert editor.locator('#mosaicAddClip').is_enabled()
    assert editor.evaluate('posts')==1
    assert editor.evaluate('latestReads')==reads_before_submit
    assert editor.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.includes('workbench.v3')))).deletions.length")==1


@pytest.mark.parametrize('stale',['old_plan','same_id_same_plan','different_id_wrong_plan','different_attempt_same_plan'])
def test_lost_submit_response_waits_past_old_job_then_restores_accepted_attempt(editor,stale):
    editor.evaluate('''stale => {
      const old=api;window.posts=0;window.latestReads=0;window.recoveryReads=0;
      window.oldJob={id:'b'.repeat(32),queued_task:true,status:'cancelled',targets:[],ranges:[[3,9],[12,18]],effect:{style:'pixel'}};
      window.newJob=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){
          if(body){
            posts++;
            newJob={...body,id:'c'.repeat(32),queued_task:true,status:'queued',source_sha256:session.source_sha256,progress:'本次任务已排队'};
            throw Object.assign(Error('response lost'),{outcomeUnknown:true});
          }
          latestReads++;
          if(posts&&++recoveryReads>1)return new Promise(resolve=>window.acceptNew=()=>resolve({job:newJob}));
          if(posts&&stale==='same_id_same_plan')return {job:{...newJob,id:oldJob.id,status:'cancelled'}};
          if(posts&&stale==='different_id_wrong_plan')return {job:{...oldJob,id:'d'.repeat(32),submission_id:newJob.submission_id,source_sha256:session.source_sha256,reset_at:''}};
          if(posts&&stale==='different_attempt_same_plan')return {job:{...newJob,id:'d'.repeat(32),submission_id:'e'.repeat(32),status:'failed'}};
          return {job:oldJob};
        }
        if(url.includes('/render/'+'c'.repeat(32)))return new Promise(()=>{});
        return old(url,body);
      };
    }''',stale)
    open_auto(editor)
    editor.locator('[data-event-delete]').first.click()
    editor.locator('#mosaicBatchStart').click()
    editor.wait_for_function('!!window.acceptNew',timeout=6000)
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.evaluate('posts')==1
    editor.evaluate('acceptNew()')
    editor.wait_for_function("document.getElementById('mosaicProcessingText').textContent==='本次任务已排队'")
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.locator('#mosaicStop').is_visible()
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert editor.evaluate('posts')==1


def test_lost_response_completed_attempt_restores_confirmation_without_resubmitting(editor):
    editor.evaluate('''() => {
      const old=api;window.posts=0;window.accepted=null;
      window.api=async(url,body)=>{
        if(url.endsWith('/render')){
          if(body){
            posts++;accepted={...body,id:'c'.repeat(32),queued_task:true,status:'completed',source_sha256:session.source_sha256,result:{}};
            throw TypeError('network response lost');
          }
          return {job:accepted};
        }
        return old(url,body);
      };
    }''')
    open_auto(editor)
    editor.locator('#mosaicAddClip').click()
    editor.locator('#mosaicStart').fill('00:00:03')
    editor.locator('#mosaicEnd').fill('00:00:08')
    editor.locator('#mosaicAddSubmit').click()
    editor.locator('#mosaicBatchStart').click()
    editor.locator('#mosaicBatchDialog').wait_for()
    assert editor.locator('#mosaicReplace').is_enabled()
    editor.locator('#mosaicBatchClose').click()
    assert editor.locator('#mosaicAddClip').is_disabled()
    assert editor.locator('#mosaicBatchStart').is_hidden()
    assert '删除 1 段' in editor.locator('#mosaicBatchHint').inner_text()
    assert editor.evaluate('posts')==1
