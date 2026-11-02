"""An accepted receipt may precede compute; retries deduplicate and cleanup follows work."""
import threading,time
import pytest
from exactcut import hook_preview,stage_results,webserver as web,interactive_queue
from exactcut.util import write_json_atomic
from test_phase2_preview_api import fixture,call,serve


@pytest.mark.parametrize('first',['deferred','success','error'])
def test_accepted_preview_reply_can_be_held_while_work_and_retry_complete_independently(fixture,tmp_path,monkeypatch,first):
    monkeypatch.setattr(web,'JOBS',tmp_path/'.web-state/jobs');web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web,'INLINE_RENDERS',set())
    clock=[1000.];monkeypatch.setattr(web.time,'time',lambda:clock[0])
    replied,return_from_reply=threading.Event(),threading.Event()
    computing,finish_compute=threading.Event(),threading.Event();attempts=[]
    def compute(project,request,work):
        attempts.append(project);cache=work/'cache'
        for name in ('hook-emphasis','hook-placement'):(cache/name).mkdir(parents=True)
        if len(attempts)==1:
            computing.set();assert finish_compute.wait(5)
            if first=='deferred':
                write_json_atomic(cache/'hook-emphasis'/('b'*64+'.json'),{'complete':True})
                raise stage_results.ResourceDeferred(1010.)
            if first=='error':raise OSError('fixture compute failure')
        return {'preview_key':'a'*64},{'decision':{}},cache
    monkeypatch.setattr(hook_preview,'compute',compute)
    respond=web.Handler.respond;paused=[]
    def pause_first_reply(self,code,data):
        respond(self,code,data)
        if not paused:
            paused.append(code);replied.set();assert return_from_reply.wait(5)
    monkeypatch.setattr(web.Handler,'respond',pause_first_reply)
    app=serve();endpoint=f"/api/batches/{fixture['id']}/hook-preview";body={'revision':fixture['revision'],'index':0};responses=[]
    client=threading.Thread(target=lambda:responses.append(call(app,'POST',endpoint,body)))
    def wait_job(jid,expected):
        deadline=time.monotonic()+3
        while True:
            job=hook_preview.state(fixture['id'],jid);snapshot=interactive_queue.snapshot()
            active=any(row['job']['id']==jid for row in snapshot['active'])
            if job['status']==expected and not active:return job
            assert time.monotonic()<deadline;time.sleep(.01)
    try:
        client.start();assert replied.wait(3) and computing.wait(2)
        code,duplicate=call(app,'POST',endpoint,body)
        assert code==202 and len(attempts)==1
        assert len(web.INLINE_RENDERS)==1,'the real worker, not the held HTTP reply, owns its disk reservation'
        finish_compute.set()
        initial=wait_job(duplicate['id'],{'deferred':'queued','success':'completed','error':'failed'}[first])
        assert not web.INLINE_RENDERS
        with web.storage.reservation_lock(web.JOBS) as ledger:assert not ledger['jobs']
        if first=='deferred':
            pending=[row for row in interactive_queue.snapshot()['pending'] if row['job']['id']==duplicate['id']]
            assert len(pending)==1 and pending[0]['not_before']==1010.
        clock[0]=1010.
        code,retry=call(app,'POST',endpoint,body)
        assert code in {200,202} and retry['id']==duplicate['id']
        assert wait_job(retry['id'],'completed')['result']['preview_key']=='a'*64
        assert len(attempts)==(1 if first=='success' else 2)
    finally:
        finish_compute.set();return_from_reply.set();client.join(3);app.shutdown();app.server_close()
    assert responses[0][0]==202 and not web.INLINE_RENDERS


from interactive_test_support import interactive_dispatcher
