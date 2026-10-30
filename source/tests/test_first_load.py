import gzip
import fnmatch
import tomllib
import re
import subprocess
from pathlib import Path
from test_web_base_path import app, login, request


def test_versioned_scripts_compressed_and_private(app):
    headers = login(app)
    _, page_headers, html = request(app, 'GET', '/home/', headers=headers)
    urls = re.findall(rb'src="([^"]+\.js\?v=[0-9a-f]+)"', html)
    assert {url.split(b'?')[0] for url in urls} == {
        b'diagnostics.js', b'editor-busy.js', b'app.js', b'editor-source.js', b'cutting.js', b'mosaic.js', b'helper.js',
        b'preview-subtitles.js'
    }
    assert page_headers['Cache-Control'] == 'no-store'
    for url in urls:
        path = '/home/' + url.decode()
        status, compressed_headers, compressed = request(app, 'GET', path, headers={**headers, 'Accept-Encoding': 'gzip'})
        assert status == 200
        assert compressed_headers['Content-Encoding'] == 'gzip'
        assert compressed_headers['Cache-Control'] == 'private, max-age=31536000, immutable'
        _, _, plain = request(app, 'GET', path, headers=headers)
        assert gzip.decompress(compressed) == plain
        assert len(compressed) < len(plain)
        assert request(app, 'GET', path)[0] == 401
    _, stale_headers, _ = request(app, 'GET', '/home/app.js?v=stale', headers=headers)
    assert stale_headers['Cache-Control'] == 'no-store'
    _, denied_headers, _ = request(app, 'GET', '/home/app.js', headers={**headers, 'Accept-Encoding': 'gzip;q=0'})
    assert 'Content-Encoding' not in denied_headers


def test_every_served_script_is_declared_as_package_data(app):
    headers = login(app)
    _, _, html = request(app, 'GET', '/home/', headers=headers)
    names = re.findall(rb'src="([^"?]+\.js)\?v=', html)
    config = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
    patterns = config['tool']['setuptools']['package-data']['exactcut']
    missing = [name.decode() for name in names
               if not any(fnmatch.fnmatch('web/' + name.decode(), pattern) for pattern in patterns)]
    assert not missing, f'Browser modules missing from installed package: {missing}'


def test_startup_parallel_identity_preserves_workspace_dependency():
    source = Path(__file__).resolve().parents[1] / 'exactcut/web/app.js'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
(async()=>{
for(const linked of ['', '#linked']){
 const calls=[],elements=new Map();let resolveMe,resolveWorkspace,opened;
 const ctx=vm.createContext({Promise,location:{hash:linked},$:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id)},
 api:path=>{calls.push(path);if(path==='/api/me')return new Promise(r=>resolveMe=r);if(path==='/api/workspace')return new Promise(r=>resolveWorkspace=r);throw Error(path)},
 refreshBatches:async()=>{calls.push('list');return [{id:'linked'}]},openBatch:async(id,opts)=>{opened=[id,opts]},showEmptyWorkspace:()=>calls.push('empty'),toast:()=>{},poll:async()=>calls.push('poll')});
 vm.runInContext('let currentUser,polling=true;'+source.slice(source.indexOf('async function restoreWorkspace('),source.indexOf(';setInterval(')),ctx);
 assert.deepEqual(calls,['/api/me','/api/workspace']);
 resolveWorkspace({batch_id:null});await new Promise(setImmediate);
 assert(!calls.includes('list'),'list must wait for identity');
 resolveMe({role:'producer',name:'Fixture'});await new Promise(setImmediate);
 assert.equal(calls.filter(x=>x==='list').length,1);assert(!calls.includes('poll'),'no immediate duplicate poll');
 if(linked){assert.equal(opened[0],'linked');assert.equal(opened[1].refreshList,false)}else assert(calls.includes('empty'));
 assert.equal(vm.runInContext('polling',ctx),false);
}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-e', script, str(source)], check=True, capture_output=True, text=True)
