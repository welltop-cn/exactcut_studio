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
