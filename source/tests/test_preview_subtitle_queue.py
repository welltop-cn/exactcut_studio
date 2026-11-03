"""Durable FIFO queue: ordering, idempotency, recovery and paid-call safety."""
import json
import threading
import time

import pytest

from exactcut import accounts
from exactcut import preview_subtitle_queue as queue
from exactcut import preview_subtitle_model as model
from exactcut import preview_subtitles as subtitles
from exactcut import workflow as w
from exactcut.util import read_json, write_json_atomic
from test_phase2_preview_api import fixture, serve
from test_cutting_api import ready, call
from test_preview_subtitles import CUE, configured, dispatcher, route, wait  # noqa: F401

pytestmark = pytest.mark.integration


def descriptor(number, actor='admin'):
    return {'task_id':f'{number:064x}','batch_id':'a'*32,'request_id':f'R{number}',
            'source_sha256':'b'*64,'version_id':None,'actor':actor,'settings_sha':'c'*64}


def test_fifo_capacity_and_duplicate_admission_stay_one_task(configured):
    seen=[]
    first=queue.enqueue(descriptor(1),3,lambda entry:seen.append(('queued',entry['task_id'])))
    second=queue.enqueue(descriptor(2),3)
    assert first['position']==1 and second['position']==2 and seen[0][0]=='queued'
    # A repeated submit of the same binding reuses the pending slot.
    assert queue.enqueue(descriptor(1),3)['position']==1
    assert [item['task_id'] for item in queue.snapshot()['pending']]==[descriptor(1)['task_id'],descriptor(2)['task_id']]
    third=queue.enqueue(descriptor(3),3)
    assert third['position']==3
    with pytest.raises(queue.QueueError) as full:
        queue.enqueue(descriptor(4),3)
    assert full.value.status==429 and '排队已满' in str(full.value)
    assert queue.claim()['task_id']==descriptor(1)['task_id']
    # Only one task may ever be active; repeats of it are reported as active.
    assert queue.claim() is None
    assert queue.enqueue(descriptor(1),3)=={'status':'active','position':0}
    assert queue.membership(descriptor(1)['task_id'])['active']
    assert queue.membership(descriptor(2)['task_id'])['position']==1
    assert queue.finish(descriptor(1)['task_id'])
    assert queue.claim()['task_id']==descriptor(2)['task_id']
    assert queue.snapshot()['active']['task_id']==descriptor(2)['task_id']


def test_dispatcher_runs_fifo_and_keeps_going_after_a_failure(configured, dispatcher):
    order=[]
    def run(item, stop):
        order.append(item['task_id'])
        if item['task_id']==descriptor(1)['task_id']:
            raise RuntimeError('provider exploded')
        time.sleep(.02)
    queue.enqueue(descriptor(1),20);queue.enqueue(descriptor(2),20);queue.enqueue(descriptor(3),20)
    dispatcher(run)
    for _ in range(200):
        if len(order)==3 and queue.snapshot()['active'] is None:break
        time.sleep(.01)
    assert order==[descriptor(n)['task_id'] for n in (1,2,3)]
    assert queue.snapshot()['pending']==[] and queue.snapshot()['active'] is None


def test_queued_survives_restart_and_runs_once(configured, dispatcher):
    runs=[]
    queue.enqueue(descriptor(1),20);queue.enqueue(descriptor(2),20)
    # No dispatcher was alive, so the queue is untouched and durable.
    assert [item['order'] for item in queue.snapshot()['pending']]==[1,2]
    dispatcher(lambda item,stop:runs.append(item['task_id']))
    for _ in range(200):
        if len(runs)==2:break
        time.sleep(.01)
    assert runs==[descriptor(1)['task_id'],descriptor(2)['task_id']]
    assert queue.snapshot()['pending']==[]

    # A second process-level lease must not take over a live dispatcher.
    second=queue.Dispatcher(poll_seconds=.02)
    assert second.start() is False


def test_stranded_active_task_fails_without_repaying(configured, monkeypatch, dispatcher):
    bid,rid,sha,_=configured
    subtitles.start(bid,rid,sha,None,'admin')
    identifier=queue.snapshot()['pending'][0]['task_id']
    path=subtitles.report_dir(bid,identifier)/'report.json'
    # Simulate a crash after dequeue: active set, running report, no live worker lease.
    queue.claim()
    report=read_json(path);report['status']='running';write_json_atomic(path,report)
    calls=[]
    monkeypatch.setattr(model,'recognize',lambda *a: calls.append(True) or [CUE])
    dispatcher()
    for _ in range(200):
        if queue.snapshot()['active'] is None:break
        time.sleep(.01)
    assert queue.snapshot()['active'] is None
    report=subtitles.state(bid,rid,sha,None)['report']
    assert report['status']=='failed' and not report['cues'] and not calls


def test_half_written_enqueue_and_dequeue_never_lose_or_rebill(configured, dispatcher):
    bid,rid,sha,_=configured
    subtitles.start(bid,rid,sha,None,'admin')
    identifier=queue.snapshot()['pending'][0]['task_id']
    path=subtitles.report_dir(bid,identifier)/'report.json'
    # Crash between the queue.json append and the queued report write: the task is
    # still scheduled exactly once, but its report never claimed a result.
    path.unlink()
    runs=[]
    dispatcher(lambda item,stop:runs.append(item['task_id']))
    for _ in range(200):
        if runs:break
        time.sleep(.01)
    assert runs==[identifier]
    for _ in range(200):
        if queue.snapshot()['active'] is None:break
        time.sleep(.01)
    assert queue.snapshot()['active'] is None and queue.snapshot()['pending']==[]
    assert runs==[identifier]


def test_completed_report_with_stale_active_is_cleared_without_a_second_run(configured, dispatcher):
    bid,rid,sha,_=configured
    subtitles.start(bid,rid,sha,None,'admin')
    identifier=queue.snapshot()['pending'][0]['task_id']
    path=subtitles.report_dir(bid,identifier)/'report.json'
    # Crash after the final report but before the active slot was released.
    queue.claim()
    report=read_json(path)
    report.update(status='completed',cues=[CUE])
    write_json_atomic(path,report)
    runs=[]
    dispatcher(lambda item,stop:runs.append(item['task_id']))
    for _ in range(200):
        if not runs and queue.snapshot()['active'] is None:break
        time.sleep(.01)
    assert runs==[] and queue.snapshot()['active'] is None
    assert subtitles.state(bid,rid,sha,None)['report']['status']=='completed'


def test_get_never_enqueues_and_queued_in_flight_is_not_failed(configured):
    bid,rid,sha,_=configured
    app=serve();base=f'/api/batches/{bid}/preview-subtitles/{rid}'
    try:
        assert call(app,'GET',route(bid,rid,sha))[1]['report']['status']=='idle'
        assert queue.snapshot()['pending']==[]
        body={'source_sha256':sha,'version_id':None}
        code,queued=call(app,'POST',base,body)
        assert code==202 and queued['report']['status']=='queued' and queued['report']['queue_position']==1
        for _ in range(3):
            state=call(app,'GET',route(bid,rid,sha))[1]['report']
            assert state['status']=='queued' and state['queue_position']==1
        assert len(queue.snapshot()['pending'])==1
        # Duplicate POSTs from another tab reuse the same queued task.
        assert call(app,'POST',base,body)[1]['report']['id']==queued['report']['id']
        assert len(queue.snapshot()['pending'])==1
        # A dequeue in flight (active, report still queued) must not read as interrupted.
        queue.claim()
        assert call(app,'GET',route(bid,rid,sha))[1]['report']['status']=='queued'
        # Torn away from both pending and active: an honest, explicit failure.
        queue._save({'schema_version':1,'sequence':1,'active':None,'pending':[]})
        assert call(app,'GET',route(bid,rid,sha))[1]['report']['status']=='failed'
    finally:
        app.shutdown();app.server_close()


def test_actor_comes_from_the_session_not_the_request_body(configured):
    bid,rid,sha,_=configured
    app=serve();base=f'/api/batches/{bid}/preview-subtitles/{rid}'
    try:
        assert call(app,'POST',base,{'source_sha256':sha,'version_id':None,'actor':'someone-else'})[0]==202
        assert queue.snapshot()['pending'][0]['actor']=='admin'
    finally:
        app.shutdown();app.server_close()


def test_dequeue_revalidates_account_acl_source_and_configuration(configured, monkeypatch, dispatcher):
    bid,rid,sha,_=configured
    db_path=w.BATCHES.parent/'accounts.sqlite3'
    accounts.initialize(db_path,'x'*24)
    with accounts.connect(db_path) as db:
        db.execute("INSERT INTO users VALUES ('editor','编辑','user','s','h',1)")
    accounts.assign(w.folder(bid),{'username':'editor','role':'user'})
    calls=[]
    monkeypatch.setattr(model,'recognize',lambda *a: calls.append(True) or [CUE])

    def drain():
        for _ in range(300):
            if queue.snapshot()['active'] is None and not queue.snapshot()['pending']:break
            time.sleep(.01)

    # Account disabled while queued: the task fails instead of recognizing.
    subtitles.start(bid,rid,sha,None,'editor')
    with accounts.connect(db_path) as db:
        db.execute("UPDATE users SET enabled=0 WHERE username='editor'")
    dispatcher();drain()
    report=subtitles.state(bid,rid,sha,None)['report']
    assert report['status']=='failed' and '账号' in report['error'] and not calls

    # Re-enabled but the project was handed to someone else: still no paid call.
    with accounts.connect(db_path) as db:
        db.execute("UPDATE users SET enabled=1 WHERE username='editor'")
    write_json_atomic(w.folder(bid)/'access.json',{'owner':'admin'})
    subtitles.start(bid,rid,sha,None,'editor');drain()
    report=subtitles.state(bid,rid,sha,None)['report']
    assert report['status']=='failed' and '无权' in report['error'] and not calls

    # Source replaced while queued: the stale task is rejected, never recharged.
    accounts.assign(w.folder(bid),{'username':'editor','role':'user'})
    identifier=subtitles.start(bid,rid,sha,None,'editor')['report']['id']
    result=read_json(w.folder(bid)/'production/results.json')
    result['requests'][rid]['output_sha256']='b'*64
    write_json_atomic(w.folder(bid)/'production/results.json',result)
    drain()
    report=read_json(subtitles.report_dir(bid,identifier)/'report.json')
    assert report['status']=='failed' and '已变化' in report['error'] and not calls
    result['requests'][rid]['output_sha256']=sha
    write_json_atomic(w.folder(bid)/'production/results.json',result)


def test_recognition_identity_ignores_scheduling_config_but_follows_output_config():
    binding={'batch_id':'a'*32,'request_id':'R1','source_sha256':'b'*64,'version_id':None}
    cfg=subtitles.settings()
    baseline=subtitles.identity(binding,cfg)
    for key,value in [('max_queued_tasks',3),('max_active_tasks',1),('enabled',False),
                      ('request_timeout_ms',9000),('total_timeout_seconds',600),
                      ('max_source_bytes',2048),('max_cues',5),('prepare_timeout_seconds',9)]:
        changed={**cfg,key:value}
        assert subtitles.identity(binding,changed)==baseline,key
    for key,value in [('model','other-model'),('sample_fps',2),('chunk_seconds',30),('proxy_crf',18)]:
        changed={**cfg,key:value}
        assert subtitles.identity(binding,changed)!=baseline,key
    other={**binding,'source_sha256':'c'*64}
    assert subtitles.identity(other,cfg)!=baseline


def test_unrelated_reports_and_batches_cannot_read_cached_cues(configured, monkeypatch, dispatcher):
    bid,rid,sha,source=configured
    monkeypatch.setattr(model,'recognize',lambda *a:[CUE])
    subtitles.start(bid,rid,sha,None,'admin');dispatcher()
    assert wait(bid,rid,sha)['cues']==[CUE]
    other=w.create_blank()
    app=serve()
    try:
        assert call(app,'GET',route(other['id'],rid,sha))[0]==404
        monkeypatch.setattr(app.RequestHandlerClass,'can_access',lambda self,bid:False)
        assert call(app,'GET',route(bid,rid,sha))[0]==404
    finally:
        app.shutdown();app.server_close()
    assert queue.snapshot()['pending']==[]


def test_stop_timeout_keeps_dispatcher_lease_until_worker_finishes(configured):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def run(item, stop):
        calls.append(item['task_id'])
        entered.set()
        assert release.wait(3)

    first = queue.Dispatcher(run=run, poll_seconds=.01)
    second = queue.Dispatcher(run=lambda item, stop: calls.append(item['task_id']),
                              poll_seconds=.01)
    queue.enqueue(descriptor(1), 20)
    queue.enqueue(descriptor(2), 20)
    try:
        assert first.start()
        assert entered.wait(1)
        first.stop(timeout=.01)
        assert first.running
        assert second.start() is False
        assert queue.snapshot()['active']['task_id'] == descriptor(1)['task_id']
        assert calls == [descriptor(1)['task_id']]
        release.set()
        first.stop(timeout=1)
        assert not first.running
        assert second.start()
        for _ in range(100):
            if len(calls) == 2:
                break
            time.sleep(.01)
        assert calls == [descriptor(1)['task_id'], descriptor(2)['task_id']]
    finally:
        release.set()
        first.stop(timeout=1)
        second.stop(timeout=1)


def test_recovery_retains_active_if_final_report_cannot_be_written(configured, monkeypatch):
    queue.enqueue(descriptor(1), 20)
    queue.claim()

    def cannot_write(item):
        raise OSError('storage temporarily unavailable')

    monkeypatch.setattr(queue, '_interrupt', cannot_write)
    with pytest.raises(OSError):
        queue.recover()
    assert queue.snapshot()['active']['task_id'] == descriptor(1)['task_id']


def test_failed_recovery_releases_lease_without_dropping_active(configured, monkeypatch):
    queue.enqueue(descriptor(1), 20)
    queue.claim()
    calls = []
    owner = queue.Dispatcher(run=lambda item, stop: calls.append(item['task_id']),
                             poll_seconds=.01)

    def cannot_write(item):
        raise OSError('storage temporarily unavailable')

    monkeypatch.setattr(queue, '_interrupt', cannot_write)
    assert owner.start() is False
    assert not owner.running
    assert queue.snapshot()['active']['task_id'] == descriptor(1)['task_id']
    monkeypatch.setattr(queue, '_interrupt', lambda item: None)
    queue.enqueue(descriptor(2), 20)
    try:
        assert owner.start()
        for _ in range(100):
            if calls:
                break
            time.sleep(.01)
        assert calls == [descriptor(2)['task_id']]
    finally:
        owner.stop(timeout=1)
