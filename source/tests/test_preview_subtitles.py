import json
import threading
import time

import pytest

from exactcut import preview_subtitles as subtitles, preview_subtitle_model as model
from exactcut import preview_subtitle_queue as queue
from exactcut import workflow as w, material_versions as media, cutting, accounts
from exactcut.util import write_json_atomic, read_json, file_sha256
from test_cutting_api import ready, call
from test_phase2_preview_api import fixture, serve

CUE = {'start_ms': 100, 'end_ms': 500, 'original_text': 'Hello', 'translation_zh': '你好'}


@pytest.fixture
def configured(ready, tmp_path, monkeypatch):
    accounts.initialize(queue.accounts_path(), 'independent-subtitle-test-password')
    key = tmp_path/'subtitle.env'
    key.write_text('GEMINI_API_KEY=fixture-key\nUNRELATED_SECRET=do-not-load\n')
    monkeypatch.setenv('EXACTCUT_PREVIEW_SUBTITLE_ENV', str(key))
    cfg = subtitles.settings()
    cfg.update(enabled=True,max_active_tasks=1)
    monkeypatch.setattr(subtitles, 'settings', lambda: cfg)
    return ready


@pytest.fixture
def dispatcher():
    """Foreground, isolated dispatcher; never the process-wide dev one."""
    running = []
    def start(run=None):
        item = queue.Dispatcher(run=run, poll_seconds=.02)
        assert item.start(), 'dispatcher lease should be free in an isolated data root'
        running.append(item)
        return item
    yield start
    for item in running:
        item.stop()


def wait(bid, rid, sha, version=None):
    for _ in range(400):
        report = subtitles.state(bid, rid, sha, version)['report']
        if report['status'] not in {'queued', 'running'}:
            return report
        time.sleep(.01)
    pytest.fail('worker did not finish')


def route(bid, rid, sha, version=None):
    return f'/api/batches/{bid}/preview-subtitles/{rid}?source_sha256={sha}&version_id={version or ""}'


def test_chunk_offsets_bounds_and_repeated_dialogue_are_preserved():
    assert model.chunks(60100,60000) == [(0,30050),(30050,30050)]
    assert model.chunks(120100,60000) == [(0,40033),(40033,40033),(80066,40034)]
    assert model.chunks(300000,60000) == [(n*60000,60000) for n in range(5)]
    first = model.checked_cues(json.dumps({'cues':[CUE,CUE]}), 60000)
    later = model.offset_cues(first, 60000)
    assert len(first) == 1 and later[0]['start_ms'] == 60100
    assert model.join_chunks([(0,first),(60000,first)]) == [CUE,later[0]]
    crossing = dict(CUE, start_ms=59900, end_ms=60000)
    continued = dict(CUE, start_ms=0, end_ms=300)
    assert model.join_chunks([(0,[crossing]),(60000,[continued])]) == [dict(crossing,end_ms=60300)]
    for bad in [dict(CUE,start_ms=True),dict(CUE,start_ms=-1),dict(CUE,end_ms=60001),dict(CUE,translation_zh=''),dict(CUE,original_text='x'*2001)]:
        with pytest.raises(ValueError):model.checked_cues(json.dumps({'cues':[bad]}),60000)
    assert model.checked_cues('{"cues":[]}',60000) == []
    with pytest.raises(ValueError):model.checked_cues('{}',60000)
    assert model.inline_request_bytes(3) >= 4
    assert model.inline_request_bytes(15_000_000) > 20_000_000


@pytest.mark.parametrize('duration',[999,1000,59999,60000,60001,60100,120001,300000])
def test_balanced_chunks_cover_every_millisecond_without_tiny_tail(duration):
    parts=model.chunks(duration,60000)
    assert parts[0][0]==0 and sum(length for _,length in parts)==duration
    assert all(0<length<=60000 for _,length in parts)
    assert all(a+length==b for (a,length),(b,_) in zip(parts,parts[1:]))
    assert max(length for _,length in parts)-min(length for _,length in parts)<=1


@pytest.mark.integration
def test_sdk_schema_conversion_cannot_change_persistent_task_identity(tmp_path,monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from google.genai import _transformers
    original=deepcopy(model.SCHEMA)
    cfg=subtitles.settings();cfg['model']='fixture'
    binding={'batch_id':'a'*32,'request_id':'R1','source_sha256':'b'*64,'version_id':None}
    before=subtitles.identity(binding,cfg)
    def request(**kwargs):
        _transformers.t_schema(None,kwargs['config'].response_schema)
        return SimpleNamespace(text='{"cues":[]}')
    client=SimpleNamespace(models=SimpleNamespace(generate_content=request))
    proxy=tmp_path/'proxy.mp4';proxy.write_bytes(b'proxy')
    try:
        model.ask(client,proxy,1000,cfg,time.monotonic()+500)
        assert model.SCHEMA==original
        assert subtitles.identity(binding,cfg)==before
    finally:
        model.SCHEMA.clear();model.SCHEMA.update(original)


@pytest.mark.integration
def test_any_chunk_failure_discards_partial_success(tmp_path, monkeypatch):
    source=tmp_path/'source.mp4';source.write_bytes(b'video')
    cfg=subtitles.settings();cfg['model']='fixture-model'
    monkeypatch.setattr(model,'prepare_chunk',lambda source, target, *a: target.write_bytes(b'proxy'))
    calls=[]
    def ask(*args):
        calls.append(True)
        if len(calls)==2:raise RuntimeError('private-provider-url')
        from types import SimpleNamespace
        text=json.dumps({'cues':[CUE]})
        return SimpleNamespace(text=text,model_dump=lambda **_: {'text':text})
    monkeypatch.setattr(model,'ask',ask)
    with pytest.raises(RuntimeError):model.recognize(source,60100,cfg,'key',lambda **_:None,tmp_path)
    assert len(calls)==2


@pytest.mark.integration
def test_legacy_delivered_acl_cache_and_immutability(configured, monkeypatch, dispatcher):
    bid,rid,sha,source=configured
    record=read_json(w.folder(bid)/'production/results.json')
    record['requests'][rid].update(status='fbbm_ready',publication={'fixture':True})
    write_json_atomic(w.folder(bid)/'production/results.json',record)
    before=(w.folder(bid)/'production/results.json').read_bytes()
    calls=[]
    monkeypatch.setattr(model,'recognize',lambda *a: calls.append(True) or [CUE])
    app=serve();base=f'/api/batches/{bid}/preview-subtitles/{rid}'
    body={'source_sha256':sha,'version_id':None}
    try:
        assert call(app,'GET',route(bid,rid,sha),auth=False)[0]==401
        assert call(app,'POST',base,body,csrf=False)[0]==403
        assert call(app,'POST',base,{**body,'source_sha256':'f'*64})[0]==409
        assert call(app,'GET',route(bid,'missing',sha))[0]==404
        assert call(app,'GET',route(bid,rid,sha))[1]['report']['status']=='idle'
        assert not calls
        code,queued=call(app,'POST',base,body)
        assert code==202 and queued['report']['status']=='queued'
        dispatcher()
        report=wait(bid,rid,sha)
        assert report['status']=='completed', (report.get('status'),report.get('error'))
        assert call(app,'POST',base,body)[0]==200
        assert len(calls)==1 and file_sha256(source)==sha
        assert (w.folder(bid)/'production/results.json').read_bytes()==before
        assert call(app,'GET',f'/api/batches/{bid}/video/{rid}')[1]==source.read_bytes()
        monkeypatch.setattr(app.RequestHandlerClass,'can_access',lambda self,bid:False)
        assert call(app,'GET',route(bid,rid,sha))[0]==404
    finally:app.shutdown();app.server_close()


@pytest.mark.integration
def test_managed_delivered_uses_current_not_candidate(configured, monkeypatch, dispatcher):
    bid,rid,sha,source=configured
    media.enroll(w.folder(bid))
    state=media.read(w.folder(bid));vid=state['assets'][rid]['current_version']
    state['deliveries']['test']={'asset_id':rid,'version_id':vid,'status':'fbbm_ready'}
    state['assets'][rid]['candidate_version']='f'*32
    state['versions']['f'*32]={'status':'processing','version_id':'f'*32}
    write_json_atomic(w.folder(bid)/'media-state.json',state)
    with pytest.raises(ValueError):cutting.open_session(bid,rid,sha)
    monkeypatch.setattr(model,'recognize',lambda *a:[CUE])
    subtitles.start(bid,rid,sha,vid,'admin');dispatcher()
    assert wait(bid,rid,sha,vid)['cues']==[CUE]
    with pytest.raises(subtitles.SubtitleError) as error:subtitles.state(bid,rid,sha,'f'*32)
    assert error.value.status==409


@pytest.mark.integration
def test_invalid_subtitle_configuration_isolated_from_login_and_existing_ui(configured, monkeypatch):
    bid,rid,sha,_=configured
    def invalid():raise subtitles.SubtitleError('private-invalid-setting',503)
    monkeypatch.setattr(subtitles,'settings',invalid)
    app=serve()
    try:
        code,me=call(app,'GET','/api/me')
        assert code==200 and me['features']['preview_subtitles'] is False
        assert call(app,'GET','/')[0]==200
        code,error=call(app,'GET',route(bid,rid,sha))
        assert code==503 and 'private' not in str(error)
        code,error=call(app,'POST',f'/api/batches/{bid}/preview-subtitles/{rid}',{'source_sha256':sha,'version_id':None})
        assert code==503 and 'private' not in str(error)
    finally:app.shutdown();app.server_close()


@pytest.mark.integration
def test_extremely_short_video_fails_clearly_without_provider_request(configured,monkeypatch,dispatcher):
    bid,rid,sha,_=configured
    monkeypatch.setattr(subtitles,'ffprobe',lambda _: {'format':{'duration':'0.1'}})
    calls=[];monkeypatch.setattr(model,'recognize',lambda *a: calls.append(True) or [])
    subtitles.start(bid,rid,sha,None,'admin');dispatcher()
    report=wait(bid,rid,sha)
    assert report['status']=='failed' and '过短' in report['error'] and not calls


@pytest.mark.integration
def test_duration_limit_message_uses_configured_limit(configured,monkeypatch,dispatcher):
    bid,rid,sha,_=configured
    subtitles.settings()['max_duration_seconds']=1
    monkeypatch.setattr(model,'recognize',lambda *a: pytest.fail('duration rejected before model call'))
    subtitles.start(bid,rid,sha,None,'admin');dispatcher()
    report=wait(bid,rid,sha)
    assert report['status']=='failed' and '1 秒' in report['error'] and '300' not in report['error']


@pytest.mark.integration
def test_same_task_reuses_and_source_change_fails(configured, monkeypatch, dispatcher):
    bid,rid,sha,source=configured
    entered,finish=threading.Event(),threading.Event();calls=[]
    def recognize(*args):
        calls.append(True);entered.set();assert finish.wait(5)
        return [CUE]
    monkeypatch.setattr(model,'recognize',recognize)
    first=subtitles.start(bid,rid,sha,None,'admin')
    dispatcher()
    try:
        assert entered.wait(2)
        # Duplicate submissions never add a second paid task.
        assert subtitles.start(bid,rid,sha,None,'admin')['report']['id']==first['report']['id']
        assert len(queue.snapshot()['pending'])==0 and queue.membership(first['report']['id'])['active']
        assert queue.snapshot()['active']['task_id']==first['report']['id']
        # Deleting or replacing the source stays protected by the shared batch lease.
        with pytest.raises(cutting.CutError):
            with cutting.batch_lease(bid,exclusive=True):pytest.fail('source deletion unprotected')
        result=read_json(w.folder(bid)/'production/results.json')
        result['requests'][rid]['output_sha256']='b'*64
        write_json_atomic(w.folder(bid)/'production/results.json',result)
    finally:finish.set()
    path=subtitles.report_path(first['binding'],subtitles.settings())
    for _ in range(200):
        report=read_json(path)
        if report['status']!='running':break
        time.sleep(.01)
    assert report['status']=='failed' and not report['cues'] and len(calls)==1
    result['requests'][rid]['output_sha256']=sha;write_json_atomic(w.folder(bid)/'production/results.json',result)
    # A report left "running" without any live worker lease is repaired, never resent.
    report['status']='running';write_json_atomic(path,report)
    assert subtitles.state(bid,rid,sha,None)['report']['status']=='failed'
    assert len(calls)==1


@pytest.mark.integration
def test_provider_error_text_never_reaches_the_report(configured, monkeypatch, dispatcher):
    bid,rid,sha,_=configured
    monkeypatch.setattr(model,'recognize',lambda *a: (_ for _ in ()).throw(RuntimeError('private-key-url')))
    subtitles.start(bid,rid,sha,None,'admin');dispatcher()
    report=wait(bid,rid,sha)
    assert report['status']=='failed' and 'private' not in report['error'] and not report['cues']
