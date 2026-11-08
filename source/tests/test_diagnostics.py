import json
import os
import shutil
import stat
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


@pytest.mark.parametrize('url, expected', [
    ('https://user:password@media.example/EP25.mp4?Signature=secret&AccessKeyId=key&Expires=123&token=token#private',
     'https://media.example/EP25.mp4'),
    ('http://user:password@[2001:db8::1]:8080/EP25.mp4?x=secret', 'http://[2001:db8::1]:8080/EP25.mp4'),
    ('https://media.example/EP25.mp4', 'https://media.example/EP25.mp4'),
    ('https://[invalid', ''), ('https://media.example:bad/file', ''),
    ('file:///private/file', ''), ('relative?token=secret', ''), (None, ''),
])
def test_redact_upstream_url(url, expected):
    assert module().redact_url(url) == expected


@pytest.mark.parametrize('status', [None, 200])
def test_http_failed_original_context_and_signed_url_redaction(diag, monkeypatch, status):
    from http.server import BaseHTTPRequestHandler
    from io import BytesIO
    from types import SimpleNamespace
    from exactcut import originals, webserver

    d, root = diag
    batch = 'a' * 32
    (root / 'draft.json').write_text(json.dumps({'display_name': '测试项目', 'draft': {'drama_title': '剧名'}}))
    monkeypatch.setattr(webserver.workflow, 'folder', lambda _: root)
    monkeypatch.setattr(BaseHTTPRequestHandler, 'parse_request', lambda _: True)
    signed = 'https://user:private-password@media.example/EP25.mp4?Signature=private-signature&AccessKeyId=private-key&Expires=private-expiry&token=private-token#private-fragment'
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': signed})

    def fail():
        raise ValueError('IncompleteRead ' + signed)

    class Remote:
        status = 200
        headers = {'Content-Length': '100'}
        closed = False

        def iter_bytes(self):
            yield b'partial'
            fail()

        def close(self):
            self.closed = True

    remote = Remote()
    handler = webserver.Handler.__new__(webserver.Handler)
    handler.command = 'GET'
    handler.path = f'/api/batches/{batch}/originals/EP25/file'
    handler.headers = {'X-Client-Request-ID': 'b' * 32}
    handler.server = SimpleNamespace(base_path='')
    handler.wfile = BytesIO()
    handler.send_response = lambda code: setattr(handler, 'diagnostic_status', code)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None

    def request(self):
        assert self.parse_request()
        d.bind(actor='fixture-user')
        def getter(url):
            assert url == signed  # The actual fetch still uses the signed address.
            if status is None:
                fail()
            return remote
        self._send_original(originals.open_original('101606', 'EP25', getter=getter))

    monkeypatch.setattr(BaseHTTPRequestHandler, 'handle_one_request', request)
    with d.context():
        previous = d.current()
        with pytest.raises(ValueError, match='IncompleteRead'):
            handler.handle_one_request()
        assert d.current() == previous
    failed, completed = records(root)
    assert failed['event'] == 'http.failed'
    for row in (failed, completed):
        assert row['method'] == 'GET' and row['status'] == status
        assert isinstance(row['duration_ms'], int) and row['duration_ms'] >= 0
        assert row['project_name'] == '测试项目' and row['batch_id'] == batch
        assert row['upstream_url'] == 'https://media.example/EP25.mp4'
        assert row['client_request_id'] == 'b' * 32
    assert completed['event'] == 'http.completed'
    assert remote.closed is (status == 200)
    text = json.dumps(records(root))
    for secret in ('private-', 'Signature=', 'AccessKeyId=', 'Expires=', 'token=', 'user:'):
        assert secret not in text


@pytest.mark.parametrize('document, expected', [
    ({'draft': {'drama_title': '剧名'}}, '剧名'),
    ({'draft': None}, None), ({'display_name': 123}, None),
    ([], None), ('invalid-json', None), (None, None),
])
def test_request_project_context_is_optional(tmp_path, monkeypatch, document, expected):
    from http.server import BaseHTTPRequestHandler
    from types import SimpleNamespace
    from exactcut import webserver
    d = module()
    if document is not None:
        (tmp_path / 'draft.json').write_text(document if isinstance(document, str) else json.dumps(document))
    monkeypatch.setattr(webserver.workflow, 'folder', lambda _: tmp_path)
    monkeypatch.setattr(BaseHTTPRequestHandler, 'parse_request', lambda _: True)
    handler = webserver.Handler.__new__(webserver.Handler)
    handler.path = '/api/batches/' + 'a' * 32 + '/originals'
    handler.command = 'GET'
    handler.headers = {}
    handler.server = SimpleNamespace(base_path='')
    with d.context():
        assert handler.parse_request()
        assert d.current().get('project_name') == expected


@pytest.mark.parametrize('succeeds', [True, False])
def test_segment_attempt_event_keeps_job_context(diag, succeeds):
    from exactcut import segment_reencode
    d, root = diag
    def render():
        if not succeeds:
            raise ValueError('fixture fallback')
        return 'rendered'
    with d.context(job_id='job-x'):
        result = segment_reencode.attempt(render, {})
    assert result == ('rendered' if succeeds else None)
    row = records(root)[0]
    assert row['event'] == 'cut.segments'
    assert row['job_id'] == 'job-x'
    assert row['used'] is succeeds


@pytest.mark.parametrize('fails', [False, True])
def test_cutting_render_job_scopes_diagnostics_and_releases_locks(diag, monkeypatch, fails):
    from contextlib import nullcontext
    from exactcut import cutting, editor_resources
    from exactcut.util import read_json, write_json_atomic
    d, root = diag
    job = {'id': 'job-x', 'ranges': []}
    write_json_atomic(root / 'session.json', {'jobs': [job]})
    monkeypatch.setattr(cutting, 'file_sha256', lambda path: 'source-hash')
    monkeypatch.setattr(editor_resources, 'final_group', nullcontext)
    def render(*args):
        d.event('cut.segments', used=False, reason='not eligible')
        if fails:
            raise cutting.CutError('fixture failure')
        return {'sha256': 'output-hash'}
    monkeypatch.setattr(cutting, 'render_video', render)
    closed = []
    class Lease:
        def close(self):
            closed.append(d.current().get('job_id'))
    with d.context(job_id='outer-job'):
        cutting._render_job(root, {'source_sha256': 'source-hash', 'metadata': {}}, job, Lease(), Lease())
        assert d.current()['job_id'] == 'outer-job'
    row = next(r for r in records(root) if r['event'] == 'cut.segments')
    assert row['job_id'] == 'job-x'
    assert closed == ['job-x', 'job-x']
    saved = read_json(root / 'session.json')['jobs'][0]
    assert saved['status'] == ('failed' if fails else 'ready')


def test_new_logs_are_private(diag):
    d, root = diag
    d._append(b'{"event":"created"}\n')
    logs = root / 'logs'
    assert logs.stat().st_mode & 0o077 == 0
    for path in [logs / '.writer.lock', *logs.glob('events-*.jsonl')]:
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('writer', ['_append', 'event'])
def test_append_preserves_existing_permissions(diag, writer):
    d, root = diag
    d._append(b'{"event":"created"}\n')
    logs = root / 'logs'
    target = next(logs.glob('events-*.jsonl'))
    logs.chmod(0o750)
    target.chmod(0o640)

    if writer == '_append':
        d._append(b'{"event":"appended"}\n')
    else:
        d.event('appended')

    assert stat.S_IMODE(logs.stat().st_mode) == 0o750
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert [row['event'] for row in records(root)] == ['created', 'appended']


def test_append_preserves_existing_acl(diag):
    setfacl, getfacl = shutil.which('setfacl'), shutil.which('getfacl')
    if not setfacl or not getfacl:
        pytest.skip('setfacl/getfacl unavailable')
    d, root = diag
    d._append(b'{"event":"created"}\n')
    logs = root / 'logs'
    target = next(logs.glob('events-*.jsonl'))
    # A numeric named user need not exist in the local account database.
    reader_uid = os.getuid() + 1
    before = {}
    for path, access in [(logs, 'r-x'), (target, 'r--')]:
        subprocess.run([setfacl, '-m', f'u:{reader_uid}:{access}', str(path)], check=True)
        acl = subprocess.check_output([getfacl, '-c', '-n', '-p', str(path)], text=True)
        assert f'user:{reader_uid}:{access}' in acl
        assert f'mask::{access}' in acl
        before[path] = acl

    d.event('appended')

    for path, acl in before.items():
        assert subprocess.check_output([getfacl, '-c', '-n', '-p', str(path)], text=True) == acl
    assert [row['event'] for row in records(root)] == ['created', 'appended']


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
    from exactcut import media,processing_cancel
    d, root = diag
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(['ffmpeg', 'password=never-log-this'], 3)
    monkeypatch.setattr(processing_cancel, 'run', timeout)
    with pytest.raises(RuntimeError) as error:
        media.run_checked(['ffmpeg', '-i', 'private-input', 'out.mp4'], timeout=3)
    assert isinstance(error.value.__cause__,subprocess.TimeoutExpired)
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


def test_hook_worker_disk_reservation_logs_without_the_old_submit_quota(diag, monkeypatch):
    from exactcut import webserver as web, concurrency, storage
    from exactcut.stage_results import ResourceDeferred
    d, root = diag
    monkeypatch.setattr(web, 'JOBS', root / 'jobs')
    monkeypatch.setattr(web, 'INLINE_RENDERS', set())
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {'hook_previews': 1}})
    with d.context(trace_id='a' * 32, batch_id='b' * 32, actor='producer03'):
        first=web.admit_hook_preview(root/'project')
        try:
            second=web.admit_hook_preview(root/'project')
            web.release_hook_preview(second)
        finally:web.release_hook_preview(first)
    rows=records(root);admitted=[r for r in rows if r['event']=='hook_preview.admitted']
    assert [r['active_previews'] for r in admitted]==[1,2]
    assert all(r['actor']=='producer03' and r['batch_id']=='b'*32 for r in admitted)
    assert [r for r in rows if r['event']=='hook_preview.released'][-1]['active_previews']==0
    assert not any(r['event']=='hook_preview.admission_denied' for r in rows)
    monkeypatch.setattr(storage, 'reserve', lambda *args: False)
    with pytest.raises(ResourceDeferred):web.admit_hook_preview(root/'project')
    assert records(root)[-1]['reason']=='disk_reservation'


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


@pytest.mark.parametrize('outcome,status', [('success','completed'),('error','failed'),('deferred','queued'),('cleanup_error','completed')])
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
        code,job=call(app,'POST',f"/api/batches/{fixture['id']}/hook-preview",{'index':0,'revision':fixture['revision']})
        assert code==202
        from exactcut import interactive_queue
        deadline=time.monotonic()+5
        while True:
            job=hook_preview.state(fixture['id'],job['id'])
            queue_state=interactive_queue.snapshot()
            pending=[row for row in queue_state['pending'] if row['job']['id']==job['id']]
            active=any(row['job']['id']==job['id'] for row in queue_state['active'])
            finished=(bool(pending and pending[0].get('not_before',0)>time.time()) if outcome=='deferred' else job['status'] in {'completed','failed'} and not active)
            if finished:
                job=hook_preview.state(fixture['id'],job['id']);break
            assert time.monotonic()<deadline;time.sleep(.01)
        assert job['status']==status
        if status=='completed':assert job['result']['preview_key']=='a'*64
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
  if(String(url).endsWith('client-events')) {sent.push(...JSON.parse(options.body).events.map(event=>({...JSON.parse(options.body),...event})));return {ok:true};}
  throw error;
}};
const document={baseURI:'http://localhost/home/',addEventListener:()=>{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window,document,URL,performance,setTimeout:()=>1,clearTimeout(){}});
(async()=>{
  await assert.rejects(window.fetch('api/jobs',{body:'password=private-form'}),e=>e===error);
  events.error({filename:'http://localhost/home/app.js?secret=private-query',lineno:12,colno:3,error});
  (events.pagehide||events['window.pagehide'])();
  assert(sent.some(x=>x.event==='network_error'));
  (events.pagehide||events['window.pagehide'])();
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
  if(String(url).endsWith('client-events')){sent.push(...JSON.parse(options.body).events.map(event=>({...JSON.parse(options.body),...event})));return {ok:true};}
  calls.push([url,options]); return response;
}};
const document={baseURI:'http://localhost/home/',visibilityState:'visible',
  currentScript:{src:'http://localhost/home/diagnostics.js?v='+'d'.repeat(20)},
  querySelector:()=>({dataset:{batch:'b'.repeat(32)}}),
  addEventListener:(n,fn)=>events[n]=fn};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),
  {window,document,URL,performance,Headers,Request,crypto:require('crypto').webcrypto,Date:Clock,setTimeout:()=>1,clearTimeout(){}});
(async()=>{
  const button={id:'saveButton',dataset:{},disabled:false};
  events.click({target:{closest:()=>button}});
  (events.pagehide||events['window.pagehide'])();
  assert(sent.some(x=>x.event==='action'&&x.action==='saveButton'&&x.batch_id==='b'.repeat(32)));
  events.click({target:{closest:()=>({...button,id:'startNow'})}});
  (events.pagehide||events['window.pagehide'])();
  assert(sent.some(x=>x.event==='action'&&x.action==='startNow'));
  const req=new Request('http://localhost/home/api/batches/'+'b'.repeat(32)+'/hook-preview?secret=private-query',
    {method:'POST',headers:{'X-Exactcut-Request':'1'},body:'private-form'});
  assert.strictEqual(await window.fetch(req),response);
  const hdr=new Headers(calls[0][1].headers);
  assert.strictEqual(hdr.get('X-Exactcut-Request'),'1');
  const id=hdr.get('X-Client-Request-ID');assert(/^[a-f0-9]{32}$/.test(id));
  (events.pagehide||events['window.pagehide'])();
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
  (events.pagehide||events['window.pagehide'])();
  assert(sent.some(x=>x.event==='media_error'&&x.request_id==='R123'&&x.media_error===4));
  const video={tagName:'VIDEO',id:'mosaicSource',dataset:{previewRequest:'R456',previewBatch:'c'.repeat(32),previewSession:'e'.repeat(32)},readyState:2,networkState:2,error:null,currentTime:12.5,paused:true,seeking:true,ended:false,src:'private-video-url'};
  events.waiting({target:video});
  (events.pagehide||events['window.pagehide'])();
  assert(sent.some(x=>x.event==='media_waiting'&&x.source==='mosaicSource'&&x.batch_id==='c'.repeat(32)&&x.session_id==='e'.repeat(32)&&x.current_ms===12500&&x.seeking===1));
  const context={batch:'c'.repeat(32),asset:{request_id:'R456'},session:{id:'e'.repeat(32)}};
  video.dataset.previewSession='f'.repeat(32);
  window.reportMediaPlayFailure(video,{name:'AbortError',message:'private-error-url'},context);
  (events.pagehide||events['window.pagehide'])();
  assert(sent.some(x=>x.event==='media_play_rejection'&&x.error_type==='AbortError'&&x.session_id==='e'.repeat(32)&&x.paused===1));
  window.reportMediaPlayFailure(null,{},null); // Diagnostics must never interrupt the UI.
  for(let i=0;i<50;i++)events.click({target:{closest:()=>button}});
  events['window.pagehide']();const count=sent.length;now=61000;
  events.click({target:{closest:()=>button}});
  events['window.pagehide']();assert(sent.length>count,'reports must resume after the minute budget resets');
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

from interactive_test_support import interactive_dispatcher


def test_browser_batch_matches_legacy_logs_and_skips_invalid(diag, monkeypatch):
    from io import BytesIO
    from exactcut import webserver
    d, root = diag
    monkeypatch.setattr(webserver, 'maintenance_file', lambda: root / 'absent')
    headers = {}
    app = None
    def request(app, method, path, payload, headers):
        handler = webserver.Handler.__new__(webserver.Handler)
        handler.path = '/api/client-events'
        handler.user = {'username': 'fixture'}
        handler.authorized = lambda: True
        raw = json.dumps(payload).encode()
        handler.headers = {'Content-Length': str(len(raw)), 'X-Exactcut-Request': '1'}
        handler.rfile = BytesIO(raw)
        result = []
        handler.respond = lambda status, body: result.append((status, body))
        handler.handle_post()
        return result[0]
    logged = []
    monkeypatch.setattr(d, 'throttled', lambda *args, **kwargs: logged.append((args, kwargs)))
    common = {'page_id': 'a' * 32, 'frontend_build': 'test'}
    events = [{'event': 'action', 'action': 'saveButton', 'message': 'ignored'},
              {'event': 'page_error', 'source': 'app.js', 'line': 12}]
    for event in events:
        assert request(app, 'POST', '/home/api/client-events', {**common, **event}, headers)[0] == 200
    expected = logged[:]
    logged.clear()
    payload = {**common, 'events': [events[0], None, {'event': 'unknown'},
                                  {'event': 'page_error', 'source': 'invalid/value'}, events[1]]}
    assert request(app, 'POST', '/home/api/client-events', payload, headers)[0] == 200
    assert logged == expected
    assert request(app, 'POST', '/home/api/client-events', {**common, 'events': events * 26}, headers)[0] == 400
    assert request(app, 'POST', '/home/api/client-events', {**common, 'events': [], 'padding': 'x' * 65536}, headers)[0] == 400


def test_browser_buffers_flushes_and_never_retries():
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const listeners={},timers=new Map(),sent=[];let next=0;
const window={addEventListener:(n,f)=>listeners[n]=f,fetch:(url,opts)=>{sent.push(opts);return Promise.reject(Error('offline'))}};
const document={baseURI:'https://example.invalid/',visibilityState:'visible',addEventListener:(n,f)=>listeners[n]=f};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8').replace("report({event: 'page_ready'","window.testReport=report; report({event: 'page_ready'"),{window,document,URL,performance,
 setTimeout:(f,ms)=>{assert.equal(ms,3000);timers.set(++next,f);return next},clearTimeout:id=>timers.delete(id)});
const emit=()=>window.reportEditorRetry({action:'retry_scheduled',source:'test'});
assert.equal(sent.length,0);assert.equal(timers.size,1);
let timer=[...timers.values()][0];timer();assert.equal(sent.length,1);
assert.equal(JSON.parse(sent[0].body).events[0].event,'page_ready');
for(let i=0;i<20;i++)emit();assert.equal(sent.length,2);assert.equal(JSON.parse(sent[1].body).events.length,20);
emit();document.visibilityState='hidden';listeners.visibilitychange();
assert.equal(sent.length,3);assert.equal(JSON.parse(sent[2].body).events.length,2);
emit();listeners.pagehide();listeners.pagehide();assert.equal(sent.length,4);
const all=sent.flatMap(o=>JSON.parse(o.body).events);assert.equal(all.length,24);
assert(sent.every(o=>o.keepalive&&Buffer.byteLength(o.body)<=60*1024));
// Exercise byte splitting with deliberately oversized synthetic metadata.
for(let i=0;i<20;i++)window.testReport({event:'page_error',source:'字'.repeat(1500)});
assert.equal(sent.slice(4).flatMap(o=>JSON.parse(o.body).events).length,20);
assert(sent.length>5,'large batches must split');
assert(sent.every(o=>Buffer.byteLength(o.body)<=60*1024));
'''
    path = Path(__file__).resolve().parents[1] / 'exactcut/web/diagnostics.js'
    result = subprocess.run(['node', '-e', script, str(path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
