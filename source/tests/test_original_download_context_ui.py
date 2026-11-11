"""Batch downloads keep the source selected when the user clicked download."""
from urllib.parse import parse_qs, urlsplit

import pytest
from test_clip_text_ui import page

pytestmark = pytest.mark.ui


@pytest.mark.parametrize('page', ['http://localhost/'], indirect=True)
@pytest.mark.parametrize('change', ['drama', 'project'])
def test_editing_context_during_download_cannot_mix_sources(page, change):
    page.evaluate('''async()=>{
      const root=await navigator.storage.getDirectory();
      window.downloadTestDir=await root.getDirectoryHandle('originals-context',{create:true});
      await writeOriginalsDir(downloadTestDir);
      const second=structuredClone(current);second.id='second';second.draft.drama_id='101606';
      api=async path=>path==='/api/batches/second'?structuredClone(second):({drama_id:enteredDramaId(),episodes:[{episode:'EP1',bytes:3},{episode:'EP2',bytes:3}]});
      renderBatches([{id:'fixture',drama_id:'101494',status:'draft',count:1,created_at:'2026-11-11'},
                     {id:'second',drama_id:'101606',status:'draft',count:1,created_at:'2026-11-11'}]);
      scheduleOriginalsLookup(true);
    }''')
    page.wait_for_function('!document.querySelector("#originalsOpen").disabled')
    requests, pending = [], []

    def download(route):
        requests.append(route.request.url)
        if len(requests) == 1:
            pending.append(route)
        else:
            drama = parse_qs(urlsplit(route.request.url).query)['drama_id'][0]
            route.fulfill(body=b'AAA' if drama == '101494' else b'BBB')

    page.route('**/originals/*/file?*', download)
    page.locator('#originalsOpen').click()
    with page.expect_request(lambda request: '/originals/EP1/file?' in request.url):
        page.locator('#originalsDownloadAll').click()
    page.locator('#closeOriginals').click()
    if change == 'drama':
        page.locator('#meta-drama_id').fill('101606')
    else:
        page.locator('[data-batch="second"]').click()
        page.wait_for_function("current?.id==='second'")
    assert len(pending) == 1
    pending[0].fulfill(body=b'AAA')
    page.wait_for_function('!originalsDownload.running')
    files = page.evaluate('''async()=>{
      const folder=await downloadTestDir.getDirectoryHandle('101494');const contents=[];
      for(const ep of ['EP1','EP2']){const file=await (await folder.getFileHandle(ep+'.mp4')).getFile();contents.push(await file.text())}
      return contents;
    }''')
    assert files == ['AAA', 'AAA'], {'files': files, 'requests': requests}
    assert [parse_qs(urlsplit(url).query)['drama_id'][0] for url in requests] == ['101494', '101494']
    assert all('/api/batches/fixture/' in url for url in requests)
