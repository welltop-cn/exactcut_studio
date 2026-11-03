"""Visibility recovery uses the current editor identity and frame only."""
import pytest
from test_mosaic_ui import editor as mosaic_editor
from test_cutting_ui import editor as cut_editor

pytestmark = pytest.mark.ui


@pytest.mark.parametrize('immediate', [False, True])
@pytest.mark.parametrize('mode', ['inflight', 'backoff'])
@pytest.mark.parametrize('kind', ['mosaic', 'cut'])
def test_hidden_first_frame_resumes_latest_frame(request, kind, mode, immediate):
    page=request.getfixturevalue(kind+'_editor')
    page.clock.install()
    page.clock.pause_at(page.evaluate('Date.now()'))
    page.evaluate('''({kind,mode})=>{
      window.hiddenForTest=false;Object.defineProperty(document,'hidden',{get:()=>hiddenForTest,configurable:true});
      window.changeVisibility=hidden=>{hiddenForTest=hidden;document.dispatchEvent(new Event('visibilitychange'))};
      window.frameCalls=[];window.scanPosts=0;const oldApi=api;window.api=async(url,body)=>{if(kind==='mosaic'&&url.endsWith('/scan')){if(body){scanPosts++;return {id:'auto-task',status:'completed',source_sha256:session.source_sha256,reset_at:'',events:[]}}return {enabled:true,report:null};}return oldApi(url,body)};
      const original=fetch;window.fetch=async(url,options)=>{
        if(!String(url).includes('/frame/'))return original(url,options);
        frameCalls.push(String(url));
        if(frameCalls.length===1){
          if(mode==='backoff')return new Response(JSON.stringify({error:'busy',code:'editor_resource_busy',resource:'frame',retryable:true,retry_after_ms:1000}),{status:429});
          return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('cancel','AbortError'))));
        }
        return original(url,options);
      };
      $(kind+'Frame').decode=async()=>{};
      $(kind+'Dialog').close();
    }''', {'kind':kind,'mode':mode})
    page.locator('[data-open-'+kind+']').evaluate('(el)=>el.click()')
    page.wait_for_function('frameCalls.length===1')
    if mode=='backoff':
        page.wait_for_function(f"document.getElementById('{kind}FrameHint').textContent.includes('正在重试')")
    if immediate:
        page.evaluate('() => {changeVisibility(true);changeVisibility(false)}')
    else:
        page.evaluate('changeVisibility(true)')
        page.clock.run_for(5000)
        assert page.evaluate('frameCalls.length')==1
        page.evaluate('changeVisibility(false)')
    page.wait_for_function('frameCalls.length===2')
    page.wait_for_function(f"!document.getElementById('{kind}Frame').hidden")
    if kind=='mosaic':
        page.wait_for_function('scanPosts===1')
    assert page.evaluate('scanPosts')==(1 if kind=='mosaic' else 0)
