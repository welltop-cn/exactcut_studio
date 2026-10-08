import base64
import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import pytest
from exactcut import webserver as server, workflow as w, hook_preview
from exactcut.intake import read_excel
from exactcut.util import write_json_atomic


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    monkeypatch.setattr(w,'ROOT',tmp_path)
    monkeypatch.setattr(w,'BATCHES',tmp_path/'.web-state/batches')
    monkeypatch.setattr(w,'FEEDBACK',tmp_path/'feedback')
    monkeypatch.setattr(w,'read_excel',read_excel)
    monkeypatch.setattr(server,'ROOT',tmp_path)
    template=Path(__file__).resolve().parents[1]/'templates/精确剪辑_FBBM_填写模板.xlsx'
    batch=w.import_excel({'filename':'test.xlsx','content':base64.b64encode(template.read_bytes()).decode()})
    stored=w.read_json(w.folder(batch['id'])/'draft.json')
    stored['draft']['author']='测试'
    stored['draft']['rows'][0]['hook_options']={'mode':'none','position':'top'}
    write_json_atomic(w.folder(batch['id'])/'draft.json',stored)
    return w.detail(batch['id'])


def serve():
    app=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    app.password='phase2-local-test-password-only'
    threading.Thread(target=app.serve_forever,daemon=True).start()
    return app


def call(app,method,path,data=None,auth=True,csrf=True):
    headers={}
    if auth:
        headers['Authorization']='Basic '+base64.b64encode(('admin:'+app.password).encode()).decode()
    if csrf:
        headers['X-Exactcut-Request']='1'
    conn=HTTPConnection(*app.server_address,timeout=3)
    try:
        conn.request(method,path,None if data is None else json.dumps(data),headers)
        response=conn.getresponse()
        return response.status,json.loads(response.read())
    finally:
        conn.close()


def test_preview_requires_auth_csrf_access_and_matching_revision(fixture,monkeypatch):
    monkeypatch.setattr(hook_preview,'compute',lambda *a:pytest.fail('invalid requests must not call AI'))
    app=serve()
    path=f"/api/batches/{fixture['id']}/hook-preview"
    body={'revision':fixture['revision'],'index':0}
    try:
        assert call(app,'POST',path,body,auth=False)[0]==401
        assert call(app,'POST',path,body,csrf=False)[0]==403
        assert call(app,'POST','/api/batches/'+'f'*32+'/hook-preview',body)[0]==404
        assert call(app,'POST',path,{**body,'revision':999})[0]==409
        assert call(app,'POST',path,{**body,'index':True})[0]==400
    finally:
        app.shutdown();app.server_close()


def test_long_preview_does_not_block_reads_or_saves_and_rejects_stale_result(fixture,tmp_path,monkeypatch):
    entered,finish=threading.Event(),threading.Event()
    def slow_compute(project,request,work):
        entered.set()
        assert finish.wait(2)
        cache=work/'cache'
        for directory in ('hook-emphasis','hook-placement'):
            (cache/directory).mkdir(parents=True)
        return {'preview_key':'a'*64,'decision':{}},{'identity':{},'decision':{}},cache
    monkeypatch.setattr(hook_preview,'compute',slow_compute)
    app=serve();response=[]
    path=f"/api/batches/{fixture['id']}"
    task=threading.Thread(target=lambda:response.append(call(app,'POST',path+'/hook-preview',{'revision':fixture['revision'],'index':0})))
    try:
        task.start();assert entered.wait(1)
        start=time.monotonic()
        assert call(app,'GET',path)[0]==200
        draft=fixture['draft'];draft['rows'][0]['hook_text']='A changed Hook'
        assert call(app,'POST',path+'/save',{'revision':fixture['revision'],'draft':draft})[0]==200
        assert time.monotonic()-start<1
        finish.set();task.join(3)
        assert response[0][0]==409
        assert not (tmp_path/'projects/101460/cache/hook-previews'/('a'*64+'.json')).exists()
    finally:
        finish.set();task.join(3);app.shutdown();app.server_close()


def test_preview_saves_complete_cache_only_after_successful_recheck(fixture,tmp_path,monkeypatch):
    def computed(project,request,work):
        cache=work/'cache'
        (cache/'hook-emphasis').mkdir(parents=True)
        (cache/'hook-placement').mkdir()
        write_json_atomic(cache/'hook-emphasis'/('b'*64+'.json'),{'test':'complete'})
        return {'preview_key':'a'*64,'decision':{}},{'identity':{},'decision':{}},cache
    monkeypatch.setattr(hook_preview,'compute',computed)
    app=serve()
    try:
        status,result=call(app,'POST',f"/api/batches/{fixture['id']}/hook-preview",{'revision':fixture['revision'],'index':0})
        assert status==200 and result['preview_key']=='a'*64
        assert (tmp_path/'projects/101460/cache/hook-previews'/('a'*64+'.json')).is_file()
        assert w.read_json(tmp_path/'projects/101460/cache/hook-emphasis'/('b'*64+'.json'))=={'test':'complete'}
    finally:
        app.shutdown();app.server_close()
