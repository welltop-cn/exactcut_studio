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
    instance.base_path = '/home'
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
    status, headers, _ = request(app, 'POST', '/home/api/login',
                                {'username': 'admin', 'password': app.password},
                                {'X-Exactcut-Request': '1'})
    assert status == 200
    assert '; Path=/home/;' in headers['Set-Cookie']
    return {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}


def test_prefixed_login_redirect_pages_and_session(app):
    status, headers, _ = request(app, 'GET', '/home/')
    assert status == 302
    assert headers['Location'] == '/home/login'
    status, _, body = request(app, 'GET', '/home/login')
    assert status == 200
    assert b'<base href="/home/">' in body
    headers = login(app)
    status, _, body = request(app, 'GET', '/home/', headers=headers)
    assert status == 200 and b'<base href="/home/">' in body
    assert b'src="app.js?v=' in body and b'href="template.xlsx"' not in body
    for path in ('app.js', 'helper.js', 'template.xlsx', 'api/me', 'api/batches', 'healthz'):
        assert request(app, 'GET', '/home/' + path, headers=headers)[0] == 200
    assert request(app, 'GET', '/healthz')[0] == 200


def test_prefixed_creation_image_and_video_range(app, tmp_path, monkeypatch):
    headers = login(app)
    status, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    assert status == 201
    created = json.loads(body)
    batch_id = created['id']
    request_id = created['draft']['rows'][0]['request_id']
    png = b'\x89PNG\r\n\x1a\nfixture'
    endpoint = f'/home/api/batches/{batch_id}/ending-image'
    assert request(app, 'POST', endpoint, {'request_id': request_id,
                   'content': base64.b64encode(png).decode()}, headers)[0] == 200
    assert request(app, 'GET', endpoint + '/' + request_id + '?v=fixture.png', headers=headers)[2] == png
    video = tmp_path / 'range-fixture.mp4'
    video.write_bytes(b'0123456789')
    monkeypatch.setattr(workflow, 'preview_file', lambda *args, **kwargs: (video, {}))
    for suffix in ('video', 'preview'):
        status, response_headers, body = request(app, 'GET',
            f'/home/api/batches/{batch_id}/{suffix}/fixture.mp4',
            headers={**headers, 'Range': 'bytes=2-5'})
        assert status == 206 and body == b'2345'
        assert response_headers['Content-Range'] == 'bytes 2-5/10'


@pytest.mark.parametrize('value', ['/home/', 'exactcut', '//other', '/a/../b', '/a?x', '/a"'])
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
for(const prefix of ['', '/home', '/tools/cut']){
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
 const delays=[];context.setTimeout=(fn,ms)=>{delays.push(ms);return 1};context.clearTimeout=()=>{};
 vm.runInContext("current={id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',media_managed:true,revision:1}",context);
 await vm.runInContext("api('/api/batches/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/hook-preview',{index:0})",context);
 assert.equal(delays.at(-1),210000,'managed Hook preview must retain the AI timeout');
 context.fetch=async()=>({status:401});
 await assert.rejects(vm.runInContext("api('/api/me')",context));
 assert.equal(redirects[0],'http://example.test'+prefix+'/login');
}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_default_and_production_base_path_are_home():
    config = Path(__file__).resolve().parents[1] / 'config'
    for name in ('default.json', 'production.json'):
        assert json.loads((config / name).read_text())['web']['base_path'] == '/home'


def test_other_prefix_is_not_redirected_or_compatible(app):
    headers = login(app)
    for suffix in ('/', '/api/me', '/login', '/app.js'):
        status, response_headers, _ = request(app, 'GET', '/retired-prefix' + suffix, headers=headers)
        assert status == 404
        assert 'Location' not in response_headers


def test_preview_url_authorizes_before_upload_and_does_not_stream(app, tmp_path, monkeypatch):
    from exactcut import preview_oss
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    video = tmp_path / 'preview.mp4'; video.write_bytes(b'fixture')
    called = []
    monkeypatch.setattr(workflow, 'preview_file', lambda *a, **kw: (video, {}))
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    monkeypatch.setattr(preview_oss, 'signed_preview', lambda *a, **kw: called.append(a) or {'url':'https://oss.invalid/private?signature=fixture','expires_in':900})
    path = f'/home/api/batches/{batch_id}/preview-url/R1'
    assert request(app, 'GET', path)[0] == 401
    assert request(app, 'GET', '/home/api/batches/'+'f'*32+'/preview-url/R1', headers=headers)[0] == 404
    assert called == []
    status, response_headers, body = request(app, 'GET', path, headers=headers)
    assert status == 200
    assert json.loads(body)['url'].startswith('https://oss.invalid/')
    assert response_headers['Cache-Control'] == 'no-store'
    assert called == [(video, batch_id, 'R1')]
    def failed(*a, **kw): raise RuntimeError('sensitive upstream detail')
    monkeypatch.setattr(preview_oss, 'signed_preview', failed)
    status, _, body = request(app, 'GET', path, headers=headers)
    assert status == 503
    assert b'sensitive' not in body


def test_oss_preparation_does_not_hold_project_read_lock(app, tmp_path, monkeypatch):
    from exactcut import preview_oss
    from concurrent.futures import ThreadPoolExecutor
    headers = login(app)
    _, _, body = request(app, 'POST', '/home/api/batches/blank', {}, headers)
    batch_id = json.loads(body)['id']
    video = tmp_path / 'preview.mp4'; video.write_bytes(b'fixture')
    entered, finish = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert finish.wait(4)
        return {'url': 'https://oss.invalid/private', 'expires_in': 900}
    monkeypatch.setattr(workflow, 'preview_file', lambda *a, **kw: (video, {}))
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    monkeypatch.setattr(preview_oss, 'signed_preview', slow)
    with ThreadPoolExecutor(2) as pool:
        preview = pool.submit(request, app, 'GET', f'/home/api/batches/{batch_id}/preview-url/R1', headers=headers)
        try:
            assert entered.wait(1)
            detail = pool.submit(request, app, 'GET', f'/home/api/batches/{batch_id}', headers=headers)
            assert detail.result(timeout=1)[0] == 200
            assert not preview.done()
        finally:
            finish.set()
        assert preview.result()[0] == 200


def test_original_download_redirects_to_signed_oss_without_streaming(app,tmp_path,monkeypatch):
    from exactcut import preview_oss
    headers=login(app)
    _,_,body=request(app,'POST','/home/api/batches/blank',{},headers)
    batch_id=json.loads(body)['id'];video=tmp_path/'master.mp4';video.write_bytes(b'master')
    monkeypatch.setattr(workflow,'preview_file',lambda *a,**kw:(video,{}))
    monkeypatch.setattr(preview_oss,'enabled',lambda:True)
    monkeypatch.setattr(preview_oss,'signed_preview',lambda *a,**kw:{'url':'https://oss.invalid/master?signature=fixture','expires_in':900})
    status,response_headers,body=request(app,'GET',f'/home/api/batches/{batch_id}/download/R1',headers=headers)
    assert status==302
    assert response_headers['Location']=='https://oss.invalid/master?signature=fixture'
    assert response_headers['Cache-Control']=='no-store'
    assert body==b''
