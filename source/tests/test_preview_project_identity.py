"""Browser regressions for cached project switching and preview ownership."""
import re
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / 'exactcut/web'


@pytest.fixture
def page():
    html = re.sub(r'<script src="[^"]+"></script>', '', (WEB / 'index.html').read_text())
    js = '\n'.join(line for line in (WEB / 'app.js').read_text().splitlines()
                   if not line.startswith('(async()=>{try{const initial='))
    with pytest.importorskip('playwright.sync_api').sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        page.goto('http://fixture.test')
        page.add_script_tag(content=js)
        page.evaluate('''() => {
          currentUser={role:'producer',features:{material_fetch:false}};
          window.makeProject=(id,rid)=>({id,filename:'网页填写',revision:1,
            media_managed:true,errors:[],warnings:[],feedback:[],feedback_count:0,
            draft:{drama_id:id,author:'test',business_date:'20261013',rows:[]},
            plan:{required:['EP1','EP2'],extra:[],episode_limit:10},
            runtime:{status:'review',updated_at:'2026-10-13T01:00:00Z',download_complete:true,events:[]},
            assets:[{request_id:rid,title:rid,status:'qc_passed',preview:'/preview/'+rid,
              sha256:rid,version_id:rid,filename:rid+'.mp4',duration_ms:1000}]});
          window.a=makeProject('a','RA');window.b=makeProject('b','RB');
          window.previewCalls=[];
          window.fetch=async url=>{previewCalls.push(String(url));return {ok:true,
            json:async()=>({url:'https://media.invalid/preview.mp4',expires_in:900})}};
          window.apiCalls=[];
          api=async(path,data)=>{apiCalls.push(path);return path==='/api/batches/b'?structuredClone(b):{}};
          scheduleSourcePrefetch=()=>{};
          selectedId='a';displayBatch(a);
        }''')
        yield page
        browser.close()


def test_cached_managed_prefetch_without_runtime_plan_recovers_to_latest(page):
    # material_versions.detail puts plan at top level; batch_response adds prefetch runtime fields.
    result = page.evaluate('''async()=>{
      const cached=structuredClone(b);
      Object.assign(cached.runtime,{status:'queued',download_complete:false,download_prefetch:true,
        download_current_episode:'EP2',download_current_received:1024,download_current_total:4096});
      rememberBatch(cached);
      let finish;api=path=>path==='/api/batches/b'?new Promise(r=>finish=r):Promise.resolve({});
      const opening=openBatch('b').catch(e=>e.message);
      const progress=$('stageOverview').textContent;
      if(finish)finish(structuredClone(b));
      const error=await opening;
      setTab('review');await loadPreview(document.querySelector('video'));
      return {error,progress,openingBatchId,status:current.runtime.status,
        cards:Array.from(document.querySelectorAll('video')).map(v=>v.dataset.previewRequest),calls:previewCalls};
    }''')
    assert result.get('error') is None, result
    assert 'EP2（2/2）' in result['progress']
    assert result['openingBatchId'] is None
    assert result['status'] == 'review'
    assert result['cards'] == ['RB']
    assert result['calls'] == ['http://fixture.test/api/batches/b/preview-url/RB']


def test_cached_render_failure_clears_switch_and_allows_retry(page):
    result = page.evaluate('''async()=>{
      rememberBatch(b);
      const render=renderRuntime;renderRuntime=()=>{throw Error('injected render failure')};
      let error;try{await openBatch('b')}catch(e){error=e.message}
      const failed={error,openingBatchId,hidden:$('workspace').hidden,
        cached:batchDetails.has('b')};
      renderRuntime=render;
      await openBatch('b');setTab('review');await loadPreview(document.querySelector('video'));
      return {failed,status:current.runtime.status,openingBatchId,calls:previewCalls};
    }''')
    assert result['failed'] == dict(error='injected render failure', openingBatchId=None,
                                   hidden=True, cached=False)
    assert result['openingBatchId'] is None
    assert result['calls'] == ['http://fixture.test/api/batches/b/preview-url/RB']


def test_old_video_cannot_request_preview_with_new_project_even_with_same_material_id(page):
    result = page.evaluate('''async()=>{
      const video=document.querySelector('video');
      // A stale DOM must not be accepted merely because IDs also occur in a copied project.
      current=makeProject('b','RA');selectedId='b';activeTab='review';
      await loadPreview(video);
      return previewCalls;
    }''')
    assert result == []


@pytest.mark.parametrize('plan', [None, {}, {'required': []}, {'required': ['EP1']}])
def test_prefetch_unknown_episode_or_missing_plan_does_not_break_render(page, plan):
    result = page.evaluate('''plan=>{
      current.plan=plan;
      Object.assign(current.runtime,{status:'queued',download_complete:false,download_prefetch:true,
        download_current_episode:'EP2',download_current_received:1024,download_current_total:4096});
      renderRuntime();return $('stageOverview').textContent;
    }''', plan)
    assert 'EP2' in result and '25%' in result
    assert '0/' not in result and '/0' not in result


def test_preview_plays_real_video_after_cached_project_switch(page, tmp_path):
    video = tmp_path / 'preview.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=96x96:r=12:d=1', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(video)], check=True)
    page.route('https://media.invalid/preview.mp4',
               lambda route: route.fulfill(body=video.read_bytes(), content_type='video/mp4'))
    page.evaluate("""async()=>{
      const cached=structuredClone(b);
      Object.assign(cached.runtime,{status:'queued',download_complete:false,download_prefetch:true,
        download_current_episode:'EP2',download_current_received:1024,download_current_total:4096});
      rememberBatch(cached);await openBatch('b');setTab('review');
      await loadPreview(document.querySelector('video'));
    }""")
    page.wait_for_function("document.querySelector('video').readyState>=2")
    page.evaluate("document.querySelector('video').play()")
    page.wait_for_function("document.querySelector('video').currentTime>0.1")
    assert page.evaluate("previewCalls") == ['http://fixture.test/api/batches/b/preview-url/RB']
    assert page.evaluate("document.querySelector('video').error") is None
