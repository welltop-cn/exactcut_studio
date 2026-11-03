"""Resource busy is an explicit admission contract, never a generic 429."""
import io
import json
from contextlib import ExitStack
import pytest
from exactcut import cutting, cutting_http, mosaic_http, webserver, editor_resources


@pytest.mark.parametrize('resource', ['compute', 'scan', 'frame'])
def test_admission_contention_is_typed_and_generic_lock_stays_generic(tmp_path, resource):
    path = tmp_path / (resource + '.lock')
    with cutting.lock_file(path):
        with pytest.raises(cutting.CutError) as generic:
            cutting.lock_file(path)
        assert type(generic.value) is cutting.CutError
        with pytest.raises(cutting.ResourceBusy) as busy:
            cutting.lock_file(path, resource=resource)
        assert busy.value.resource == resource
        assert busy.value.status == 429


def test_frame_timeout_is_typed(tmp_path):
    path = tmp_path / 'frame.lock'
    with cutting.lock_file(path):
        with pytest.raises(cutting.ResourceBusy) as busy:
            with cutting.frame_lease(path, timeout=0):
                pytest.fail('must not admit frame work')
    assert busy.value.resource == 'frame'
    with cutting.frame_lease(path, timeout=0):
        pass


@pytest.mark.parametrize('adapter', [cutting_http, mosaic_http])
@pytest.mark.parametrize('typed', [True, False])
def test_adapter_response_preserves_contract_headers_and_trace(monkeypatch, adapter, typed):
    class Handler:
        respond = webserver.Handler.respond
        headers = {'X-Exactcut-Request': '1', 'Content-Length': '2'}
        rfile = io.BytesIO(b'{}')
        wfile = io.BytesIO()
        trace_id = 'test-trace'
        def authorized(self): return True
        def can_access(self, bid): return True
        def send_response(self, code): self.status = code
        def send_header(self, key, value): self.response_headers[key] = value
        def end_headers(self): pass
    handler = Handler()
    handler.response_headers = {}
    monkeypatch.setattr(cutting, 'enabled', lambda: True)
    monkeypatch.setattr(adapter, 'maintenance_file', lambda: __import__('pathlib').Path('/nonexistent-test-maintenance'))
    error = cutting.ResourceBusy('compute') if typed else cutting.CutError('素材正在处理，请稍后重试', 429)
    def fail(*args, **kwargs): raise error
    monkeypatch.setattr(cutting, 'batch_lease', fail)
    adapter.handle(handler, 'batch', 'open', 'POST')
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == 429
    assert body['error'] == str(error)
    assert body['trace_id'] == 'test-trace'
    if typed:
        assert body['code'] == body['error_info']['code'] == 'editor_resource_busy'
        assert body['resource'] == 'compute' and body['retryable'] is True
        assert body['retry_after_ms'] == 1000
        assert handler.response_headers['Retry-After'] == '1'
    else:
        assert 'code' not in body and 'retryable' not in body
        assert 'Retry-After' not in handler.response_headers


@pytest.mark.parametrize('value', [True, '1\r\nInjected: yes', 0, -1, 3601, 1.5])
def test_retry_after_rejects_non_integer_or_unbounded_headers(value):
    with pytest.raises(ValueError, match='retry_after'):
        webserver.Handler.respond(object(), 429, {'error': 'busy'}, retry_after=value)


# These fixtures use isolated synthetic videos and fake provider credentials.
from test_cutting_api import ready
from test_cutting_scan import configured
from test_phase2_preview_api import fixture


@pytest.mark.parametrize('kind', ['cuts', 'mosaics'])
@pytest.mark.parametrize('resource', ['scan', 'compute'])
def test_busy_scan_is_persisted_without_waiting_request_threads_or_execution_leases(configured, monkeypatch, kind, resource):
    from exactcut import cutting_scan, mosaic_scan, mosaic_session
    bid, sid, _ = configured
    if kind == 'mosaics':
        cut = cutting.read_session(bid, sid)
        sid = mosaic_session.open_session(bid, cut['request_id'], cut['source_sha256'])['id']
        path = mosaic_session.session_path(bid, sid) / 'scan.json'
        start = lambda: mosaic_scan.start(bid, sid, '')
    else:
        session = cutting.read_session(bid, sid)
        identifier = cutting_scan.identity(session, 'standard', cutting_scan.settings())
        path = cutting.session_path(bid, sid) / f'scan-{identifier}.json'
        start = lambda: cutting_scan.start(bid, sid, 'standard')
    def unexpected(*a, **k): pytest.fail('HTTP admission must not create a waiting worker thread')
    monkeypatch.setattr(cutting_scan.threading.Thread, 'start', unexpected)
    with ExitStack() as leases:
        role='scan' if resource=='scan' else 'preprocess'
        for actor in ('busy-alpha','busy-beta'):leases.enter_context(editor_resources.acquire(role,actor))
        job=start()
        from exactcut import interactive_queue
        from exactcut.util import read_json
        assert job['status']=='queued' and read_json(path)['id']==job['id']
        assert any(row['job']['id']==job['id'] for row in interactive_queue.snapshot()['pending'])
        assert not interactive_queue.snapshot()['active']
        with cutting.lock_file(path.with_suffix('.worker.lock')):pass
        if resource == 'compute':
            with editor_resources.acquire('scan','admin'):
                pass


@pytest.mark.parametrize('kind', ['cuts', 'mosaics'])
def test_prepare_diagnostics_include_safe_phases(ready, monkeypatch, kind):
    from exactcut import diagnostics, mosaic_session
    bid, rid, sha, source = ready
    events = []
    monkeypatch.setattr(diagnostics, 'event', lambda name, **fields: events.append((name, fields)))
    module = cutting if kind == 'cuts' else mosaic_session
    result = module.open_session(bid, rid, sha)
    assert result['schema_version'] == 1
    phases = ['copy', 'hash', 'probe_basic', 'probe_timestamps']
    for phase in phases:
        fields = next(fields for name, fields in events if name == f'editor.prepare.{phase}.completed')
        assert fields['duration_ms'] >= 0
    assert any(name == 'editor.open.completed' for name, _ in events)
    assert str(source) not in str(events) and sha not in str(events)


@pytest.mark.parametrize('resource', ['frame', 'compute', 'scan'])
def test_cross_process_capacity_contention(tmp_path, resource):
    import subprocess
    import sys
    path = tmp_path / (resource + '.lock')
    child = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); sys.stdin.read(1)',
        str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(cutting.ResourceBusy):
            cutting.lock_file(path, resource=resource)
        with pytest.raises(cutting.CutError) as generic:
            cutting.lock_file(path)
        assert type(generic.value) is cutting.CutError
    finally:
        child.communicate('x', timeout=5)
    with cutting.lock_file(path, resource=resource):
        pass


def test_asset_and_batch_gates_never_advertise_retry(tmp_path):
    from exactcut import cutting_access
    batch = tmp_path / 'batches' / 'one'
    with cutting_access.gate(batch, 'asset'):
        with pytest.raises(cutting.CutError) as error:
            with cutting_access.gate(batch, 'asset'): pass
        assert type(error.value) is cutting.CutError and error.value.status == 429
    with cutting_access.gate(batch):
        with pytest.raises(cutting.CutError) as error:
            with cutting_access.gate(batch, 'asset'): pass
        assert type(error.value) is cutting.CutError and error.value.status == 409


@pytest.mark.parametrize('kind', ['cuts', 'mosaics'])
@pytest.mark.parametrize('resource', ['worker', 'batch'])
def test_worker_ownership_waits_but_http_batch_mutation_gate_still_rejects(configured, monkeypatch, kind, resource):
    from exactcut import cutting_scan, mosaic_scan, mosaic_session
    bid, sid, _ = configured
    if kind == 'mosaics':
        cut = cutting.read_session(bid, sid)
        sid = mosaic_session.open_session(bid, cut['request_id'], cut['source_sha256'])['id']
        path = mosaic_session.session_path(bid, sid) / 'scan.json'
        start = lambda: mosaic_scan.start(bid, sid, '')
    else:
        session = cutting.read_session(bid, sid)
        identifier = cutting_scan.identity(session, 'standard', cutting_scan.settings())
        path = cutting.session_path(bid, sid) / f'scan-{identifier}.json'
        start = lambda: cutting_scan.start(bid, sid, 'standard')
    lock = path.with_suffix('.worker.lock') if resource == 'worker' else cutting.lock_root() / f'{bid}.lock'
    from test_cutting_api import call
    from test_phase2_preview_api import serve
    app=serve()
    try:
        with cutting.lock_file(lock):
            code,value=call(app,'POST',f'/api/batches/{bid}/{kind}/{sid}/scan',
                {'reset_at':''} if kind=='mosaics' else {'policy':'standard'})
            if resource=='batch':
                assert code==429 and 'code' not in value and 'retryable' not in value
                assert not path.exists()
            else:
                assert code==202 and value['status']=='queued' and path.is_file()
            with editor_resources.acquire('scan','admin'):pass
    finally:app.shutdown();app.server_close()


@pytest.mark.parametrize('module_name', ['cutting_scan', 'mosaic_scan', 'mosaic_locate', 'mosaic_render'])
def test_recovery_uses_worker_liveness_without_retry_classification(tmp_path, module_name):
    from importlib import import_module
    from exactcut.util import write_json_atomic
    module = import_module('exactcut.' + module_name)
    path = tmp_path / 'job.json'
    write_json_atomic(path, {'id': 'a'*32, 'status': 'running'})
    with cutting.lock_file(path.with_suffix('.worker.lock')):
        assert module.recover(path)['status'] == 'running'
    assert module.recover(path)['status'] == 'failed'
