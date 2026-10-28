"""Real Chromium decoding of a synthetic master through the OSS source path."""
import pytest
from test_cutting_ui import editor as cut_editor
from test_mosaic_ui import editor as mosaic_editor, open_editor
from test_cutting import make_video


@pytest.fixture(params=['cut','mosaic'])
def cloud_editor(request,tmp_path):
    kind=request.param
    page=request.getfixturevalue(kind+'_editor')
    if kind=='cut':page.locator('#cutClose').click();page.wait_for_function("!document.querySelector('#cutDialog').open")
    file=tmp_path/'master.mp4';make_video(file)
    data=file.read_bytes()
    def media(route):
        start=int(route.request.headers.get('range','bytes=0-').split('=')[1].split('-')[0])
        route.fulfill(status=206,body=data[start:],content_type='video/mp4',headers={'Accept-Ranges':'bytes','Content-Range':f'bytes {start}-{len(data)-1}/{len(data)}'})
    page.route('https://media.test/**',media)
    page.route('**/render/*/video*',lambda r:r.fulfill(body=data,content_type='video/mp4'))
    page.route('**/source-url',lambda r:r.fulfill(json={'source':'oss','url':'https://media.test/master.mp4','expires_in':900,'reason':'verified'}))
    page.evaluate('''() => {
      session.capabilities={...session.capabilities,oss_playback:true};
      const old=api;
      window.api=async(url,body)=>{
        if(url.endsWith('/render')&&!body)return {job:{id:'b'.repeat(32),status:'completed',targets:[],ranges:[],result:{},completed_at:'now'}};
        return old(url,body);
      };
    }''')
    if kind=='mosaic':open_editor(page)
    else:page.locator('[data-open-cut]').evaluate('(el)=>el.click()')
    page.wait_for_function('kind=>document.getElementById(kind+"Source").readyState>=2',arg=kind)
    return page,kind


def test_cloud_master_decodes_plays_and_seeks(cloud_editor):
    p,kind=cloud_editor
    assert p.locator('#'+kind+'Source').get_attribute('src')=='https://media.test/master.mp4'
    assert p.locator('#'+kind+'Source').get_attribute('crossorigin') is None
    p.locator('#'+kind+'Play').click()
    p.wait_for_function('kind=>document.getElementById(kind+"Source").currentTime>.3',arg=kind)
    p.locator('#'+kind+'Play').click()
    p.locator('#'+kind+'Seek').evaluate("el=>{el.value=45;el.dispatchEvent(new Event('input'));}")
    p.wait_for_function('kind=>Math.abs(document.getElementById(kind+"Source").currentTime-1.5)<.02',arg=kind)
    assert p.locator('#'+kind+'Source').evaluate('v=>v.paused')


def test_before_uses_master_but_after_and_download_keep_job_output(cloud_editor):
    p,kind=cloud_editor
    if kind!='mosaic':pytest.skip('mosaic comparison only')
    p.locator('#mosaicBatchView').click()
    after=p.locator('#mosaicBatchVideo').get_attribute('src')
    assert '/render/' in after and '/video?' in after
    assert p.locator('#mosaicBatchDownload').get_attribute('href')==after
    p.locator('#mosaicBatchBefore').click()
    p.wait_for_function("document.getElementById('mosaicBatchVideo').getAttribute('src')==='https://media.test/master.mp4'")
    assert p.locator('#mosaicBatchVideo').get_attribute('src')==p.locator('#mosaicSource').get_attribute('src')
    p.locator('#mosaicBatchAfter').click()
    assert p.locator('#mosaicBatchVideo').get_attribute('src')==after
    assert p.locator('#mosaicBatchDownload').get_attribute('href')==after


def test_pause_during_source_reload_keeps_selected_frame(cloud_editor):
    p,kind=cloud_editor
    p.evaluate('''kind=>{
      const v=document.getElementById(kind+'Source');v.pause();
      let time=0,paused=true;
      Object.defineProperties(v,{currentTime:{get:()=>time,set:t=>time=t},paused:{get:()=>paused},src:{get:()=>v.getAttribute('src'),set:()=>{}}});
      v.play=()=>{paused=false;v.dispatchEvent(new Event('play'));return Promise.resolve();};
      v.pause=()=>{paused=true;};v.load=()=>{time=0;paused=true;};
      document.getElementById(kind+'Play').click();
      v.currentTime=1.5;v.dispatchEvent(new Event('timeupdate'));v.dispatchEvent(new Event('error'));
    }''',kind)
    p.wait_for_function('kind=>document.getElementById(kind+"Source").currentTime===0',arg=kind)
    p.locator('#'+kind+'Play').evaluate('el=>el.click()')
    assert p.locator('#'+kind+'FrameNumber').input_value()=='00:01:15'
