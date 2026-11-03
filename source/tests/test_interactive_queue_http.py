"""Real authenticated HTTP admission persists work while execution is occupied."""
from contextlib import ExitStack
import pytest
from exactcut import editor_resources,interactive_queue,mosaic_session,cutting
from test_phase2_preview_api import fixture,serve,call
from test_cutting_api import ready
from test_mosaic_interactive_admission import enable_models


@pytest.mark.parametrize('operation,role',[('scan','scan'),('locate','scan'),('clip-preview','preview'),('track','preview'),('cuts-scan','scan')])
def test_full_execution_pool_returns_persisted_queued_receipt(ready,tmp_path,monkeypatch,operation,role):
    bid,rid,sha,_=ready;enable_models(tmp_path,monkeypatch)
    session=(cutting if operation=='cuts-scan' else mosaic_session).open_session(bid,rid,sha)
    prefix=f"/api/batches/{bid}/{'cuts' if operation=='cuts-scan' else 'mosaics'}/{session['id']}"
    target={'start':0,'end':2,'seed_frame':0,'x':12,'y':23,'w':35,'h':40}
    payload={'scan':{'reset_at':''},'cuts-scan':{'policy':'standard'},'locate':{'start':0,'end':2,'seed_frame':0,'reset_at':''},'clip-preview':{'targets':[target],'reset_at':''},'track':{'target':target,'reset_at':''}}[operation]
    url=prefix+('/scan' if operation=='cuts-scan' else '/'+operation);server=serve()
    try:
        with ExitStack() as stack:
            for _ in range(2):stack.enter_context(editor_resources.acquire(role,'admin'))
            code,job=call(server,'POST',url,payload)
            assert code==202 and job['status']=='queued',job
            code,repeated=call(server,'POST',url,payload)
            assert code==202 and repeated['id']==job['id']
            assert len(interactive_queue.snapshot()['pending'])==1
    finally:server.shutdown();server.server_close()
