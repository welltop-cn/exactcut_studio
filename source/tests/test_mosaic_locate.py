import json
import time
from types import SimpleNamespace
import pytest
from exactcut import cutting, cutting_scan as scan
from exactcut import mosaic_session as sessions
from exactcut.util import write_json_atomic
from test_cutting_scan import configured
from test_cutting_api import ready, call
from test_phase2_preview_api import fixture, serve


def module():
    from exactcut import mosaic_locate
    return mosaic_locate


def poll(m,bid,sid,jid):
    for _ in range(150):
        job=m.state(bid,sid,jid)
        if job['status']!='running':return job
        time.sleep(.02)
    pytest.fail('worker did not complete')


def test_candidate_contract():
    m=module();meta={'width':1920,'height':1080}
    candidate={'box_2d':[100,200,501,701],'label':'区域','reason':'可见裸露'}
    assert m.checked_candidates(json.dumps({'candidates':[candidate]}),meta)==[{**candidate,'id':'0','x':384,'y':108,'w':962,'h':434}]
    assert m.checked_candidates('{"candidates":[]}',meta)==[]
    for text in ('', 'refused', '{}', '{"candidates":null}',json.dumps({'candidates':[candidate]*9})):
        with pytest.raises(cutting.CutError):m.checked_candidates(text,meta)
    for box in ([True,0,100,100],[0,0,1001,100],[100,0,0,100],[0,0,1.5,100],[0,0,1]):
        with pytest.raises(cutting.CutError):m.checked_candidates(json.dumps({'candidates':[{**candidate,'box_2d':box}]}),meta)


def test_http_lifecycle(configured,monkeypatch):
    m=module();bid,cut_sid,source=configured;original=source.read_bytes()
    cut=cutting.read_session(bid,cut_sid);sid=sessions.open_session(bid,cut['request_id'],cut['source_sha256'])['id']
    monkeypatch.setattr(m,'detect',lambda *args:[])
    app=serve();base=f'/api/batches/{bid}/mosaics/{sid}'
    body={'start':10,'end':20,'seed_frame':15,'reset_at':''}
    try:
        assert call(app,'GET',base)[1]['capabilities']['region_recognition']
        assert call(app,'POST',base+'/locate',body,auth=False)[0]==401
        assert call(app,'POST',base+'/locate',body,csrf=False)[0]==403
        for patch in ({'start':True},{'end':91},{'seed_frame':20},{'start':11.5},{'reset_at':None}):
            assert call(app,'POST',base+'/locate',{**body,**patch})[0] in (400,409)
        code,job=call(app,'POST',base+'/locate',body)
        assert code==202
        assert poll(m,bid,sid,job['id'])['status']=='completed'
        assert call(app,'GET',base+'/locate/'+job['id'])[1]['candidates']==[]
        assert call(app,'POST',base+'/locate',body)[1]['id']==job['id']
        assert cutting.read_session(bid,cut_sid)['jobs']==[] and source.read_bytes()==original
        cutting.discard(bid,cut_sid)
        assert call(app,'GET',base+'/locate/'+job['id'])[0]==200
        session=sessions.read_session(bid,sid);session['reset_at']='new-reset'
        write_json_atomic(sessions.session_path(bid,sid)/'session.json',session)
        assert call(app,'GET',base+'/locate/'+job['id'])[0]==409
        assert call(app,'POST',base+'/locate',body)[0]==409
    finally:app.shutdown();app.server_close()


def test_failure_recovery_disabled(configured,monkeypatch):
    m=module();bid,cut_sid,_=configured
    cut=cutting.read_session(bid,cut_sid);sid=sessions.open_session(bid,cut['request_id'],cut['source_sha256'])['id']
    monkeypatch.setattr(m,'detect',lambda *args: (_ for _ in ()).throw(RuntimeError('private-secret')))
    job=m.start(bid,sid,0,20,5,'');result=poll(m,bid,sid,job['id'])
    assert result['status']=='failed' and 'secret' not in result['error']
    path=sessions.session_path(bid,sid)/f"locate-{job['id']}.json"
    result['status']='running';path.write_text(json.dumps(result))
    assert m.state(bid,sid,job['id'])['status']=='failed'
    monkeypatch.delenv('EXACTCUT_CUT_SCAN_ENV')
    with pytest.raises(cutting.CutError) as error:m.start(bid,sid,0,20,5,'')
    assert error.value.status==503


def test_provider_single_jpeg_refusal(monkeypatch):
    m=module()
    from google import genai
    requests=[];closed=[]
    def ask(**kwargs):requests.append(kwargs);return SimpleNamespace(text=None)
    monkeypatch.setattr(genai,'Client',lambda **kwargs:SimpleNamespace(models=SimpleNamespace(generate_content=ask),close=lambda:closed.append(True)))
    monkeypatch.setattr(scan.ai_rate,'acquire',lambda:None)
    with pytest.raises(cutting.CutError):m.detect(b'jpeg',{'width':100,'height':100},{'model':'fixture','request_timeout_ms':1000},'fixture-key')
    assert len(requests)==1 and closed
    assert requests[0]['contents'][0].inline_data.mime_type=='image/jpeg'


def test_snapshot_open_and_frame_are_independent(configured):
    bid,cut_sid,source=configured;cut=cutting.read_session(bid,cut_sid)
    app=serve();base=f'/api/batches/{bid}/mosaics'
    try:
        code,session=call(app,'POST',base+'/open',{'request_id':cut['request_id'],'source_sha256':cut['source_sha256']})
        assert code==200 and 'jobs' not in session
        snapshot=sessions.session_path(bid,session['id'])/'source.mp4'
        assert snapshot.read_bytes()==source.read_bytes() and snapshot!=cutting.session_path(bid,cut_sid)/'source.mp4'
        code,jpeg=call(app,'GET',base+'/'+session['id']+'/frame/15')
        assert code==200 and jpeg.startswith(b'\xff\xd8')
        assert call(app,'GET',base+'/'+session['id']+'/frame/90')[0]==400
        other=__import__('exactcut.workflow',fromlist=['create_blank']).create_blank()
        assert call(app,'GET',f"/api/batches/{other['id']}/mosaics/{session['id']}")[0]==404
    finally:app.shutdown();app.server_close()


def test_running_job_does_not_hold_edit_gate_and_rejects_stale_result(configured,monkeypatch):
    import threading
    from exactcut import cutting_access, workflow
    m=module();bid,cut_sid,_=configured;cut=cutting.read_session(bid,cut_sid)
    session=sessions.open_session(bid,cut['request_id'],cut['source_sha256']);sid=session['id']
    entered,finish=threading.Event(),threading.Event()
    def detect(*args):entered.set();assert finish.wait(5);return []
    monkeypatch.setattr(m,'detect',detect)
    job=m.start(bid,sid,0,20,5,'')
    try:
        assert entered.wait(2)
        assert m.start(bid,sid,0,20,5,'')['id']==job['id']
        with pytest.raises(cutting.CutError):m.start(bid,sid,0,20,6,'')
        with pytest.raises(cutting.CutError):
            with cutting.batch_lease(bid,exclusive=True):pytest.fail('active worker must retain source')
        with cutting_access.gate(workflow.folder(bid),session['request_id']):
            session['reset_at']='changed';write_json_atomic(sessions.session_path(bid,sid)/'session.json',session)
    finally:finish.set()
    path=sessions.session_path(bid,sid)/f"locate-{job['id']}.json"
    for _ in range(150):
        data=json.loads(path.read_text())
        if data['status']!='running':break
        time.sleep(.02)
    assert data['status']=='failed' and data['candidates']==[]
    with pytest.raises(cutting.CutError):m.state(bid,sid,job['id'])


def test_modified_snapshot_never_reaches_provider(configured,monkeypatch):
    m=module();bid,cut_sid,_=configured;cut=cutting.read_session(bid,cut_sid)
    session=sessions.open_session(bid,cut['request_id'],cut['source_sha256']);sid=session['id']
    (sessions.session_path(bid,sid)/'source.mp4').write_bytes(b'changed')
    calls=[];monkeypatch.setattr(m,'detect',lambda *args:calls.append(True) or [])
    job=m.start(bid,sid,0,20,5,'')
    assert poll(m,bid,sid,job['id'])['status']=='failed' and not calls
