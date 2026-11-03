"""Cold exact frames wait durably; hot images do not wait for execution slots."""
from contextlib import ExitStack
import time
import pytest
from exactcut import cutting,mosaic_session,editor_resources,interactive_queue
from test_phase2_preview_api import fixture,serve
from test_cutting_api import ready
from test_mosaic_interactive_admission import call_as
from test_blank_batch_api import call as raw_call


@pytest.mark.integration
@pytest.mark.parametrize('family',['cuts','mosaics'])
def test_frame_get_queues_while_short_pool_full_and_serves_hot_bytes(ready,family):
    from test_phase2_preview_api import call
    bid,rid,sha,_=ready;module=cutting if family=='cuts' else mosaic_session;session=module.open_session(bid,rid,sha)
    endpoint=f"/api/batches/{bid}/{family}/{session['id']}/frame/5";server=serve();dispatcher=interactive_queue.Dispatcher(poll_seconds=.01)
    try:
        with ExitStack() as held:
            for _ in range(2):held.enter_context(editor_resources.acquire('frame'))
            code,job=call(server,'GET',endpoint)
            assert code==202 and job['status']=='queued'
            assert dispatcher.start();time.sleep(.05);assert dispatcher.active_workers==0
        deadline=time.monotonic()+5
        while interactive_queue.snapshot()['pending'] or interactive_queue.snapshot()['active']:
            assert time.monotonic()<deadline;time.sleep(.02)
        from test_blank_batch_api import call as binary_call
        with ExitStack() as held:
            for _ in range(2):held.enter_context(editor_resources.acquire('frame'))
            started=time.monotonic();code,headers,data=binary_call(server,'GET',endpoint)
            assert code==200 and data[:2]==b'\xff\xd8' and time.monotonic()-started<1
    finally:dispatcher.stop(timeout=None);server.shutdown();server.server_close()


def _frame_job(module,bid,sid,jid):
    from exactcut.util import read_json
    return read_json(module.session_path(bid,sid)/'frame-tasks'/(jid+'.json'))


def _wait_job(module,bid,sid,jid,statuses=('completed','failed','cancelled'),seconds=15):
    deadline=time.monotonic()+seconds
    while True:
        job=_frame_job(module,bid,sid,jid)
        if job['status'] in statuses:return job
        assert time.monotonic()<deadline,job
        time.sleep(.02)


@pytest.mark.integration
def test_queued_frame_waits_for_busy_cache_and_other_cache_progresses(ready,monkeypatch):
    import json,threading
    from pathlib import Path
    from exactcut.util import json_sha256
    from test_phase2_preview_api import call
    from test_blank_batch_api import call as binary_call
    bid,rid,sha,_=ready
    cut_session=cutting.open_session(bid,rid,sha);cut_sid=cut_session['id']
    mosaic_sid=mosaic_session.open_session(bid,rid,sha)['id']
    entered,release=threading.Event(),threading.Event();calls=[]
    def frames(module):
        def run(command,timeout):
            calls.append((module.__name__,command))
            if module is cutting and len([c for c in calls if c[0]==cutting.__name__])==1:
                entered.set();assert release.wait(15)
            start=int(command[command.index('-start_number')+1]);count=int(command[command.index('-frames:v')+1])
            for n in range(start,start+count):Path(command[-1].replace('%d',str(n))).write_bytes(b'\xff\xd8frame')
        return run
    monkeypatch.setattr(cutting,'run',frames(cutting))
    monkeypatch.setattr(mosaic_session,'run',frames(mosaic_session))
    server=serve();dispatcher=interactive_queue.Dispatcher(poll_seconds=.01);assert dispatcher.start()
    try:
        code,first=call(server,'GET',f'/api/batches/{bid}/cuts/{cut_sid}/frame/5')
        assert code==202 and entered.wait(5)
        code,second=call(server,'GET',f'/api/batches/{bid}/cuts/{cut_sid}/frame/75')
        assert code==202
        time.sleep(2.4)  # past the old 2-second admission deadline
        waiting=_frame_job(cutting,bid,cut_sid,second['id'])
        assert waiting['status'] in {'queued','running'},waiting
        assert waiting['status']!='failed'
        code,third=call(server,'GET',f'/api/batches/{bid}/mosaics/{mosaic_sid}/frame/5')
        assert code==202
        mosaic_job=_wait_job(mosaic_session,bid,mosaic_sid,third['id'])
        assert mosaic_job['status']=='completed',mosaic_job
        code,_,data=binary_call(server,'GET',f'/api/batches/{bid}/mosaics/{mosaic_sid}/frame/5')
        assert code==200 and data.startswith(b'\xff\xd8')
        release.set()
        assert _wait_job(cutting,bid,cut_sid,first['id'])['status']=='completed'
        assert _wait_job(cutting,bid,cut_sid,second['id'])['status']=='completed'
        for index in (5,75):
            code,_,data=binary_call(server,'GET',f'/api/batches/{bid}/cuts/{cut_sid}/frame/{index}')
            assert code==200 and data.startswith(b'\xff\xd8')
        assert len([c for c in calls if c[0]==cutting.__name__])==2
    finally:
        release.set();dispatcher.stop(timeout=None);server.shutdown();server.server_close()


def test_frame_wait_is_cancelled_while_holding_contended_cache(tmp_path):
    from exactcut import processing_cancel
    lock=tmp_path/'cache.lock'
    with cutting.lock_file(lock):
        with processing_cancel.scope(lambda:True):
            with pytest.raises(processing_cancel.Cancelled):
                with cutting.frame_lease(lock,timeout=5):pytest.fail('cancelled waiter acquired lock')
