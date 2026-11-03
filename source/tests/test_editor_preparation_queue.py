"""The real /open routes queue snapshot copying; cached sessions stay immediate."""
from contextlib import ExitStack
import time
import pytest
from exactcut import editor_resources,interactive_queue,workflow,cutting,mosaic_session
from exactcut.util import file_sha256
from test_phase2_preview_api import fixture,serve,call
from test_cutting_api import ready


@pytest.mark.parametrize('family',['cuts','mosaics'])
def test_open_persists_while_copy_resources_full_then_real_snapshot_completes(ready,family):
    bid,rid,sha,original=ready;server=serve();dispatcher=interactive_queue.Dispatcher(poll_seconds=.01)
    url=f'/api/batches/{bid}/{family}';body={'request_id':rid,'source_sha256':sha}
    try:
        with ExitStack() as held:
            for _ in range(2):held.enter_context(editor_resources.acquire('preprocess'))
            started=time.monotonic();code,job=call(server,'POST',url+'/open',body)
            assert code==202 and job['status']=='queued' and time.monotonic()-started<1
            code,repeat=call(server,'POST',url+'/open',body)
            assert code==202 and repeat['id']==job['id']
            assert len(interactive_queue.snapshot()['pending'])==1
            assert dispatcher.start();assert dispatcher.active_workers==0
        deadline=time.monotonic()+5
        while True:
            code,report=call(server,'GET',url+'/preparation/'+job['id'])
            assert code==200
            if report['status'] not in {'queued','running'}:break
            assert time.monotonic()<deadline;time.sleep(.02)
        assert report['status']=='completed',report
        session=report['result']['session'];assert session['source_sha256']==sha
        module=cutting if family=='cuts' else mosaic_session
        copy=module.session_path(bid,session['id'])/'source.mp4'
        assert file_sha256(copy)==sha==file_sha256(original) and copy.stat().st_ino!=original.stat().st_ino
        code,warm=call(server,'POST',url+'/open',body)
        assert code==200 and warm['id']==session['id'] and 'metadata' in warm
        assert not interactive_queue.snapshot()['pending']
    finally:dispatcher.stop(timeout=None);server.shutdown();server.server_close()
