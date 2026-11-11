"""Use real browser fetch and atomic file writes for original downloads."""
from pathlib import Path

import pytest

from browser_support import shared_playwright
from test_original_download_integrity import upstream

pytestmark = pytest.mark.ui


@pytest.mark.parametrize('endpoint,expected,previous,outcome,stored', [
    ('/short', 16, 'old-file', None, 'old-file'),
    ('/short', 3, 'old-file', None, 'old-file'),
    ('/declared', 16, 'old-file', None, 'old-file'),
    ('/short', 5, 'old-file', 'saved', 'first'),
    ('/declared', 5, 'old-file', 'saved', 'first'),
    ('/short', 0, 'old-file', 'saved', 'first'),
    ('/short', 5, 'first', 'cached', 'first'),
    ('/short', 16, '', None, ''),
])
def test_browser_commits_only_complete_files(upstream, endpoint, expected, previous, outcome, stored):
    script = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    save = script[script.index('async function writeOriginalFile('):script.index('\nfunction startBrowserOriginalDownloads(')]
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(upstream + '/')
        # Fetch, the response reader and OPFS staging/abort/commit are real.
        page.add_script_tag(content="const originalsDownload={stopped:false,abort:new AbortController()};function originalFileUrl(){return window.originalTestPath}\n" + save)
        result = page.evaluate('''async([endpoint,expected,previous])=>{
            window.originalTestPath=endpoint;
            const dir=await navigator.storage.getDirectory();
            const folder=await dir.getDirectoryHandle('101606',{create:true});
            const handle=await folder.getFileHandle('EP1.mp4',{create:true});
            const old=await handle.createWritable();await old.write(previous);await old.close();
            let outcome=null,error=null;
            try{outcome=await writeOriginalFile(dir,'101606','EP1',expected,()=>{})}catch(e){error=e.message}
            const file=await handle.getFile();return {outcome,error,text:await file.text()};
        }''', [endpoint, expected, previous])
        browser.close()
    assert result['outcome'] == outcome, result
    assert bool(result['error']) == (outcome is None), result
    assert result['text'] == stored, result
