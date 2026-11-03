"""Queue admission, immutable editing and cancellation over real HTTP/media."""
from contextlib import ExitStack
import shutil
import threading
import time
import pytest
from exactcut import editor_resources, accounts, workflow, cutting, mosaic_queue as queue, mosaic_render as render
from exactcut import mosaic_session as sessions, material_versions
from exactcut.util import read_json, write_json_atomic, file_sha256
from test_cutting_api import ready, call, prepared, exact_frame
from test_phase2_preview_api import fixture, serve


def wait(bid,sid,jid):
    for _ in range(500):
        job=render.state(bid,sid,jid)
        if job['status'] not in {'queued','running','cancelling'}:return job
        time.sleep(.02)
    pytest.fail('background task did not finish')


def second_asset(bid,source,sha):
    batch=workflow.folder(bid)
    manifest=workflow.read_json(batch/'confirmed.json');rows=manifest['requests']
    other_request={**rows[0],'request_id':'queue-second','seq':'2','output_filename':rows[0]['output_filename'].replace('_1_#','_2_#')}
    rows.append(other_request);write_json_atomic(batch/'confirmed.json',manifest)
    document=read_json(batch/'draft.json')
    document['draft']['rows'].append({**document['draft']['rows'][0],'request_id':'queue-second','seq':'2'})
    write_json_atomic(batch/'draft.json',document)
    rid=other_request['request_id'];other=batch/'production/videos'/other_request['output_filename']
    shutil.copyfile(source,other)
    records=read_json(batch/'production/results.json');records['requests'][rid]={'status':'qc_passed','output_sha256':sha,'duration_ms':3000}
    write_json_atomic(batch/'production/results.json',records)
    return rid


@pytest.mark.integration
@pytest.mark.parametrize('managed',[False,True])
def test_compute_busy_open_another_submit_restore_and_freeze(ready,managed):
    bid,rid,sha,source=ready;rid2=second_asset(bid,source,sha)
    batch=workflow.folder(bid)
    if managed:material_versions.enroll(batch)
    accounts.initialize(workflow.BATCHES.parent/'accounts.sqlite3','queue-integration-password')
    app=serve();root=f'/api/batches/{bid}/mosaics'
    dispatcher=queue.Dispatcher(.01)
    try:
        with ExitStack() as stack:
            for _ in range(editor_resources.settings()['final_media_tasks']):
                stack.enter_context(editor_resources.acquire('final_media'))
            dispatcher.start()
            code,a=call(app,'POST',root+'/open',{'request_id':rid,'source_sha256':sha})
            assert code in {200,202};a=prepared(app,root,a)
            path=root+'/'+a['id'];body={'targets':[],'ranges':[[1,3]],'reset_at':''}
            code,job=call(app,'POST',path+'/render',body);assert code==202,job
            assert job['actor']=='admin'
            assert call(app,'POST',path+'/render',{**body,'actor':'forged'})[1]['id']==job['id']
            assert call(app,'POST',path+'/render',{**body,'ranges':[[4,6]]})[0]==409
            assert call(app,'POST',path+'/scan',{'reset_at':''})[0]==409
            assert call(app,'POST',f'/api/batches/{bid}/cuts/open',{'request_id':rid,'source_sha256':sha})[0]==409
            assert call(app,'GET',path+'/source')[0]==200
            assert exact_frame(app,path+'/frame/2')[0]==200
            assert call(app,'POST',root+'/open',{'request_id':rid,'source_sha256':sha})[0]==200
            if managed:
                import copy,uuid
                snapshot=material_versions.read(batch);draft=copy.deepcopy(snapshot['draft'])
                draft['rows'][1]['notes']='other material remains editable'
                saved=material_versions.save_draft(batch,{'draft':draft,'revision':snapshot['revision'],'mutation_id':uuid.uuid4().hex})
                assert saved['assets'][rid2]['row']['notes']=='other material remains editable'
                for change in ['same_asset','delete_asset','project_metadata']:
                    candidate=copy.deepcopy(saved['draft'])
                    if change=='same_asset':candidate['rows'][0]['notes']='must be frozen'
                    elif change=='delete_asset':candidate['rows']=candidate['rows'][1:]
                    else:candidate['drama_title']='must be frozen'
                    with pytest.raises(cutting.CutError):
                        material_versions.save_draft(batch,{'draft':candidate,'revision':saved['revision'],'mutation_id':uuid.uuid4().hex})
                with pytest.raises(cutting.CutError):material_versions.control(batch,'archive',{'revision':saved['revision'],'mutation_id':uuid.uuid4().hex})
            else:
                from exactcut import batch_control
                with pytest.raises(cutting.CutError):batch_control.change(batch,'archive')
            before=time.monotonic()
            code,b=call(app,'POST',root+'/open',{'request_id':rid2,'source_sha256':sha});assert code in {200,202},b
            b=prepared(app,root,b)
            assert time.monotonic()-before<5
            code,job2=call(app,'POST',root+'/'+b['id']+'/render',body);assert code==202,job2
            assert len(queue.load()['tasks'])==2
            with pytest.raises(cutting.CutError):
                with __import__('exactcut.cutting_access',fromlist=['gate']).gate(batch):pass
            assert call(app,'POST',path+'/stop',{'job_id':job['id'],'reset_at':''})[0]==202
        assert wait(bid,a['id'],job['id'])['status']=='cancelled'
        done=wait(bid,b['id'],job2['id']);assert done['status']=='completed',done
        assert file_sha256(source)==sha
        code,restored=call(app,'GET',root+'/'+b['id']+'/render');assert code==200
        assert restored['job']['ranges']==[[1,3]]
        assert call(app,'POST',root+'/'+b['id']+'/discard',{'reset_at':''})[0]==409
        assert call(app,'POST',root+'/'+b['id']+'/discard-result',{'job_id':job2['id'],'reset_at':''})[0]==200
        code,retry=call(app,'POST',root+'/'+b['id']+'/render',body);assert code==202
        assert retry['id']!=job2['id']
        assert wait(bid,b['id'],retry['id'])['status']=='completed'
    finally:
        dispatcher.stop();app.shutdown();app.server_close()


@pytest.mark.integration
def test_submission_receipt_is_persisted_and_validated(ready):
    bid,rid,sha,source=ready;app=serve()
    try:
        code,session=call(app,'POST',f'/api/batches/{bid}/mosaics/open',{'request_id':rid,'source_sha256':sha})
        assert code in {200,202}
        session=prepared(app,f'/api/batches/{bid}/mosaics',session)
        endpoint=f"/api/batches/{bid}/mosaics/{session['id']}/render"
        body={'targets':[],'ranges':[[1,3]],'reset_at':'','submission_id':'a'*32}
        assert call(app,'POST',endpoint,{**body,'submission_id':'invalid'})[0]==400
        code,job=call(app,'POST',endpoint,body);assert code==202
        assert job['submission_id']==body['submission_id']
        assert call(app,'GET',endpoint)[1]['job']['submission_id']==body['submission_id']
        assert queue.load()['tasks'][0]['job']['submission_id']==body['submission_id']
        code,replayed=call(app,'POST',endpoint,{**body,'submission_id':'b'*32})
        assert code==202 and replayed['id']==job['id']
        assert file_sha256(source)==sha
    finally:app.shutdown();app.server_close()


def test_three_users_rotate_without_starvation(tmp_path,monkeypatch):
    monkeypatch.setattr(workflow,'BATCHES',tmp_path/'batches')
    for actor in ['a','b','c']:
        for i in range(3):
            jid=actor+str(i)
            queue.enqueue('1'*32,'2'*32,jid,actor,{'id':jid,'status':'queued'})
    sequence=[]
    while item:=queue.claim():
        sequence.append((item['actor'],item['id']));queue.finish(item['id'])
    assert [a for a,j in sequence]==['a','b','c']*3
    assert [j for a,j in sequence if a=='b']==['b0','b1','b2']


@pytest.mark.integration
def test_dispatcher_lease_excludes_second_owner_and_retains_worker(ready,monkeypatch):
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha)
    accounts.initialize(workflow.BATCHES.parent/'accounts.sqlite3','queue-integration-password')
    first,second=queue.Dispatcher(.01),queue.Dispatcher(.01)
    entered,release=threading.Event(),threading.Event()
    original=render.process_video
    def slow(*a,**kw):
        entered.set();assert release.wait(5);return original(*a,**kw)
    monkeypatch.setattr(render,'process_video',slow)
    try:
        assert first.start();assert not second.start()
        job=render.start(bid,session['id'],[],'',[[1,3]])
        assert entered.wait(3)
        first.stop(timeout=.01)
        assert not second.start()
        release.set()
        assert wait(bid,session['id'],job['id'])['status']=='completed'
        first.stop()
        assert second.start()
    finally:
        release.set();first.stop();second.stop()


@pytest.mark.integration
@pytest.mark.parametrize('failure',['disabled_actor','changed_version','disk_full','missing_input'])
def test_invalid_queued_task_fails_without_replacing_original(ready,monkeypatch,failure):
    from exactcut import storage
    bid,rid,sha,source=ready;session=sessions.open_session(bid,rid,sha)
    db=workflow.BATCHES.parent/'accounts.sqlite3';accounts.initialize(db,'isolated-queue-password')
    job=render.start(bid,session['id'],[],'',[[1,3]])
    if failure=='disabled_actor':monkeypatch.setattr(accounts,'get',lambda *a:None)
    elif failure=='changed_version':
        records=read_json(source.parent.parent/'results.json');records['requests'][rid]['output_sha256']='f'*64
        write_json_atomic(source.parent.parent/'results.json',records)
    elif failure=='disk_full':
        def full(*a,**kw):raise cutting.CutError('磁盘空间不足',507)
        monkeypatch.setattr(storage,'require_space',full)
    else:(sessions.session_path(bid,session['id'])/'source.mp4').unlink()
    dispatcher=queue.Dispatcher(.01)
    try:
        dispatcher.start()
        done=wait(bid,session['id'],job['id'])
        assert done['status']=='failed',done
        assert not queue.blocked(bid,rid)
        assert file_sha256(source)==sha
    finally:dispatcher.stop(timeout=None)


@pytest.mark.integration
def test_two_authenticated_users_fifo_fairness_and_actual_ffmpeg(ready,monkeypatch):
    import json,uuid
    from http.client import HTTPConnection
    bid,rid,sha,source=ready;second=second_asset(bid,source,sha);batch=workflow.folder(bid)
    document=read_json(batch/'draft.json');manifest=read_json(batch/'confirmed.json');records=read_json(batch/'production/results.json')
    for number in (3,4):
        request={**manifest['requests'][0],'request_id':f'queue-{number}','seq':str(number),'output_filename':manifest['requests'][0]['output_filename'].replace('_1_#',f'_{number}_#')}
        manifest['requests'].append(request)
        document['draft']['rows'].append({**document['draft']['rows'][0],'request_id':request['request_id'],'seq':str(number)})
        shutil.copyfile(source,source.parent/request['output_filename'])
        records['requests'][request['request_id']]={'status':'qc_passed','output_sha256':sha,'duration_ms':3000}
    write_json_atomic(batch/'draft.json',document);write_json_atomic(batch/'confirmed.json',manifest);write_json_atomic(batch/'production/results.json',records)
    db=workflow.BATCHES.parent/'accounts.sqlite3';accounts.initialize(db,'two-actor-admin-password')
    accounts.manage(db,'admin',{'action':'create','username':'queue_alice','name':'Queue Alice','password':'two-actor-alice-password'})
    accounts.assign(batch,{'username':'queue_alice'})
    app=serve();app.auth_db=db
    def request(method,path,body,cookie=None):
        conn=HTTPConnection(*app.server_address,timeout=10)
        headers={'X-Exactcut-Request':'1','Content-Type':'application/json'}
        if cookie:headers['Cookie']=cookie
        try:
            conn.request(method,path,json.dumps(body) if body is not None else None,headers)
            response=conn.getresponse();return response.status,json.loads(response.read()),response.getheader('Set-Cookie')
        finally:conn.close()
    order=[];active=0;peak=0;guard=threading.Lock();sid_owners={};original=render.process_video
    def process(src,*args,**kwargs):
        nonlocal active,peak
        with guard:active+=1;peak=max(peak,active);order.append(sid_owners[src.parent.name])
        try:return original(src,*args,**kwargs)
        finally:
            with guard:active-=1
    monkeypatch.setattr(render,'process_video',process)
    dispatcher=queue.Dispatcher(.01);jobs=[]
    try:
        admin=request('POST','/api/login',{'username':'admin','password':'two-actor-admin-password'})[2].split(';')[0]
        alice=request('POST','/api/login',{'username':'queue_alice','password':'two-actor-alice-password'})[2].split(';')[0]
        for actor,cookie,request_id in [('queue_alice',alice,rid),('queue_alice',alice,second),('admin',admin,'queue-3'),('admin',admin,'queue-4')]:
            code,session,_=request('POST',f'/api/batches/{bid}/mosaics/open',{'request_id':request_id,'source_sha256':sha},cookie);assert code in {200,202},session
            deadline=time.monotonic()+5
            while session.get('status') in {'queued','running','cancelling'}:
                assert time.monotonic()<deadline;time.sleep(.01)
                code,session,_=request('GET',f"/api/batches/{bid}/mosaics/preparation/{session['id']}",None,cookie);assert code==200
            if session.get('status'):
                assert session['status']=='completed',session;session=session['result']['session']
            sid=session['id'];sid_owners[sid]=(actor,request_id)
            code,job,_=request('POST',f'/api/batches/{bid}/mosaics/{sid}/render',{'targets':[],'ranges':[[1,3]],'actor':'forged'},cookie)
            assert code==202,job;assert job['actor']==actor
            jobs.append((sid,job['id']))
        accounts.manage(db,'admin',{'action':'create','username':'queue_bob','name':'Queue Bob','password':'two-actor-bob-password'})
        outsider=request('POST','/api/login',{'username':'queue_bob','password':'two-actor-bob-password'})[2].split(';')[0]
        first_sid,first_jid=jobs[0];endpoint=f'/api/batches/{bid}/mosaics/{first_sid}'
        for method,action,body in [('GET','render/'+first_jid,None),('POST','stop',{'job_id':first_jid}),('POST','replace',{'job_id':first_jid})]:
            assert request(method,endpoint+'/'+action,body,outsider)[0]==404
        dispatcher.start()
        for sid,jid in jobs:assert wait(bid,sid,jid)['status']=='completed'
        assert peak==1
        assert order==[('queue_alice',rid),('admin','queue-3'),('queue_alice',second),('admin','queue-4')]
        assert file_sha256(source)==sha
    finally:dispatcher.stop(timeout=None);app.shutdown();app.server_close()

from interactive_test_support import interactive_dispatcher
