import json
import time
import threading
from fractions import Fraction

import pytest
from exactcut import cutting_scan as scan, cutting
from test_cutting_api import ready, call
from test_phase2_preview_api import fixture, serve

META = {'fps_num': 30000, 'fps_den': 1001, 'frame_count': 90}
EVENT = {'category': '性暗示动作', 'start_ms': 34, 'end_ms': 35, 'evidence_zh': '可见身体动作，需复核'}


def test_checked_events_and_fractional_frame_bounds():
    event = scan.checked_events(json.dumps({'risk_events': [EVENT]}), META)[0]
    assert [event['start_frame'], event['end_frame']] == [1, 2]
    assert scan.checked_events('{"risk_events":[]}', META) == []
    for value in ({}, {'risk_events': None}, {'risk_events':[dict(EVENT,start_ms=True)]},
                  {'risk_events':[dict(EVENT,start_ms=34.1)]}, {'risk_events':[dict(EVENT,end_ms=4000)]},
                  {'risk_events':[dict(EVENT,evidence_zh='')]}, {'risk_events':[dict(EVENT,end_ms=34)]}):
        with pytest.raises(cutting.CutError): scan.checked_events(json.dumps(value), META)


def test_refinement_never_erases_coarse_risk():
    event=scan.checked_events(json.dumps({'risk_events':[EVENT]}), META)[0]
    result=scan.refined_event(event, [])
    assert result['start_frame']==1 and result['end_frame']==2
    assert result['refinement']=='inconclusive'
    wide=scan.checked_events(json.dumps({'risk_events':[dict(EVENT,start_ms=0,end_ms=100)]}), META)
    result=scan.refined_event(event,wide)
    assert result['start_frame']==0 and result['end_frame']==3


@pytest.fixture
def configured(ready, tmp_path, monkeypatch):
    key=tmp_path/'scan.env';key.write_text('GEMINI_API_KEY=fixture-key\nALIYUN_SECRET=never-load\n')
    monkeypatch.setenv('EXACTCUT_CUT_SCAN_ENV',str(key))
    monkeypatch.setattr(scan,'settings',lambda: {'enabled':True,'model':'fixture-model','coarse_fps':2,'max_refine_events':8,'request_timeout_ms':120000,'default_policy':'standard'})
    bid,rid,sha,source=ready
    session=cutting.open_session(bid,rid,sha)
    return bid,session['id'],source


@pytest.mark.integration
def test_scan_http_lifecycle_cache_recovery_and_acl(configured, monkeypatch):
    bid,sid,source=configured
    started,finish=threading.Event(),threading.Event()
    def detect(*args):
        started.set();assert finish.wait(5)
        return {'risk_events':[], 'warnings':[]}
    monkeypatch.setattr(scan,'detect',detect)
    app=serve();base=f'/api/batches/{bid}/cuts/{sid}/scan'
    try:
        assert call(app,'POST',base,{'policy':'standard'},auth=False)[0]==401
        assert call(app,'POST',base,{'policy':'standard'},csrf=False)[0]==403
        assert call(app,'POST',base,{'policy':'invented'})[0]==400
        assert call(app,'POST',base,{'policy':[]})[0]==400
        code,report=call(app,'POST',base,{'policy':'standard'})
        assert code==202 and started.wait(2)
        assert call(app,'POST',base,{'policy':'standard'})[1]['id']==report['id']
        strict_code,strict=call(app,'POST',base,{'policy':'strict'})
        assert strict_code==202 and strict['id']!=report['id']
        with pytest.raises(cutting.CutError):
            with cutting.batch_lease(bid,exclusive=True):pytest.fail('active scan must protect its source')
        finish.set()
        for _ in range(100):
            state=call(app,'GET',base+'/standard')[1]
            if state['report']['status'] not in {'queued','running','cancelling'}:break
            time.sleep(.02)
        assert state['report']['status']=='completed'
        assert state['report']['risk_events']==[]
        assert call(app,'POST',base,{'policy':'standard'})[1]['id']==report['id']
        path=scan.report_path(bid,sid,'standard')
        data=json.loads(path.read_text());data['status']='running';path.write_text(json.dumps(data))
        assert call(app,'GET',base+'/standard')[1]['report']['status']=='failed'
        monkeypatch.setattr(scan,'detect',lambda *args: (_ for _ in ()).throw(RuntimeError('secret-private-url')))
        assert call(app,'POST',base,{'policy':'standard'})[0]==202
        for _ in range(100):
            state=call(app,'GET',base+'/standard')[1]
            if state['report']['status'] not in {'queued','running','cancelling'}:break
            time.sleep(.02)
        assert state['report']['status']=='failed' and 'secret' not in state['report']['error']
    finally:
        finish.set();app.shutdown();app.server_close()


@pytest.mark.integration
def test_detector_preserves_coarse_on_provider_refinement_failure(tmp_path, monkeypatch):
    from exactcut import diagnostics
    logged=[]
    monkeypatch.setattr(diagnostics,'event',lambda name,**fields:logged.append((name,fields)))
    from google import genai
    from types import SimpleNamespace
    calls=[]
    def ask(**kwargs):
        calls.append(kwargs)
        if len(calls)>1:raise RuntimeError('provider blocked refinement')
        return SimpleNamespace(text=json.dumps({'risk_events':[EVENT]}))
    closed=[]
    monkeypatch.setattr(genai,'Client',lambda **kwargs:SimpleNamespace(models=SimpleNamespace(generate_content=ask),close=lambda:closed.append(True)))
    def run(command,*args,**kwargs):
        if command[-1]=='-':return b'jpeg'
        from pathlib import Path
        Path(command[-1]).write_bytes(b'proxy')
    monkeypatch.setattr(cutting,'run',run)
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    cfg={'model':'fixture','coarse_fps':2,'max_refine_events':8,'request_timeout_ms':1000}
    result=scan.detect(source,META,cfg,'standard','fixture-key',lambda _:None)
    assert result['risk_events'][0]['start_frame']==1
    assert result['risk_events'][0]['refinement']=='inconclusive'
    assert result['warnings'] and closed
    names=[name for name,_ in logged]
    assert 'scan.proxy.completed' in names and 'scan.model_request.completed' in names
    assert 'scan.rate_wait.completed' not in names
    assert 'scan.model_request.completed' in names and 'scan.model_request.failed' in names
    assert 'scan.result_validate.completed' in names and 'scan.refinement_failed' in names
    assert 'provider blocked refinement' not in str(logged)
    assert 'fixture-key' not in str(logged)
    assert calls[0]['contents'][0].video_metadata.fps==2
    assert '普通亲吻、拥抱不单独标记' in calls[0]['contents'][-1].text


@pytest.mark.integration
def test_scan_disabled(configured, monkeypatch):
    bid,sid,source=configured
    monkeypatch.delenv('EXACTCUT_CUT_SCAN_ENV')
    assert scan.state(bid,sid,'standard')['enabled'] is False
    with pytest.raises(cutting.CutError):scan.start(bid,sid,'standard')


def test_real_development_scan_configuration(monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','development')
    monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(Path(__file__).resolve().parents[1]/'config/development-cutting.json'))
    cfg=scan.settings()
    assert cfg['model'] and cfg['coarse_fps']==2 and cfg['max_refine_events']==8


def test_refinement_tail_never_seeks_beyond_last_frame():
    meta={'fps_num':30,'fps_den':1,'frame_count':4500}
    stamps,left,end=scan.refine_window({'start_ms':149000,'end_ms':150000},meta)
    assert left==148500 and end==150000
    assert max(stamps)==149966 and all(0<=s<=149966 for s in stamps)


@pytest.mark.integration
def test_scan_rejects_modified_frozen_source(configured, monkeypatch):
    bid,sid,original=configured
    original_bytes=original.read_bytes()
    (cutting.session_path(bid,sid)/'source.mp4').write_bytes(b'changed')
    called=[]
    monkeypatch.setattr(scan,'detect',lambda *args:called.append(True) or {'risk_events':[],'warnings':[]})
    scan.start(bid,sid,'standard')
    for _ in range(100):
        report=scan.state(bid,sid,'standard')['report']
        if report['status'] not in {'queued','running','cancelling'}:break
        time.sleep(.02)
    assert report['status']=='failed' and '校验' in report['error']
    assert not called and original.read_bytes()==original_bytes


@pytest.mark.integration
def test_explicit_rescan_bypasses_completed_cache_but_not_running(configured,monkeypatch):
    bid,sid,_=configured
    calls=[];finish=threading.Event()
    def detect(*args):
        calls.append(True)
        if len(calls)==2:assert finish.wait(5)
        return {'risk_events':[],'warnings':[]}
    monkeypatch.setattr(scan,'detect',detect)
    app=serve();base=f'/api/batches/{bid}/cuts/{sid}/scan'
    try:
        assert call(app,'POST',base,{'policy':'standard'})[0]==202
        for _ in range(100):
            if scan.state(bid,sid,'standard')['report']['status']=='completed':break
            time.sleep(.02)
        assert len(calls)==1
        assert call(app,'POST',base,{'policy':'standard'})[1]['status']=='completed'
        assert len(calls)==1
        assert call(app,'POST',base,{'policy':'standard','force':'true'})[0]==400
        code,report=call(app,'POST',base,{'policy':'standard','force':True})
        assert code==202 and report['status'] in {'queued','running'}
        for _ in range(100):
            if len(calls)==2:break
            time.sleep(.02)
        assert len(calls)==2
        assert call(app,'POST',base,{'policy':'standard','force':True})[1]['status'] in {'queued','running'}
        assert len(calls)==2
    finally:
        finish.set()
        for _ in range(100):
            if scan.state(bid,sid,'standard')['report']['status'] not in {'queued','running','cancelling'}:break
            time.sleep(.02)
        app.shutdown();app.server_close()


@pytest.mark.parametrize('environment', ['development', 'test', 'production'])
def test_scan_requires_explicit_configuration(environment, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', environment)
    monkeypatch.setattr(cutting, 'enabled', lambda: True)
    monkeypatch.setattr(scan, 'settings', lambda: {'enabled': True})
    monkeypatch.delenv('EXACTCUT_CUT_SCAN_ENV', raising=False)
    assert not scan.enabled()
    monkeypatch.setenv('EXACTCUT_CUT_SCAN_ENV', '/fixture/scan.env')
    assert scan.enabled()
    monkeypatch.setattr(scan, 'settings', lambda: {'enabled': False})
    assert not scan.enabled()
    monkeypatch.setattr(scan, 'settings', lambda: {'enabled': True})
    monkeypatch.setattr(cutting, 'enabled', lambda: False)
    assert not scan.enabled()

from interactive_test_support import interactive_dispatcher
