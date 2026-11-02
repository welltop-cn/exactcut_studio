"""Admission regressions: final render resources must not serialize interactive users.

Models are explicit test doubles here; independent acceptance uses actual Gemini/SAM.
"""
import base64
import json
import os
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest
from exactcut import accounts,cutting,workflow,mosaic_session as sessions,mosaic_scan,mosaic_locate,mosaic_tracking as tracking,mosaic_render as render,cutting_scan
from exactcut.util import read_json,write_json_atomic,file_sha256
from test_phase2_preview_api import fixture,serve
from test_cutting_api import ready
from test_mosaic_render import make_track


def enable_models(tmp_path,monkeypatch):
    checkpoint=tmp_path/'unit-test-double.checkpoint';checkpoint.write_bytes(b'not-a-real-model')
    config=Path(os.environ['EXACTCUT_CONFIG_FILE']);cfg=read_json(config)
    cfg['cutting'].update(scan={'enabled':True,'model':'gemini-3.8-flash','coarse_fps':2,'max_refine_events':0,'request_timeout_ms':120000},tracking={'enabled':True,'checkpoint':str(checkpoint)})
    write_json_atomic(config,cfg)
    monkeypatch.setenv('EXACTCUT_CUT_SCAN_ENV',str(tmp_path/'unused-unit-credential'))
    monkeypatch.setattr(cutting_scan,'credential',lambda:'unit-test-not-an-api-key')


def terminal(path):
    deadline=time.monotonic()+5
    while path.is_file() and read_json(path)['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
    assert read_json(path)['status'] in ('completed','failed','cancelled')


def test_scan_admission_and_completion_while_final_compute_remains_held(ready,tmp_path,monkeypatch):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch)
    session=sessions.open_session(bid,rid,sha)
    def detect(source,meta,cfg,policy,key,progress,compute,**kwargs):
        if compute is not None:compute.close()
        return {'risk_events':[],'warnings':[]}
    monkeypatch.setattr(cutting_scan,'detect',detect)
    with cutting.lock_file(cutting.lock_root()/'compute.lock'):
        job=mosaic_scan.start(bid,session['id'],'')
        path=sessions.session_path(bid,session['id'])/'scan.json'
        terminal(path)
        assert read_json(path)['status']=='completed'
        assert file_sha256(source)==sha


def test_region_locate_uses_independent_frames_during_final_compute(ready,tmp_path,monkeypatch):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch)
    session=sessions.open_session(bid,rid,sha)
    monkeypatch.setattr(mosaic_locate,'detect',lambda *args,**kwargs:[])
    with cutting.lock_file(cutting.lock_root()/'compute.lock'):
        job=mosaic_locate.start(bid,session['id'],0,6,0,'')
        path=sessions.session_path(bid,session['id'])/f"locate-{job['id']}.json"
        terminal(path)
        assert read_json(path)['status']=='completed'
        assert file_sha256(source)==sha


def test_sam_preview_and_ffmpeg_complete_while_final_compute_remains_held(ready,tmp_path,monkeypatch):
    bid,rid,sha,source=ready;enable_models(tmp_path,monkeypatch)
    session=sessions.open_session(bid,rid,sha)
    monkeypatch.setattr(tracking,'run_tracker',lambda src,directory,meta,target,cfg:make_track(directory,target,meta)['result'])
    target={'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20}
    with cutting.lock_file(cutting.lock_root()/'compute.lock'):
        job=render.start(bid,session['id'],[target],'',effect={'style':'solid'},preview=True)
        path=sessions.session_path(bid,session['id'])/f"render-{job['id']}.json"
        terminal(path)
        done=read_json(path)
        assert done['status']=='completed'
        assert done['result']['metadata']['frame_count']==6
        assert file_sha256(source)==sha


def call_as(app,username,path,body):
    headers={'Content-Type':'application/json','X-Exactcut-Request':'1','Authorization':'Basic '+base64.b64encode((username+':'+app.password).encode()).decode()}
    connection=HTTPConnection(*app.server_address,timeout=10)
    try:
        connection.request('POST',path,json.dumps(body),headers)
        response=connection.getresponse()
        return response.status,json.loads(response.read())
    finally:connection.close()


def another_ready(tmp_path,monkeypatch):
    batch=fixture.__wrapped__(tmp_path,monkeypatch)
    path=workflow.folder(batch['id'])/'draft.json';draft=read_json(path)
    draft['draft']['author']='测试Beta';write_json_atomic(path,draft)
    return ready.__wrapped__(workflow.detail(batch['id']),tmp_path,monkeypatch)


@pytest.mark.parametrize('operation',['scan','locate','clip-preview'])
def test_two_authenticated_users_enter_model_work_concurrently(ready,tmp_path,monkeypatch,operation):
    other=another_ready(tmp_path,monkeypatch);enable_models(tmp_path,monkeypatch)
    app=serve();app.auth_db=workflow.BATCHES.parent/'accounts.sqlite3';accounts.initialize(app.auth_db,app.password)
    profiles=[];finish=threading.Event();both=threading.Event();entered=[];mutex=threading.Lock()
    def barrier():
        with mutex:
            entered.append(time.monotonic())
            if len(entered)==2:both.set()
        assert finish.wait(8),'test worker was not released'
    def detect(source,meta,cfg,policy,key,progress,compute,**kwargs):
        if compute is not None:compute.close()
        barrier();return {'risk_events':[],'warnings':[]}
    def infer(source,directory,meta,target,cfg):
        barrier();return make_track(directory,target,meta)['result']
    def locate(jpeg,meta,cfg,key,**kwargs):
        barrier();return []
    monkeypatch.setattr(cutting_scan,'detect',detect);monkeypatch.setattr(tracking,'run_tracker',infer);monkeypatch.setattr(mosaic_locate,'detect',locate)
    try:
        for username,(bid,rid,sha,source) in zip(('alpha','beta'),(ready,other)):
            accounts.manage(app.auth_db,'admin',{'username':username,'name':username,'password':app.password})
            accounts.assign(workflow.folder(bid),{'username':username})
            session=sessions.open_session(bid,rid,sha)
            target={'start':0,'end':6,'seed_frame':0,'x':10,'y':10,'w':20,'h':20}
            body={'reset_at':''}
            if operation=='locate':body.update(start=0,end=6,seed_frame=0)
            elif operation=='clip-preview':body.update(targets=[target],effect={'style':'solid'})
            status,job=call_as(app,username,f"/api/batches/{bid}/mosaics/{session['id']}/{operation}",body)
            assert status==202,(username,status,job)
            filename='scan.json' if operation=='scan' else f"{'locate' if operation=='locate' else 'render'}-{job['id']}.json"
            profiles.append(sessions.session_path(bid,session['id'])/filename)
        assert both.wait(2),'second user waited for the first entire model operation'
    finally:
        finish.set()
        for path in profiles:terminal(path)
        app.shutdown();app.server_close()
