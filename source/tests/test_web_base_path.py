import base64
import json
from pathlib import Path
import shutil
import subprocess
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from exactcut import accounts, util, webserver, workflow


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(webserver, 'ROOT', tmp_path)
    monkeypatch.setattr(workflow, 'ROOT', tmp_path)
    monkeypatch.setattr(workflow, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(workflow, 'FEEDBACK', tmp_path / 'feedback')
    instance = ThreadingHTTPServer(('127.0.0.1', 0), webserver.Handler)
    instance.base_path = '/exactcut'
    instance.password = 'path-prefix-fixture-password'
    instance.auth_db = tmp_path / '.web-state/accounts.sqlite3'
    instance.auth_db.parent.mkdir(parents=True)
    accounts.initialize(instance.auth_db, instance.password)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def request(app, method, path, data=None, headers=None):
    connection = HTTPConnection(*app.server_address, timeout=5)
    try:
        connection.request(method, path, None if data is None else json.dumps(data), headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def login(app):
    status, headers, _ = request(app, 'POST', '/exactcut/api/login',
                                {'username': 'admin', 'password': app.password},
                                {'X-Exactcut-Request': '1'})
    assert status == 200
    assert '; Path=/exactcut/;' in headers['Set-Cookie']
    return {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}


def test_prefixed_login_redirect_pages_and_session(app):
    status, headers, _ = request(app, 'GET', '/exactcut/')
    assert status == 302
    assert headers['Location'] == '/exactcut/login'
    status, _, body = request(app, 'GET', '/exactcut/login')
    assert status == 200
    assert b'<base href="/exactcut/">' in body
    headers = login(app)
    status, _, body = request(app, 'GET', '/exactcut/', headers=headers)
    assert status == 200 and b'<base href="/exactcut/">' in body
    assert b'src="app.js"' in body and b'href="template.xlsx"' not in body
    for path in ('app.js', 'helper.js', 'template.xlsx', 'api/me', 'api/batches', 'healthz'):
        assert request(app, 'GET', '/exactcut/' + path, headers=headers)[0] == 200
    assert request(app, 'GET', '/healthz')[0] == 200


def test_prefixed_creation_image_and_video_range(app, tmp_path, monkeypatch):
    headers = login(app)
    status, _, body = request(app, 'POST', '/exactcut/api/batches/blank', {}, headers)
    assert status == 201
    created = json.loads(body)
    batch_id = created['id']
    request_id = created['draft']['rows'][0]['request_id']
    png = b'\x89PNG\r\n\x1a\nfixture'
    endpoint = f'/exactcut/api/batches/{batch_id}/ending-image'
    assert request(app, 'POST', endpoint, {'request_id': request_id,
                   'content': base64.b64encode(png).decode()}, headers)[0] == 200
    assert request(app, 'GET', endpoint + '/' + request_id + '?v=fixture.png', headers=headers)[2] == png
    video = tmp_path / 'range-fixture.mp4'
    video.write_bytes(b'0123456789')
    monkeypatch.setattr(workflow, 'preview_file', lambda *args, **kwargs: (video, {}))
    for suffix in ('video', 'preview'):
        status, response_headers, body = request(app, 'GET',
            f'/exactcut/api/batches/{batch_id}/{suffix}/fixture.mp4',
            headers={**headers, 'Range': 'bytes=2-5'})
        assert status == 206 and body == b'2345'
        assert response_headers['Content-Range'] == 'bytes 2-5/10'


@pytest.mark.parametrize('value', ['/exactcut/', 'exactcut', '//other', '/a/../b', '/a?x', '/a"'])
def test_invalid_base_path_rejected(value, monkeypatch):
    monkeypatch.setattr(util, 'load_config', lambda: {'web': {'base_path': value}})
    with pytest.raises(ValueError, match='base_path'):
        webserver.web_base_path()


def test_frontend_prefixed_requests_and_http_submission_ids():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable; covered by host regression')
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
(async()=>{
for(const prefix of ['', '/exactcut', '/tools/cut']){
 const saved=new Map(),calls=[],redirects=[];
 const context=vm.createContext({URL,document:{baseURI:'http://example.test'+prefix+'/'},
  crypto:{getRandomValues:require('node:crypto').webcrypto.getRandomValues.bind(require('node:crypto').webcrypto)},
  AbortController,setTimeout,clearTimeout,sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
  location:{replace:url=>redirects.push(url)},fetch:async(url,opts)=>{calls.push([url,opts]);return {ok:true,status:200,json:async()=>({ok:true})}}});
 vm.runInContext(source.split('function toast(')[0],context);
 await vm.runInContext("api('/api/batches/abc/start',{})",context);
 assert.equal(calls[0][0],'http://example.test'+prefix+'/api/batches/abc/start');
 assert.match(JSON.parse(calls[0][1].body).submission_token,/^[a-zA-Z0-9-]{16,80}$/);
 for(const path of ['/api/batches/abc/video/a.mp4','/api/batches/abc/preview/a.mp4','/api/batches/abc/ending-image/R1?v=1'])
  assert.equal(vm.runInContext('appUrl('+JSON.stringify(path)+')',context),'http://example.test'+prefix+path);
 assert.equal(vm.runInContext("appUrl('data:image/png;base64,eA==')",context),'data:image/png;base64,eA==');
 context.fetch=async()=>({status:401});
 await assert.rejects(vm.runInContext("api('/api/me')",context));
 assert.equal(redirects[0],'http://example.test'+prefix+'/login');
}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
