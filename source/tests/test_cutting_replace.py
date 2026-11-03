"""Real deletion and current-media replacement; external delivery is isolated."""
import copy
import time

import pytest
from exactcut import cutting, workflow as w, preview_oss, material_versions as mv
from exactcut.util import file_sha256, read_json, write_json_atomic
from test_cutting_api import ready, fixture


def rendered(ready):
    bid,rid,sha,source=ready
    session=cutting.open_session(bid,rid,sha)
    job=cutting.start_render(bid,session['id'],[[10,20]])
    for _ in range(200):
        job=cutting.get_session(bid,session['id'])['jobs'][-1]
        if job['status']!='running':break
        time.sleep(.05)
    assert job['status']=='ready'
    return session,job


def replace(ready,session,job):
    from exactcut import cutting_replace
    result=cutting_replace.start(ready[0],session['id'],job['id'])
    for _ in range(200):
        result=cutting.get_session(ready[0],session['id'])['jobs'][-1]
        if result.get('replacement',{}).get('status')!='running':break
        time.sleep(.05)
    return result


@pytest.mark.integration
def test_replace_changes_all_current_reads_and_keeps_original(ready):
    session,job=rendered(ready);bid,rid,sha,original=ready
    result=replace(ready,session,job)
    assert result['replacement']['status']=='completed',result
    current,record=w.preview_file(bid,rid)
    assert current.name==original.name and current!=original
    assert record['output_sha256']==job['output_sha256']==file_sha256(current)
    preview,_=w.preview_file(bid,rid,lightweight=True)
    assert preview!=current and record['preview']['master_sha256']==job['output_sha256']
    assert file_sha256(original)==sha
    from exactcut.stage_results import artifacts
    batch=w.folder(bid)
    for request_id in (rid,None):
        evidence=artifacts({'phase':'render','batch_id':bid,'request_id':request_id},batch.parent,batch.parent.parent.parent)
        assert str(current) in {item['path'] for item in evidence}
        assert str(original) not in {item['path'] for item in evidence}
    asset=next(a for a in w.detail(bid)['assets'] if a['request_id']==rid)
    assert asset['sha256']==job['output_sha256'] and asset['duration_ms']==pytest.approx(80000/30,abs=2)
    assert replace(ready,session,job)['replacement']['status']=='completed'
    # Upload selects precisely the replacement via the existing workflow resolver.
    w.queue_publish(bid,rid,job['output_sha256'])
    approved=read_json(w.folder(bid)/'publications.json')[rid]
    assert approved['approved_sha256']==file_sha256(w.preview_file(bid,rid)[0])
    with pytest.raises(cutting.CutError):cutting.open_session(bid,rid,job['output_sha256'])


@pytest.mark.integration
@pytest.mark.parametrize('entry',[
    {'status':'fbbm_ready'}, {'status':'upload_queued'}, {'status':'uploading'},
    {'status':'fbbm_queued'}, {'status':'unverified'},
    {'status':'publish_failed','publication':{'sha256':'a'*64}},
    {'status':'publish_failed','intent':{'status':'pending'}},
])
def test_delivered_or_ambiguous_has_no_edit_capability(ready,entry):
    from exactcut import cutting_access
    bid,rid,sha,_=ready
    write_json_atomic(w.folder(bid)/'publications.json',{rid:entry})
    assert not cutting_access.can_edit(w.folder(bid),rid)
    with pytest.raises(cutting.CutError):cutting.open_session(bid,rid,sha)
    assert not next(a for a in w.detail(bid)['assets'] if a['request_id']==rid)['can_edit_video']


@pytest.mark.integration
def test_oss_failure_keeps_old_current_and_retry_works(ready,monkeypatch):
    session,job=rendered(ready);bid,rid,sha,original=ready
    monkeypatch.setattr(preview_oss,'enabled',lambda:True)
    monkeypatch.setattr(preview_oss,'_connect',lambda:object())
    def fail(*a,**k):raise RuntimeError('secret signed url must not leak')
    monkeypatch.setattr(preview_oss,'_ensure_uploaded',fail)
    result=replace(ready,session,job)
    assert result['replacement']['status']=='failed'
    assert 'secret' not in result['replacement']['error']
    assert w.preview_file(bid,rid)[0]==original
    monkeypatch.setattr(preview_oss,'enabled',lambda:False)
    assert replace(ready,session,job)['replacement']['status']=='completed'


@pytest.mark.integration
def test_upload_acceptance_cannot_race_replacement(ready,monkeypatch):
    import threading
    from exactcut import cutting_replace
    session,job=rendered(ready);bid,rid,sha,original=ready
    entered,finish=threading.Event(),threading.Event()
    real=cutting_replace.prepare
    def slow(*args):
        entered.set();assert finish.wait(5);return real(*args)
    monkeypatch.setattr(cutting_replace,'prepare',slow)
    cutting_replace.start(bid,session['id'],job['id'])
    try:
        assert entered.wait(2)
        with pytest.raises(ValueError):w.queue_publish(bid,rid,sha)
    finally:finish.set()
    for _ in range(200):
        if cutting.get_session(bid,session['id'])['jobs'][-1]['replacement']['status']!='running':break
        time.sleep(.05)


@pytest.mark.integration
def test_managed_replace_keeps_delivery_and_draft_identity(ready):
    bid,rid,sha,original=ready
    mv.enroll(w.folder(bid));before=mv.read(w.folder(bid))
    session,job=rendered(ready)
    result=replace(ready,session,job)
    assert result['replacement']['status']=='completed',result
    after=mv.read(w.folder(bid))
    assert after['draft']==before['draft'] and after['deliveries']==before['deliveries']
    assert after['assets'][rid]['current_version']!=before['assets'][rid]['current_version']
    assert w.preview_file(bid,rid)[1]['output_sha256']==job['output_sha256']
    assert file_sha256(original)==sha


@pytest.mark.integration
def test_actual_upload_pipeline_receives_replacement(ready,monkeypatch):
    from exactcut import publish
    session,job=rendered(ready);bid,rid,sha,original=ready
    assert replace(ready,session,job)['replacement']['status']=='completed'
    captured={}
    def publish_fixture(video,manifest,request):
        captured.update(path=video,sha=file_sha256(video),name=video.name)
        return {'feishu_record_id':'fixture-only','sha256':file_sha256(video),'oss_key':publish.OssPublisher().key_for(video)[0]}
    monkeypatch.setattr(publish,'publish_to_queue',publish_fixture)
    w.queue_publish(bid,rid,job['output_sha256'])
    w.upload(bid,rid)
    assert captured['sha']==job['output_sha256'] and captured['path']!=original
    assert captured['name']==original.name
    assert read_json(w.folder(bid)/'publications.json')[rid]['status']=='fbbm_queued'


@pytest.mark.integration
def test_corrupt_output_never_replaces(ready):
    session,job=rendered(ready);bid,rid,sha,original=ready
    (cutting.session_path(bid,session['id'])/(job['id']+'.mp4')).write_bytes(b'broken')
    assert replace(ready,session,job)['replacement']['status']=='failed'
    assert w.preview_file(bid,rid)[0]==original and file_sha256(original)==sha


@pytest.mark.integration
def test_interrupted_replacement_recovers_before_and_after_commit(ready):
    session,job=rendered(ready);bid,rid,sha,original=ready
    path=cutting.session_path(bid,session['id'])/'session.json'
    state=read_json(path);state['jobs'][-1]['replacement']={'status':'running'};write_json_atomic(path,state)
    assert cutting.get_session(bid,session['id'])['jobs'][-1]['replacement']['status']=='failed'
    assert replace(ready,session,job)['replacement']['status']=='completed'
    state=read_json(path);state['jobs'][-1]['replacement']={'status':'running'};write_json_atomic(path,state)
    assert cutting.get_session(bid,session['id'])['jobs'][-1]['replacement']['status']=='completed'


@pytest.mark.integration
def test_historical_delivery_in_results_also_locks_editing(ready):
    from exactcut import result_store, cutting_access
    bid,rid,sha,_=ready;batch=w.folder(bid)
    record=w.result_records(batch)[rid]
    record.update(status='fbbm_ready',fbbm={'video_id':'historical-fixture'})
    result_store.put(batch/'production',rid,record)
    assert not cutting_access.can_edit(batch,rid)
    with pytest.raises(cutting.CutError):cutting.open_session(bid,rid,sha)


@pytest.mark.integration
@pytest.mark.parametrize('control',['paused','archived'])
def test_managed_control_blocks_cutting(ready,control):
    from exactcut import cutting_access
    bid,rid,sha,_=ready;batch=w.folder(bid)
    mv.enroll(batch)
    state=mv.read(batch);state['control'][control]=True
    write_json_atomic(batch/'media-state.json',state)
    assert not cutting_access.can_edit(batch,rid)
    with pytest.raises(ValueError):cutting.open_session(bid,rid,sha)


@pytest.mark.integration
def test_managed_replacement_upload_uses_isolated_new_master(ready,monkeypatch):
    import uuid
    from exactcut import material_workers as worker,publish
    bid,rid,sha,original=ready;batch=w.folder(bid)
    mv.enroll(batch);session,job=rendered(ready)
    assert replace(ready,session,job)['replacement']['status']=='completed'
    monkeypatch.setattr(mv,'delivery_enabled',lambda:True)
    state=mv.read(batch);vid=state['assets'][rid]['current_version']
    state=mv.queue_delivery(batch,{'revision':state['revision'],'mutation_id':uuid.uuid4().hex,'request_id':rid,'version_id':vid,'sha256':job['output_sha256']})
    event=next(e for e in state['outbox'].values() if e['kind']=='delivery')
    child=worker.materialize(batch,event)
    captured={}
    def fake(video,manifest,request):
        captured.update(path=video,sha=file_sha256(video))
        return {'feishu_record_id':'fixture-only','sha256':file_sha256(video),'oss_key':publish.OssPublisher().key_for(video)[0]}
    monkeypatch.setattr(publish,'publish_to_queue',fake)
    assert w.upload(child.name,rid)==0
    assert captured['sha']==job['output_sha256']
    assert captured['path'].is_relative_to(child/'production/videos')
    assert 'edited_media' not in w.result_records(child)[rid]
    assert file_sha256(original)==sha


@pytest.mark.integration
def test_failure_before_delivery_intent_remains_editable(ready,monkeypatch):
    from exactcut import cutting_access
    bid,rid,sha,_=ready;batch=w.folder(bid)
    w.queue_publish(bid,rid,sha)
    with monkeypatch.context() as patch:
        patch.setattr(w,'preview_file',lambda *a,**k:(_ for _ in ()).throw(ValueError('fixture: local read failure')))
        assert w.upload(bid,rid)==2
    entry=read_json(batch/'publications.json')[rid]
    assert entry['intent']['status']=='preparation_failed'
    assert cutting_access.can_edit(batch,rid)


def test_replacement_releases_gate_even_if_lease_close_fails(tmp_path,monkeypatch):
    from exactcut import cutting_replace
    closed=[]
    class Guard:
        def __exit__(self,*args):closed.append(True)
    class Lease:
        def close(self):raise OSError('fixture close failure')
    def fail(*args):raise ValueError('fixture prepare failure')
    monkeypatch.setattr(cutting_replace,'prepare',fail)
    monkeypatch.setattr(cutting_replace,'current_is_job',lambda *a:False)
    monkeypatch.setattr(cutting_replace,'update_job',lambda *a:None)
    with pytest.raises(OSError):
        cutting_replace.run(tmp_path,{'id':'session'},{'id':'job'},Guard(),Lease())
    assert closed==[True]


def test_lock_file_closes_stream_on_other_flock_errors(tmp_path,monkeypatch):
    import io
    from pathlib import Path
    stream=io.StringIO()
    def fail(*args):raise OSError('fixture flock error')
    monkeypatch.setattr(Path,'open',lambda *a,**k:stream)
    monkeypatch.setattr(cutting.fcntl,'flock',fail)
    with pytest.raises(OSError):cutting.lock_file(tmp_path/'test.lock')
    assert stream.closed


@pytest.mark.integration
def test_discard_keeps_original_and_invalidates_recipe_and_scan(ready):
    from exactcut import cutting_scan,cutting_replace
    from test_cutting_api import call,serve
    session,job=rendered(ready);bid,rid,sha,original=ready
    previous_scan=cutting_scan.report_path(bid,session['id'],'standard')
    write_json_atomic(previous_scan,{'status':'completed','risk_events':[]})
    app=serve()
    try:
        code,state=call(app,'POST',f"/api/batches/{bid}/cuts/{session['id']}/discard",{})
        assert code==200,state
    finally:app.shutdown();app.server_close()
    assert state['reset_at'] and all(j['discarded'] for j in state['jobs'])
    assert w.preview_file(bid,rid)[0]==original and file_sha256(original)==sha
    assert cutting_scan.report_path(bid,session['id'],'standard')!=previous_scan
    assert cutting_scan.state(bid,session['id'],'standard')['report'] is None
    with pytest.raises(cutting.CutError):cutting_replace.start(bid,session['id'],job['id'])
    assert cutting.open_session(bid,rid,sha)['reset_at']==state['reset_at']
    new_session,new_job=rendered(ready)
    assert new_job['id']!=job['id'] and not new_job.get('discarded')


@pytest.mark.integration
def test_discard_rejects_live_generation(ready):
    session,job=rendered(ready);bid=ready[0]
    path=cutting.session_path(bid,session['id'])
    with cutting.lock_file(path/(job['id']+'.lock')):
        state=read_json(path/'session.json');state['jobs'][-1]['status']='running';write_json_atomic(path/'session.json',state)
        with pytest.raises(cutting.CutError,match='处理中'):cutting.discard(bid,session['id'])
        assert not read_json(path/'session.json').get('reset_at')
