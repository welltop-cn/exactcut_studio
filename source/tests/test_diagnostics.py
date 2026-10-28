import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def module():
    from exactcut import diagnostics
    return diagnostics


def records(root):
    return [json.loads(line) for p in sorted((root / 'logs').glob('events-*.jsonl'))
            for line in p.read_text().splitlines()]


@pytest.fixture
def diag(tmp_path):
    d = module()
    d.configure(tmp_path, {'max_file_bytes': 4096, 'max_total_bytes': 16384, 'retention_days': 14})
    yield d, tmp_path
    d.disable()


def test_context_and_redaction(diag, monkeypatch):
    d, root = diag
    monkeypatch.setenv('SERVICE_API_KEY', 'secret-from-environment')
    with d.context(trace_id='abc', batch_id='batch', request_id='R001'):
        try:
            raise ValueError('password="secret-one extra-private" cookie=session-secret https://user:pass@host/x?sig=secret-url Bearer secret-bearer secret-from-environment')
        except ValueError as exc:
            d.event('operation.failed', error=exc, password='secret-field')
    row = records(root)[0]
    text = json.dumps(row)
    assert row['trace_id'] == 'abc' and row['request_id'] == 'R001'
    assert row['error']['type'] == 'ValueError' and row['error']['frames']
    assert all(value not in text for value in ('secret-one', 'extra-private', 'session-secret', 'secret-url', 'secret-bearer', 'secret-from-environment', 'secret-field', 'user:pass'))
    assert (root / 'logs').stat().st_mode & 0o077 == 0


def test_retention_size_limit_and_foreign_files(diag):
    d, root = diag
    d.event('startup')
    old = root / 'logs/events-20000101-000000.jsonl'
    old.write_text('{}\n')
    os.utime(old, (0, 0))
    foreign = root / 'logs/keep.txt'
    foreign.write_text('keep')
    for i in range(70):
        d.event('sample', sequence=i, detail='x' * 500)
    files = list((root / 'logs').glob('events-*.jsonl'))
    assert not old.exists() and foreign.read_text() == 'keep'
    assert len(files) > 1 and sum(p.stat().st_size for p in files) <= 16384
    assert records(root)[-1]['sequence'] == 69


def test_multiprocess_records_are_complete(tmp_path):
    d = module()
    script = '''import sys
from pathlib import Path
from exactcut import diagnostics as d
d.configure(Path(sys.argv[1]), {'max_file_bytes': 4096, 'max_total_bytes': 1000000})
for i in range(60): d.event('parallel', worker=sys.argv[2], sequence=i)
'''
    processes = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path), str(i)]) for i in range(4)]
    assert all(p.wait(timeout=20) == 0 for p in processes)
    rows = records(tmp_path)
    assert len(rows) == 240
    assert len({(r['worker'], r['sequence']) for r in rows}) == 240


def test_disk_failure_does_not_break_business(diag, monkeypatch):
    d, _ = diag
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(d, '_append', fail)
    d.event('still.running')
    with d.span('business'):
        value = 42
    assert value == 42


def test_http_trace_errors_no_passwords(diag, app):
    from test_web_base_path import request
    d, root = diag
    status, headers, _ = request(app, 'POST', '/home/api/login?secret=query-secret',
                                {'username': 'admin', 'password': 'wrong-secret'},
                                {'X-Exactcut-Request': '1'})
    assert status == 401 and headers.get('X-Request-ID')
    # Server writes completion immediately after sending the response.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = records(root)
        if any(r['event'] == 'http.completed' for r in rows): break
        time.sleep(.01)
    row = next(r for r in rows if r['event'] == 'http.completed')
    assert row['trace_id'] == headers['X-Request-ID'] and row['status'] == 401
    assert 'query-secret' not in json.dumps(rows) and 'wrong-secret' not in json.dumps(rows)


def test_error_screenshot_id_and_client_id_correlate_to_server(diag, app):
    from test_web_base_path import request
    d, root = diag
    client_id = 'c' * 32
    status, headers, raw = request(app, 'POST', '/home/api/login',
        {'username': 'admin', 'password': 'wrong'},
        {'X-Exactcut-Request': '1', 'X-Client-Request-ID': client_id})
    body = json.loads(raw)
    assert status == 401
    assert body['trace_id'] == headers['X-Request-ID']
    assert body['trace_id'] in body['error_info']['detail']
    row = next(r for r in records(root) if r['event'] == 'http.rejected')
    assert row['client_request_id'] == client_id


def test_browser_events_keep_distinct_actions_and_context(diag, app):
    from test_web_base_path import request, login
    d, root = diag
    headers = login(app)
    for action in ('saveButton', 'confirmButton'):
        assert request(app, 'POST', '/home/api/client-events', {
            'event': 'action', 'action': action, 'batch_id': 'a' * 32,
            'page_id': 'b' * 32, 'frontend_build': 'c' * 20, 'row_index': 0,
            'route': 'batches.hook-preview'}, headers)[0] == 200
    actions = [r['browser'] for r in records(root) if r['event'] == 'browser.event']
    assert {r['action'] for r in actions} == {'saveButton', 'confirmButton'}
    assert all(r['batch_id'] == 'a' * 32 and r['page_id'] == 'b' * 32 for r in actions)
    assert request(app, 'POST', '/home/api/client-events', {
        'event': 'action', 'duration_ms': float('nan')}, headers)[0] == 400
    assert request(app, 'POST', '/home/api/client-events', {
        'event': 'action', 'status': 'not_a_number'}, headers)[0] == 400


def test_browser_budget_cannot_be_bypassed_by_new_client_ids(diag, app, monkeypatch):
    from test_web_base_path import request, login
    d, root = diag
    d.configure(root, {'browser_events_per_minute': 2})
    monkeypatch.setattr(d, '_browser_budgets', {})
    headers = login(app)
    for number in range(3):
        assert request(app, 'POST', '/home/api/client-events', {
            'event': 'request_started', 'client_request_id': f'{number:032x}'}, headers)[0] == 200
    assert len([r for r in records(root) if r['event'] == 'browser.event']) == 2
    assert len([r for r in records(root) if r['event'] == 'browser.rate_limited']) == 1


def test_browser_budget_renews_and_accounts_are_independent(diag, monkeypatch):
    d, root = diag
    d.configure(root, {'browser_events_per_minute': 2})
    monkeypatch.setattr(d, '_browser_budgets', {})
    clock = [0.]
    monkeypatch.setattr(d.time, 'monotonic', lambda: clock[0])
    assert d.allow_browser_event('alice') and d.allow_browser_event('alice')
    assert not d.allow_browser_event('alice')
    assert d.allow_browser_event('bob')
    clock[0] = 61.
    assert d.allow_browser_event('alice')


def test_query_can_find_browser_trace_and_actor(diag):
    d, root = diag
    d.event('browser.event', actor='producer03', browser={'http_trace_id': 'a' * 32})
    d.event('browser.event', actor='someone_else', browser={'http_trace_id': 'a' * 32})
    result = subprocess.run([sys.executable, '-m', 'exactcut.diagnostics', '--root', str(root),
        '--actor', 'producer03', '--trace', 'a' * 32], capture_output=True, text=True, check=True)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 1 and rows[0]['actor'] == 'producer03'


from test_web_base_path import app


def test_missing_source_reports_drama_episode_and_reason(diag, monkeypatch):
    from exactcut import source_cache
    d, root = diag
    monkeypatch.setattr(source_cache, 'source_root', lambda: root / 'sources')
    monkeypatch.setattr(source_cache, '_reserve_for_fetch', lambda: None)
    monkeypatch.setattr(source_cache, '_fetch_argv', lambda: None)
    with pytest.raises(RuntimeError):
        source_cache._ensure_one('101604', 'EP1')
    row = next(r for r in records(root) if r['event'] == 'source.unavailable')
    assert row['drama_id'] == '101604' and row['episode'] == 'EP1'
    assert row['reason'] == 'fetch_command_missing'


def test_media_timeout_retains_cause_without_command_secrets(diag, monkeypatch):
    from exactcut import media
    d, root = diag
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(['ffmpeg', 'password=never-log-this'], 3)
    monkeypatch.setattr(media.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError):
        media.run_checked(['ffmpeg', '-i', 'private-input', 'out.mp4'], timeout=3)
    row = next(r for r in records(root) if r['event'] == 'media.timeout')
    assert row['tool'] == 'ffmpeg' and row['timeout_seconds'] == 3
    assert 'never-log-this' not in json.dumps(records(root))


def test_scheduler_wait_has_identifiers_reason_and_is_throttled(diag):
    from exactcut import webserver
    d, root = diag
    job = {'id': 'job-test', 'batch_id': 'batch-test', 'request_id': 'R001', 'phase': 'render', 'created': time.time()-60}
    for _ in range(20): webserver.log_wait(job, 'pool_full')
    rows = [r for r in records(root) if r['event'] == 'job.waiting']
    assert len(rows) == 1 and rows[0]['reason'] == 'pool_full'
    assert rows[0]['queue_seconds'] >= 60 and rows[0]['batch_id'] == 'batch-test'


def test_preview_admission_logs_exact_budget_and_release(diag, monkeypatch):
    from exactcut import webserver as web, concurrency, storage
    from exactcut.stage_results import ResourceDeferred
    d, root = diag
    monkeypatch.setattr(web, 'JOBS', root / 'jobs')
    monkeypatch.setattr(web, 'INLINE_RENDERS', set())
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'hook_previews': 1}})
    with d.context(trace_id='a' * 32, batch_id='b' * 32, actor='producer03'):
        token = web.admit_hook_preview(root / 'project')
        try:
            with pytest.raises(ResourceDeferred): web.admit_hook_preview(root / 'project')
        finally:
            web.release_hook_preview(token)
    rows = records(root)
    denial = next(r for r in rows if r['event'] == 'hook_preview.admission_denied')
    assert denial['reason'] == 'preview_pool_full'
    assert denial['active_previews'] == 1 and denial['preview_limit'] == 1
    assert denial['actor'] == 'producer03' and denial['batch_id'] == 'b' * 32
    assert next(r for r in rows if r['event'] == 'hook_preview.released')['active_previews'] == 0
    monkeypatch.setattr(storage, 'reserve', lambda *args: False)
    with pytest.raises(ResourceDeferred): web.admit_hook_preview(root / 'project')
    assert records(root)[-1]['reason'] == 'disk_reservation'


def test_preview_stage_logs_real_layout_without_recording_text(diag, fixture):
    from exactcut import hook_preview
    from exactcut.intake import validate_draft
    d, root = diag
    manifest, errors = validate_draft(fixture['draft'])
    assert not errors
    work = root / 'preview-work'; work.mkdir()
    result, _, _ = hook_preview.compute(root / 'projects' / manifest.drama_id, manifest.requests[0], work)
    rows = records(root)
    assert result['image'].startswith('data:image/png')
    events = {r['event'] for r in rows}
    assert {'hook_preview.emphasis.completed', 'hook_preview.sampling.completed',
            'hook_preview.presentation.completed', 'hook_preview.result'} <= events
    text = json.dumps(rows)
    assert manifest.requests[0].hook_text not in text and 'data:image' not in text


from test_phase2_preview_api import fixture


@pytest.mark.parametrize('outcome,status', [('success', 200), ('error', 400), ('deferred', 429), ('cleanup_error', 200)])
def test_preview_http_cleanup_and_compute_deferral_are_observable(diag, fixture, monkeypatch, outcome, status):
    from exactcut import hook_preview, storage, webserver as web
    from exactcut.stage_results import ResourceDeferred
    from test_phase2_preview_api import call, serve
    d, root = diag
    monkeypatch.setattr(web, 'JOBS', root / 'jobs'); web.JOBS.mkdir()
    monkeypatch.setattr(web, 'INLINE_RENDERS', set())
    def compute(project, request, work):
        cache = work / 'cache'; cache.mkdir()
        if outcome == 'error': raise RuntimeError('fixture failure')
        if outcome == 'deferred': raise ResourceDeferred(time.time() + 10, 'AI 请求预算已用完，请稍后重试')
        return {'preview_key': 'a' * 64}, {'decision': {}}, cache
    monkeypatch.setattr(hook_preview, 'compute', compute)
    if outcome == 'cleanup_error':
        monkeypatch.setattr(storage, 'release', lambda *args: (_ for _ in ()).throw(OSError('fixture cleanup failure')))
    app = serve()
    try:
        assert call(app, 'POST', f"/api/batches/{fixture['id']}/hook-preview", {
            'index': 0, 'revision': fixture['revision']})[0] == status
        assert not web.INLINE_RENDERS
        if outcome != 'cleanup_error':
            with storage.reservation_lock(web.JOBS) as ledger:
                assert not ledger['jobs']
        if outcome == 'deferred':
            assert any(r['event'] == 'hook_preview.deferred' and r['phase'] == 'compute' for r in records(root))
        if outcome == 'cleanup_error':
            assert any(r['event'] == 'hook_preview.cleanup_failed' for r in records(root))
    finally:
        app.shutdown(); app.server_close()


def test_error_diagnostic_number_tolerates_partial_error_info(diag, app, monkeypatch):
    from exactcut.webserver import Handler
    from test_web_base_path import request
    monkeypatch.setattr(Handler, 'do_GET', lambda self: self.respond(400, {
        'error': 'fixture error', 'error_info': {'title': 'fixture', 'action': 'retry'}}))
    status, headers, raw = request(app, 'GET', '/home/')
    body = json.loads(raw)
    assert status == 400 and headers['X-Request-ID'] in body['error_info']['detail']


def test_browser_events_require_auth_and_accept_only_metadata(diag, app):
    from test_web_base_path import request, login
    d, root = diag
    assert request(app, 'POST', '/home/api/client-events', {'event': 'page_error'})[0] == 401
    headers = login(app)
    assert request(app, 'POST', '/home/api/client-events', {'event': 'page_error', 'source': 'app.js', 'line': 42}, headers)[0] == 200
    assert request(app, 'POST', '/home/api/client-events', {'event': 'page_error', 'source': 'https://host?token=private'}, headers)[0] == 400
    assert any(r['event'] == 'browser.event' and r['browser']['line'] == 42 for r in records(root))
    status, _, body = request(app, 'GET', '/home/', headers=headers)
    assert status == 200 and body.index(b'src="diagnostics.js?v=') < body.index(b'src="app.js?v=')


def test_query_filters_by_batch_and_errors(diag):
    d, root = diag
    d.event('failed', level='error', batch_id='one')
    d.event('success', batch_id='one')
    d.event('other', level='error', batch_id='two')
    result = subprocess.run([sys.executable, '-m', 'exactcut.diagnostics', '--root', str(root),
                             '--batch', 'one', '--errors'], capture_output=True, text=True, check=True)
    assert [json.loads(line)['event'] for line in result.stdout.splitlines()] == ['failed']


def test_logging_does_not_follow_directory_symlink(tmp_path):
    d = module()
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'logs').symlink_to(outside, target_is_directory=True)
    d.configure(root)
    try:
        d.event('must_not_escape')
        assert not list(outside.iterdir())
    finally:
        d.disable()


def test_browser_script_preserves_fetch_failure_and_reports_no_form_data():
    import shutil
    node = shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    script = r'''
const vm=require('vm'), fs=require('fs'), assert=require('assert');
const events={}, sent=[];
const error=new TypeError('private error text');
const window={addEventListener:(name, fn)=>events[name]=fn, fetch:async(url, options)=>{
  if(String(url).endsWith('client-events')) {sent.push(JSON.parse(options.body));return {ok:true};}
  throw error;
}};
const document={baseURI:'http://localhost/home/',addEventListener:()=>{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window,document,URL,performance});
(async()=>{
  await assert.rejects(window.fetch('api/jobs',{body:'password=private-form'}),e=>e===error);
  events.error({filename:'http://localhost/home/app.js?secret=private-query',lineno:12,colno:3,error});
  assert(sent.some(x=>x.event==='network_error'));
  assert(sent.some(x=>x.source==='app.js' && x.line===12));
  assert(!JSON.stringify(sent).includes('private'));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    path = Path(__file__).resolve().parents[1] / 'exactcut/web/diagnostics.js'
    subprocess.run([node, '-e', script, str(path)], check=True, capture_output=True, text=True)


def test_browser_context_current_buttons_media_and_renewable_budget():
    import shutil
    node = shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    script = r'''
const vm=require('vm'), fs=require('fs'), assert=require('assert');
const events={}, sent=[], calls=[]; let now=0;
class Clock extends Date {static now(){return now;}}
const response={ok:false,status:429,headers:new Headers({'X-Request-ID':'a'.repeat(32)})};
const window={addEventListener:(n,fn)=>events['window.'+n]=fn,fetch:async(url,options)=>{
  if(String(url).endsWith('client-events')){sent.push(JSON.parse(options.body));return {ok:true};}
  calls.push([url,options]); return response;
}};
const document={baseURI:'http://localhost/home/',visibilityState:'visible',
  currentScript:{src:'http://localhost/home/diagnostics.js?v='+'d'.repeat(20)},
  querySelector:()=>({dataset:{batch:'b'.repeat(32)}}),
  addEventListener:(n,fn)=>events[n]=fn};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),
  {window,document,URL,performance,Headers,Request,crypto:require('crypto').webcrypto,Date:Clock});
(async()=>{
  const button={id:'saveButton',dataset:{},disabled:false};
  events.click({target:{closest:()=>button}});
  assert(sent.some(x=>x.event==='action'&&x.action==='saveButton'&&x.batch_id==='b'.repeat(32)));
  events.click({target:{closest:()=>({...button,id:'startNow'})}});
  assert(sent.some(x=>x.event==='action'&&x.action==='startNow'));
  const req=new Request('http://localhost/home/api/batches/'+'b'.repeat(32)+'/hook-preview?secret=private-query',
    {method:'POST',headers:{'X-Exactcut-Request':'1'},body:'private-form'});
  assert.strictEqual(await window.fetch(req),response);
  const hdr=new Headers(calls[0][1].headers);
  assert.strictEqual(hdr.get('X-Exactcut-Request'),'1');
  const id=hdr.get('X-Client-Request-ID');assert(/^[a-f0-9]{32}$/.test(id));
  assert(sent.some(x=>x.http_trace_id==='a'.repeat(32)&&x.client_request_id===id&&x.route==='batches.hook-preview'));
  assert.strictEqual(req.bodyUsed,false);
  assert.strictEqual(calls[0][0],req);
  const reconstructed=new Request(calls[0][0],calls[0][1]);
  assert.strictEqual(reconstructed.method,'POST');
  assert.strictEqual(await reconstructed.text(),'private-form');
  const controller=new AbortController(), body='private-second-body';
  await window.fetch('api/batches/'+ 'b'.repeat(32) +'/save',{method:'POST',body,signal:controller.signal});
  assert.strictEqual(calls[1][1].body,body);
  assert.strictEqual(calls[1][1].signal,controller.signal);
  assert.strictEqual(calls[1][1].method,'POST');
  events.error({target:{tagName:'VIDEO',dataset:{previewRequest:'R123'},readyState:0,networkState:3,error:{code:4},currentTime:0}});
  assert(sent.some(x=>x.event==='media_error'&&x.request_id==='R123'&&x.media_error===4));
  const video={tagName:'VIDEO',id:'mosaicSource',dataset:{previewRequest:'R456',previewBatch:'c'.repeat(32),previewSession:'e'.repeat(32)},readyState:2,networkState:2,error:null,currentTime:12.5,paused:true,seeking:true,ended:false,src:'private-video-url'};
  events.waiting({target:video});
  assert(sent.some(x=>x.event==='media_waiting'&&x.source==='mosaicSource'&&x.batch_id==='c'.repeat(32)&&x.session_id==='e'.repeat(32)&&x.current_ms===12500&&x.seeking===1));
  const context={batch:'c'.repeat(32),asset:{request_id:'R456'},session:{id:'e'.repeat(32)}};
  video.dataset.previewSession='f'.repeat(32);
  window.reportMediaPlayFailure(video,{name:'AbortError',message:'private-error-url'},context);
  assert(sent.some(x=>x.event==='media_play_rejection'&&x.error_type==='AbortError'&&x.session_id==='e'.repeat(32)&&x.paused===1));
  window.reportMediaPlayFailure(null,{},null); // Diagnostics must never interrupt the UI.
  for(let i=0;i<50;i++)events.click({target:{closest:()=>button}});
  const count=sent.length;now=61000;
  events.click({target:{closest:()=>button}});
  assert(sent.length>count,'reports must resume after the minute budget resets');
  assert(!JSON.stringify(sent).includes('private'));
  assert(sent.every(x=>x.page_id&&x.frontend_build==='d'.repeat(20)));
  const before=sent.length;await window.fetch('https://external.invalid/api/private');
  assert.strictEqual(sent.length,before);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    path = Path(__file__).resolve().parents[1] / 'exactcut/web/diagnostics.js'
    subprocess.run([node, '-e', script, str(path)], check=True, capture_output=True, text=True)


def test_editor_media_diagnostic_fields_are_bounded():
    from exactcut import diagnostics
    fields = {'session_id': 'a'*32, 'paused': 1, 'seeking': 0, 'ended': 0}
    assert diagnostics.browser_fields(fields) == fields
    with pytest.raises(ValueError):
        diagnostics.browser_fields({'session_id': 'https://private.invalid/video'})
    with pytest.raises(ValueError):
        diagnostics.browser_fields({'seeking': float('nan')})


def test_play_rejection_http_preserves_session_and_warning(diag, app):
    from test_web_base_path import request, login
    _, root = diag
    headers = login(app)
    for sid in ('a'*32, 'b'*32):
        payload = {'event': 'media_play_rejection', 'source': 'mosaicSource',
                   'batch_id': 'c'*32, 'request_id': 'R456', 'session_id': sid,
                   'error_type': 'AbortError', 'ready_state': 2, 'network_state': 2,
                   'media_error': 0, 'paused': 1, 'seeking': 1, 'ended': 0,
                   'current_ms': 12500, 'message': 'private-video-url'}
        assert request(app, 'POST', '/home/api/client-events', payload, headers)[0] == 200
    rows = [r for r in records(root) if r['event'] == 'browser.event']
    assert len(rows) == 2
    assert {r['browser']['session_id'] for r in rows} == {'a'*32, 'b'*32}
    assert all(r['level'] == 'warning' for r in rows)
    assert 'private-video-url' not in json.dumps(rows)


def test_private_span_keeps_provider_code_without_response(monkeypatch):
    from exactcut import diagnostics
    from google.genai.errors import ClientError
    import pytest
    logged=[]
    monkeypatch.setattr(diagnostics,'event',lambda name,**fields:logged.append((name,fields)))
    exc=ClientError(429,{'error':{'message':'private-provider-body','status':'RESOURCE_EXHAUSTED'}})
    with pytest.raises(ClientError):
        with diagnostics.span('scan.model',private_error=True):
            raise exc
    name,fields=logged[-1]
    assert name=='scan.model.failed'
    assert fields['error']['code']==429
    assert fields['error']['type']=='ClientError'
    assert fields['error']['frames']
    assert 'private-provider-body' not in str(logged)
    assert fields['duration_ms']>=0
