"""Queued Hook previews keep edits independent; CPU/model work is an explicit double."""
from contextlib import ExitStack
import threading,time,copy
from concurrent.futures import ThreadPoolExecutor
from exactcut import editor_resources,interactive_queue,hook_preview,workflow,webserver
from test_phase2_preview_api import fixture,serve,call


def test_six_hook_submissions_wait_without_compute_and_restore_by_job_id(fixture,monkeypatch):
    monkeypatch.setattr(webserver,'JOBS',workflow.ROOT/'.web-state/jobs')
    rows=[fixture]+[workflow.clone_batch(fixture['id'],purpose='copy') for _ in range(5)]
    condition=threading.Condition();entered=[];release=threading.Event()
    def compute(project,request,work):
        with condition:entered.append(request.request_id);condition.notify_all()
        assert release.wait(5)
        cache=work/'cache';cache.mkdir()
        return {'preview_key':'a'*64,'image':'data:image/png;base64,fixture','decision':{}},{'decision':{}},cache
    monkeypatch.setattr(hook_preview,'compute',compute)
    server=serve();dispatcher=interactive_queue.Dispatcher(poll_seconds=.01);receipts=[]
    try:
        with ExitStack() as held:
            for _ in range(2):held.enter_context(editor_resources.acquire('preview'))
            for row in rows:
                code,job=call(server,'POST',f"/api/batches/{row['id']}/hook-preview",{'revision':row['revision'],'index':0})
                assert code==202 and job['status']=='queued',job
                receipts.append(job)
            assert len(interactive_queue.snapshot()['pending'])==6 and not entered
            assert dispatcher.start()
            assert dispatcher.active_workers==0
        with condition:assert condition.wait_for(lambda:len(entered)==2,2)
        assert len(interactive_queue.snapshot()['pending'])==4
        # A different material can still save while both preview workers are busy.
        row=rows[-1];draft=copy.deepcopy(row['draft']);draft['rows'][0]['hook_text']='edited while queued'
        started=time.monotonic();code,saved=call(server,'POST',f"/api/batches/{row['id']}/save",{'revision':row['revision'],'draft':draft})
        assert code==200 and time.monotonic()-started<1
        release.set();deadline=time.monotonic()+5
        while interactive_queue.snapshot()['pending'] or interactive_queue.snapshot()['active']:
            assert time.monotonic()<deadline;time.sleep(.02)
        states=[]
        for row,receipt in zip(rows,receipts):
            code,job=call(server,'GET',f"/api/batches/{row['id']}/hook-previews/{receipt['id']}")
            assert code==200;states.append(job['status'])
        assert states==['completed']*5+['superseded']
        assert len(entered)==5
        code,restored=call(server,'GET',f"/api/batches/{rows[0]['id']}/hook-previews")
        assert code==200 and restored['jobs'][0]['id']==receipts[0]['id']
    finally:release.set();dispatcher.stop(timeout=None);server.shutdown();server.server_close()
