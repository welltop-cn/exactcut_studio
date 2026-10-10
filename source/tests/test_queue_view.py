import json
from exactcut import accounts, webserver as web
from exactcut.util import write_json_atomic


def test_queue_exposes_only_global_counts_and_authorized_details(tmp_path, monkeypatch):
    batches = tmp_path / 'batches'
    monkeypatch.setattr(web.workflow, 'BATCHES', batches)
    monkeypatch.setattr(web.concurrency, 'settings', lambda: {'fetch': 2, 'render': 3, 'per_project': 1, 'enabled': True, 'max_active_children': 7})
    jobs=[]
    for bid, owner, state in [('a'*32,'alice','running'),('b'*32,'bob','queued')]:
        path=batches/bid
        write_json_atomic(path/'draft.json', {'display_name': owner+' private project'})
        accounts.assign(path, {'username':owner})
        write_json_atomic(path/'runtime.json', {'status':'producing'})
        jobs.append({'id': owner+'-job', 'batch_id':bid, 'request_id':owner+'-asset', 'lane':'render', 'status':state,'created':10,'started':20,'command':['secret command'],'actor':'spoofed'})
    jobs.append({'id':'upload','batch_id':'a'*32,'lane':'upload','status':'running'})
    monkeypatch.setattr(web, 'stored_jobs', lambda: iter(jobs))
    result=web.queue_snapshot({'username':'alice','role':'user'})
    assert result['pools']['render']=={'running':1,'queued':1,'paused':0,'limit':3}
    assert len(result['tasks'])==1
    assert result['tasks'][0]['request_id']=='alice-asset'
    assert 'bob' not in json.dumps(result)
    assert 'secret' not in json.dumps(result)
    assert 'queue_position' not in json.dumps(result)
    assert len(web.queue_snapshot({'username':'admin','role':'admin'})['tasks'])==2
    write_json_atomic(batches/('b'*32)/'control.json', {'paused':True})
    paused=web.queue_snapshot({'username':'admin','role':'admin'})
    assert paused['pools']['render']['queued']==0
    assert paused['pools']['render']['paused']==1
    assert next(t for t in paused['tasks'] if t['request_id']=='bob-asset')['status']=='paused'


def test_queue_endpoint_requires_login_and_uses_authenticated_user(monkeypatch):
    import base64
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.password = 'queue-fixture-password'
    monkeypatch.setattr(web, 'queue_snapshot', lambda user: {'scope': user['role'], 'tasks': [], 'pools': {}})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection(*server.server_address)
        conn.request('GET', '/api/queue')
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        auth = 'Basic ' + base64.b64encode(b'admin:queue-fixture-password').decode()
        conn.request('GET', '/api/queue', headers={'Authorization': auth})
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['scope'] == 'admin'
    finally:
        server.shutdown()
        server.server_close()
