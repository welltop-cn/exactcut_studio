"""Cold exact frames wait durably; hot images do not wait for execution slots."""
from contextlib import ExitStack
import time
import pytest
from exactcut import cutting,mosaic_session,editor_resources,interactive_queue
from test_phase2_preview_api import fixture,serve
from test_cutting_api import ready
from test_mosaic_interactive_admission import call_as
from test_blank_batch_api import call as raw_call


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
