import time
import pytest
from exactcut import cutting, cutting_scan, mosaic_session as sessions, mosaic_locate
from exactcut.util import read_json, write_json_atomic
from test_cutting_scan import configured
from test_cutting_api import ready, call
from test_phase2_preview_api import fixture, serve


def module():
    from exactcut import mosaic_scan
    return mosaic_scan


def setup(configured):
    bid,cut_sid,_=configured;cut=cutting.read_session(bid,cut_sid)
    return bid,sessions.open_session(bid,cut['request_id'],cut['source_sha256'])['id']


def poll(m,bid,sid):
    for _ in range(200):
        job=m.state(bid,sid)['report']
        if job['status'] not in {'queued','running','cancelling'}:return job
        time.sleep(.02)
    pytest.fail('worker did not complete')


def coarse(*args,**kwargs):
    args[6].close()
    return {'risk_events':[{'id':str(i),'start_frame':i*5,'end_frame':i*5+5,'category':'风险','evidence_zh':'证据'} for i in range(10)],'warnings':[]}


@pytest.mark.integration
def test_whole_scan_preserves_unlocalized_and_bounds_calls(configured,monkeypatch):
    m=module();bid,sid=setup(configured);calls=[]
    def forty(*args,**kwargs):
        result=coarse(*args,**kwargs)
        result['risk_events']=[{**result['risk_events'][0],'id':str(i),'start_frame':i*2,'end_frame':i*2+3} for i in range(40)]
        return result
    monkeypatch.setattr(cutting_scan,'detect',forty)
    monkeypatch.setattr(mosaic_locate,'detect',lambda *a,**k:calls.append(k) or [])
    job=m.start(bid,sid,'');result=poll(m,bid,sid)
    assert result['status']=='completed' and len(result['events'])==40
    assert len(calls)==24 and all(e['status']=='needs_review' for e in result['events'])
    assert result['warnings'] and all('deadline' in c for c in calls)
    assert m.start(bid,sid,'')['id']==job['id']
    assert m.start(bid,sid,'',force=True)['id']!=job['id']
    poll(m,bid,sid)


@pytest.mark.integration
def test_http_whole_scan_and_failure(configured,monkeypatch):
    m=module();bid,sid=setup(configured)
    monkeypatch.setattr(cutting_scan,'detect',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('secret')))
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}/scan'
    try:
        assert call(app,'GET',base)[1]['report'] is None
        assert call(app,'POST',base,{'reset_at':''},csrf=False)[0]==403
        assert call(app,'POST',base,{'reset_at':'old'})[0]==409
        assert call(app,'POST',base,{'reset_at':'','force':1})[0]==400
        assert call(app,'POST',base,{'reset_at':''})[0]==202
        result=poll(m,bid,sid)
        assert result['status']=='failed' and 'secret' not in result['error']
        path=sessions.session_path(bid,sid)/'scan.json';result['status']='running';write_json_atomic(path,result)
        assert m.state(bid,sid)['report']['status']=='failed'
    finally:app.shutdown();app.server_close()


@pytest.mark.integration
def test_scan_failure_records_private_diagnostics_without_provider_body(configured,monkeypatch):
    from exactcut import diagnostics
    m=module();bid,sid=setup(configured);logged=[]
    def fail(*args,**kwargs):
        raise RuntimeError('private provider response')
    monkeypatch.setattr(cutting_scan,'detect',fail)
    monkeypatch.setattr(diagnostics,'event',lambda name,**fields:logged.append((name,fields)))
    started=m.start(bid,sid,'');result=poll(m,bid,sid)
    assert result['status']=='failed'
    failures=[fields for name,fields in logged if name=='mosaic.scan_failed']
    assert len(failures)==1
    record=failures[0]
    assert (record['batch_id'],record['session_id'],record['job_id'])==(bid,sid,started['id'])
    assert record['error']['type']=='RuntimeError'
    assert record['error']['frames'][-1]['function']=='fail'
    assert 'private provider response' not in str(record)
    assert 'private provider response' not in str(result)


@pytest.mark.integration
def test_partial_localization_retains_evidence(configured,monkeypatch):
    m=module();bid,sid=setup(configured);seen=[]
    monkeypatch.setattr(cutting_scan,'detect',coarse)
    def locate(*args,**kwargs):
        seen.append(True)
        if len(seen)==1:raise cutting.CutError('failed')
        return [{'id':'0','box_2d':[0,0,100,100],'x':0,'y':0,'w':10,'h':10,'label':'region','reason':'visible'}]
    monkeypatch.setattr(mosaic_locate,'detect',locate)
    m.start(bid,sid,'');job=poll(m,bid,sid)
    assert job['events'][0]['status']=='localized'
    assert job['events'][0]['seed_frame']==0
    assert job['events'][0]['candidates'] and job['warnings']


@pytest.mark.integration
def test_running_reset_and_global_lease(configured,monkeypatch):
    import threading
    from exactcut import cutting_access, workflow
    m=module();bid,sid=setup(configured);entered=threading.Event();finish=threading.Event()
    def detect(*args,**kwargs):
        entered.set();assert finish.wait(5);return coarse(*args,**kwargs)
    monkeypatch.setattr(cutting_scan,'detect',detect)
    monkeypatch.setattr(mosaic_locate,'detect',lambda *a,**k:[])
    job=m.start(bid,sid,'')
    try:
        assert entered.wait(2)
        assert m.start(bid,sid,'',force=True)['id']==job['id']
        other=mosaic_locate.start(bid,sid,0,5,2,'');assert other['status']=='queued'
        with pytest.raises(cutting.CutError):
            with cutting.batch_lease(bid,exclusive=True):pytest.fail('worker holds snapshot')
        session=sessions.read_session(bid,sid)
        with cutting_access.gate(workflow.folder(bid),session['request_id']):
            session['reset_at']='new';write_json_atomic(sessions.session_path(bid,sid)/'session.json',session)
    finally:finish.set()
    path=sessions.session_path(bid,sid)/'scan.json'
    for _ in range(200):
        result=read_json(path)
        if result['status'] not in {'queued','running','cancelling'}:break
        time.sleep(.02)
    assert result['status']=='failed' and result['events']==[]
    with pytest.raises(cutting.CutError):m.state(bid,sid)


@pytest.mark.integration
def test_deadline_preserves_all_events_without_localization(configured,monkeypatch):
    m=module();bid,sid=setup(configured);calls=[]
    monkeypatch.setattr(m,'TIMEOUT',0)
    monkeypatch.setattr(cutting_scan,'detect',coarse)
    monkeypatch.setattr(mosaic_locate,'detect',lambda *a,**k:calls.append(True))
    m.start(bid,sid,'');job=poll(m,bid,sid)
    assert len(job['events'])==10 and not calls
    assert all(e['status']=='needs_review' for e in job['events'])


@pytest.mark.integration
def test_snapshot_modified_before_scan_is_failure(configured,monkeypatch):
    m=module();bid,sid=setup(configured);calls=[]
    (sessions.session_path(bid,sid)/'source.mp4').write_bytes(b'changed')
    monkeypatch.setattr(cutting_scan,'detect',lambda *a,**k:calls.append(True))
    m.start(bid,sid,'')
    assert poll(m,bid,sid)['status']=='failed' and not calls


def test_expired_region_deadline_never_calls_model(monkeypatch):
    from google import genai
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(genai,'Client',lambda **k:SimpleNamespace(models=SimpleNamespace(generate_content=lambda **k:calls.append(True)),close=lambda:None))
    with pytest.raises(cutting.CutError):mosaic_locate.detect(b'jpeg',{}, {'model':'fixture','request_timeout_ms':1000},'fixture',deadline=time.monotonic()-1)
    assert not calls


@pytest.mark.integration
def test_stale_generation_cannot_publish(configured,monkeypatch):
    import threading
    m=module();bid,sid=setup(configured);entered=threading.Event();finish=threading.Event()
    def detect(*args,**kwargs):
        args[6].close();entered.set();assert finish.wait(5)
        return {'risk_events':[],'warnings':[]}
    monkeypatch.setattr(cutting_scan,'detect',detect)
    m.start(bid,sid,'');path=sessions.session_path(bid,sid)/'scan.json'
    try:
        assert entered.wait(2)
        replacement={**read_json(path),'id':'f'*32,'status':'completed','progress':'new generation'}
        write_json_atomic(path,replacement)
    finally:finish.set()
    for _ in range(200):
        try:
            lease=cutting.lock_file(path.with_suffix('.worker.lock'));lease.close();break
        except cutting.CutError:time.sleep(.02)
    assert read_json(path)==replacement


def test_expired_coarse_deadline_skips_proxy(tmp_path,monkeypatch):
    from google import genai
    from types import SimpleNamespace
    from test_cutting_scan import META
    calls=[];closed=[]
    monkeypatch.setattr(genai,'Client',lambda **k:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(cutting,'run',lambda *a:calls.append(True))
    with pytest.raises(cutting.CutError):
        cutting_scan.detect(tmp_path/'source.mp4',META,{'model':'fixture','request_timeout_ms':1000},'standard','fixture',lambda _:None,SimpleNamespace(close=lambda:closed.append(True)),deadline=time.monotonic()-1)
    assert not calls and closed


@pytest.mark.integration
def test_coarse_visible_before_localization_finishes(configured,monkeypatch):
    import threading
    m=module();bid,sid=setup(configured);entered=threading.Event();finish=threading.Event();configs=[]
    def detect(*args,**kwargs):
        configs.append(args[2]);return coarse(*args,**kwargs)
    def locate(*args,**kwargs):
        entered.set();assert finish.wait(5);return []
    monkeypatch.setattr(cutting_scan,'detect',detect)
    monkeypatch.setattr(mosaic_locate,'detect',locate)
    m.start(bid,sid,'')
    try:
        assert entered.wait(2)
        job=m.state(bid,sid)['report']
        assert job['status']=='running' and len(job['events'])==10
        assert configs[0]['max_refine_events']==0
    finally:finish.set()
    assert poll(m,bid,sid)['status']=='completed'


@pytest.mark.integration
def test_each_localized_event_is_published_before_next_finishes(configured,monkeypatch):
    import threading
    m=module();bid,sid=setup(configured);entered=threading.Event();finish=threading.Event();calls=[]
    monkeypatch.setattr(cutting_scan,'detect',coarse)
    def locate(*args,**kwargs):
        calls.append(True)
        if len(calls)==2:entered.set();assert finish.wait(5)
        return [{'id':'0','box_2d':[0,0,100,100],'x':0,'y':0,'w':10,'h':10,'label':'region','reason':'visible'}]
    monkeypatch.setattr(mosaic_locate,'detect',locate)
    m.start(bid,sid,'')
    try:
        assert entered.wait(2)
        job=m.state(bid,sid)['report']
        assert job['status']=='running' and len(job['events'])==10
        assert job['events'][0]['status']=='localized'
        assert job['events'][1]['status']=='needs_review'
    finally:finish.set()
    poll(m,bid,sid)


@pytest.mark.integration
def test_scan_logs_lifecycle_and_request_context(configured,monkeypatch):
    from exactcut import diagnostics
    m=module();bid,sid=setup(configured);logged=[]
    monkeypatch.setattr(diagnostics,'event',lambda name,**fields:logged.append((name,{**diagnostics.current(),**fields})))
    monkeypatch.setattr(cutting_scan,'detect',coarse)
    monkeypatch.setattr(mosaic_locate,'detect',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('private-body')))
    previous=diagnostics.current()
    diagnostics.bind(trace_id='scan-test-trace',client_request_id='scan-test-client')
    try:
        job=m.start(bid,sid,'');result=poll(m,bid,sid)
    finally:
        diagnostics._context.set(previous)
    assert result['status']=='completed'
    entries={name:fields for name,fields in logged}
    for name in ('mosaic.scan_started','mosaic.scan_completed','mosaic.localization_failed'):
        fields=entries[name]
        assert fields['job_id']==job['id'] and fields['batch_id']==bid
        assert fields['trace_id']=='scan-test-trace'
        assert fields['client_request_id']=='scan-test-client'
        assert fields['duration_ms']>=0
    assert entries['mosaic.scan_completed']['event_count']==10
    assert entries['mosaic.scan_completed']['localized_count']==0
    assert entries['mosaic.localization_failed']['phase']=='region_detect'
    assert 'private-body' not in str(logged)

from interactive_test_support import interactive_dispatcher
