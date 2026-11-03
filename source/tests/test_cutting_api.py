import base64
import json
import time
from http.client import HTTPConnection

import pytest
from exactcut import cutting, workflow as w, webserver as web
from exactcut.util import write_json_atomic, file_sha256
from test_phase2_preview_api import fixture, serve
from test_cutting import make_video, frame_numbers


def call(app, method, suffix, body=None, auth=True, csrf=True):
    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Authorization'] = 'Basic '+base64.b64encode(('admin:'+app.password).encode()).decode()
    if csrf:
        headers['X-Exactcut-Request'] = '1'
    conn = HTTPConnection(*app.server_address, timeout=30)
    try:
        conn.request(method, suffix, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        data = response.read()
        return response.status, json.loads(data) if 'json' in response.getheader('Content-Type', '') else data
    finally:
        conn.close()


def prepared(app,base,value):
    """Follow the public asynchronous snapshot receipt, without hiding the first status."""
    deadline=time.monotonic()+6
    while value.get('status') in {'queued','running','cancelling'}:
        assert time.monotonic()<deadline
        time.sleep(.01);code,value=call(app,'GET',base+'/preparation/'+value['id']);assert code==200
    if value.get('status'):
        assert value['status']=='completed',value
        return value['result']['session']
    return value


def exact_frame(app,url):
    deadline=time.monotonic()+6
    while True:
        code,data=call(app,'GET',url)
        if code!=202:return code,data
        assert time.monotonic()<deadline;time.sleep(.01)


@pytest.fixture
def ready(fixture, tmp_path, monkeypatch):
    config = tmp_path/'cutting-config.json'
    write_json_atomic(config, {'cutting': {'enabled': True}, 'storage': {'reserve_gib': 0.01}})
    monkeypatch.setenv('EXACTCUT_CONFIG_FILE', str(config))
    w.start_batch(fixture['id'], fixture['revision'])
    batch = w.folder(fixture['id'])
    _, request = w.request_for(batch, fixture['assets'][0]['request_id'])
    source = batch/'production/videos'/request.output_filename
    source.parent.mkdir(parents=True, exist_ok=True)
    make_video(source)
    sha = file_sha256(source)
    write_json_atomic(batch/'production/results.json', {'requests': {
        request.request_id: {'status': 'qc_passed', 'output_sha256': sha, 'duration_ms': 3000}}})
    w.update(batch, 'review', 'fixture ready')
    return fixture['id'], request.request_id, sha, source


@pytest.mark.integration
def test_access_and_source_identity(ready):
    bid, rid, sha, source = ready
    app = serve()
    route = f'/api/batches/{bid}/cuts/open'
    body = {'request_id': rid, 'source_sha256': sha}
    try:
        assert call(app, 'POST', route, body, auth=False)[0] == 401
        assert call(app, 'POST', route, body, csrf=False)[0] == 403
        assert call(app, 'POST', route, {**body, 'source_sha256': 'f'*64})[0] == 409
        status, session = call(app, 'POST', route, body)
        assert status in {200,202}
        session=prepared(app,route.removesuffix('/open'),session)
        assert session['metadata']['frame_count'] == 90
        assert call(app, 'POST', route, body)[1]['id'] == session['id']
        other = w.create_blank()
        assert call(app, 'GET', f"/api/batches/{other['id']}/cuts/{session['id']}")[0] == 404
        assert call(app, 'GET', f"/api/batches/{bid}/cuts/{session['id']}/frame/90")[0] == 400
        status, image = exact_frame(app,f"/api/batches/{bid}/cuts/{session['id']}/frame/10")
        assert status == 200 and image.startswith(b'\xff\xd8')
        assert file_sha256(source) == sha
    finally:
        app.shutdown(); app.server_close()


@pytest.mark.integration
def test_render_save_reopen_and_source_immutable(ready):
    bid, rid, sha, source = ready
    app = serve()
    base = f'/api/batches/{bid}/cuts'
    try:
        status, session = call(app, 'POST', base+'/open', {'request_id': rid, 'source_sha256': sha})
        assert status in {200,202}
        session=prepared(app,base,session)
        path = base+'/'+session['id']
        assert call(app, 'POST', path+'/render', {'ranges': [[0, 90]]})[0] == 400
        status, job = call(app, 'POST', path+'/render', {'ranges': [[10, 11], [30, 40]]})
        assert status == 202
        assert call(app, 'POST', path+'/render', {'ranges': [[10, 11], [30, 40]]})[1]['id'] == job['id']
        for _ in range(100):
            state = call(app, 'GET', path)[1]
            job = next(j for j in state['jobs'] if j['id'] == job['id'])
            if job['status'] != 'running': break
            time.sleep(.1)
        assert job['status'] == 'ready', job
        assert job['expected_frames'] == 79
        assert call(app, 'POST', path+'/save', {'job_id': job['id']})[1]['status'] == 'saved'
        assert call(app, 'POST', path+'/save', {'job_id': job['id']})[1]['status'] == 'saved'
        status, binary = call(app, 'GET', path+'/output/'+job['id'])
        assert status == 200 and len(binary) > 1000
        assert call(app, 'GET', base+'/saved/'+rid)[1]['versions'][0]['job']['id'] == job['id']
        assert call(app, 'GET', path)[1]['jobs'][0]['status'] == 'saved'
        assert file_sha256(source) == sha
        assert w.preview_file(bid, rid)[0] == source
    finally:
        app.shutdown(); app.server_close()


@pytest.mark.integration
def test_mosaic_placeholder_rejected_by_api(ready):
    bid, rid, sha, _source = ready
    app = serve()
    try:
        session = call(app, 'POST', f'/api/batches/{bid}/cuts/open', {'request_id': rid, 'source_sha256': sha})[1]
        session=prepared(app,f'/api/batches/{bid}/cuts',session)
        path = f"/api/batches/{bid}/cuts/{session['id']}"
        box = {'start': 10, 'end': 20, 'x': 16, 'y': 16, 'w': 32, 'h': 32}
        status, result = call(app, 'POST', path+'/render', {'ranges': [], 'mosaics': [box]})
        assert status == 422 and '暂未开放' in result['error']
        assert call(app, 'GET', path)[1]['jobs'] == []
    finally:
        app.shutdown(); app.server_close()


@pytest.mark.integration
def test_disabled_feature(ready, monkeypatch):
    monkeypatch.setattr(cutting, 'settings', lambda: {'enabled': False})
    app = serve()
    try:
        assert call(app, 'POST', f'/api/batches/{ready[0]}/cuts/open', {})[0] == 404
    finally:
        app.shutdown(); app.server_close()


@pytest.mark.integration
def test_busy_failure_recovery_and_delete_lease(ready, monkeypatch):
    import threading
    from exactcut import batch_control
    bid, rid, sha, original = ready
    data = cutting.open_session(bid, rid, sha)
    sid = data['id']
    entered, finish = threading.Event(), threading.Event()
    real_render = cutting.render_video
    def failing(*args):
        entered.set()
        assert finish.wait(5)
        raise ValueError('private internal path')
    monkeypatch.setattr(cutting, 'render_video', failing)
    first = cutting.start_render(bid, sid, [[10, 11]])
    try:
        assert entered.wait(2)
        assert cutting.start_render(bid, sid, [[10, 11]])['id'] == first['id']
        with pytest.raises(cutting.CutError) as error:
            cutting.start_render(bid, sid, [[20, 21]])
        assert error.value.status == 429
        with pytest.raises(cutting.CutError):
            with cutting.batch_lease(bid, exclusive=True):
                pytest.fail('deletion must wait for active cutting')
    finally:
        finish.set()
    for _ in range(100):
        data = cutting.get_session(bid, sid)
        if data['jobs'][0]['status'] == 'failed': break
        time.sleep(.05)
    assert data['jobs'][0]['status'] == 'failed'
    assert 'private' not in data['jobs'][0]['error']
    assert original.is_file() and file_sha256(original) == sha
    # A recorded running job without a live lock simulates restart. POST itself recovers it.
    data['jobs'][0]['status'] = 'running'
    write_json_atomic(cutting.session_path(bid, sid)/'session.json', data)
    monkeypatch.setattr(cutting, 'render_video', real_render)
    retried = cutting.start_render(bid, sid, [[10, 11]])
    assert retried['status'] == 'running'
    for _ in range(200):
        data = cutting.get_session(bid, sid)
        if data['jobs'][0]['status'] != 'running': break
        time.sleep(.05)
    assert data['jobs'][0]['status'] == 'ready'
    batch_control.write(w.folder(bid), {'archived': True, 'paused': True})
    w.delete_batch(bid, bid)
    assert not (w.BATCHES/bid).exists()


@pytest.mark.parametrize('environment', ['development', 'test', 'production'])
def test_feature_requires_explicit_config(environment, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', environment)
    for settings, expected in [({}, False), ({'enabled': False}, False), ({'enabled': 'true'}, False), ({'enabled': True}, True)]:
        monkeypatch.setattr(cutting, 'settings', lambda: settings)
        assert cutting.enabled() is expected


@pytest.mark.integration
def test_discard_fences_stale_window_mutations(ready):
    bid, rid, sha, _ = ready
    app = serve()
    try:
        _, session = call(app, 'POST', f'/api/batches/{bid}/cuts/open', {'request_id': rid, 'source_sha256': sha})
        session=prepared(app,f'/api/batches/{bid}/cuts',session)
        route = f"/api/batches/{bid}/cuts/{session['id']}"
        code, reset = call(app, 'POST', route+'/discard', {})
        assert code == 200 and reset['reset_at']
        for action, body in [('render', {'ranges': []}), ('discard', {}), ('scan', {'force': True})]:
            code, error = call(app, 'POST', route+'/'+action, body)
            assert code == 409, (action, code, error)
            assert '重新打开' in error['error']
        assert call(app, 'POST', route+'/discard', {'reset_at': None})[0] == 409
        assert call(app, 'POST', route+'/render', {'ranges': [], 'reset_at': reset['reset_at']})[0] == 400
        assert call(app, 'POST', route+'/discard', {'reset_at': reset['reset_at']})[0] == 200
    finally:
        app.shutdown(); app.server_close()


@pytest.mark.integration
@pytest.mark.parametrize('kind', ['cuts', 'mosaics'])
def test_source_stream_works_while_compute_and_frame_locks_busy(ready, kind):
    bid, rid, sha, source = ready
    app = serve()
    base = f'/api/batches/{bid}/{kind}'
    try:
        status, session = call(app, 'POST', base+'/open', {
            'request_id': rid, 'source_sha256': sha})
        assert status in {200,202}
        session=prepared(app,base,session)
        with cutting.lock_file(cutting.lock_root()/'compute.lock'), \
                cutting.lock_file(cutting.lock_root()/'frame.lock'):
            status, video = call(app, 'GET', base+'/'+session['id']+'/source')
        assert status == 200
        assert video == source.read_bytes()
    finally:
        app.shutdown(); app.server_close()

from interactive_test_support import interactive_dispatcher
