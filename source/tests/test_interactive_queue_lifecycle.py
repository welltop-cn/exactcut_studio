"""Dispatcher ownership and maintenance reproduce supervisor review findings."""
import threading,time
from exactcut import interactive_queue as queue,editor_resources
from exactcut.util import read_json,write_json_atomic
from test_interactive_queue import state,submit,wait


def test_finite_stop_keeps_dispatcher_lease_until_actual_worker_exit(state):
    submit(state,1);entered=threading.Event();release=threading.Event()
    def run(entry,leases):
        entered.set();assert release.wait(5)
        path=state/entry['report'];job=read_json(path);job['status']='completed';write_json_atomic(path,job)
    first=queue.Dispatcher(run=run,poll_seconds=.01);second=queue.Dispatcher(run=run,poll_seconds=.01)
    try:
        assert first.start() and entered.wait(1)
        started=time.monotonic();first.stop(timeout=.02);assert time.monotonic()-started<.5
        assert first.active_workers==1
        assert second.start() is False,'finite stop must not hand over a still-live worker/cloud request'
        release.set();wait(lambda:first._lease is None)
        assert second.start()
    finally:release.set();first.stop(timeout=None);second.stop(timeout=None)


def test_maintenance_holds_pending_then_removal_allows_dispatch(state):
    from exactcut.runtime import maintenance_file
    submit(state,1);marker=maintenance_file();write_json_atomic(marker,{'reason':'test maintenance'})
    calls=[]
    def run(entry,leases):
        calls.append(entry['task_id']);path=state/entry['report'];job=read_json(path);job['status']='completed';write_json_atomic(path,job)
    dispatcher=queue.Dispatcher(run=run,poll_seconds=.01)
    try:
        assert dispatcher.start();time.sleep(.1)
        assert not calls and len(queue.snapshot()['pending'])==1 and not queue.snapshot()['active']
        marker.unlink();wait(lambda:len(calls)==1 and not queue.snapshot()['active'])
    finally:dispatcher.stop(timeout=None)


def test_stop_racing_a_claim_keeps_unstarted_work_pending(state,monkeypatch):
    submit(state,1);entered=threading.Event();release=threading.Event();calls=[]
    original=editor_resources.acquire
    def blocked(kind,*args,**kwargs):
        if kind=='preview':entered.set();assert release.wait(5)
        return original(kind,*args,**kwargs)
    monkeypatch.setattr(editor_resources,'acquire',blocked)
    dispatcher=queue.Dispatcher(run=lambda *args:calls.append(args),poll_seconds=.01)
    try:
        assert dispatcher.start() and entered.wait(1)
        dispatcher.stop(timeout=.02);release.set();dispatcher.stop(timeout=None)
        assert not calls and len(queue.snapshot()['pending'])==1 and not queue.snapshot()['active']
    finally:release.set();dispatcher.stop(timeout=None)


def test_maintenance_claim_uses_the_global_short_mutation_barrier(state):
    from exactcut.runtime import mutation_lock,maintenance_file
    submit(state,1);calls=[];dispatcher=queue.Dispatcher(run=lambda *args:calls.append(args),poll_seconds=.01)
    try:
        with mutation_lock():
            assert dispatcher.start();time.sleep(.1)
            assert not calls,'dispatch must not pass a held deployment/mutation barrier'
            write_json_atomic(maintenance_file(),{'reason':'test'})
        time.sleep(.05)
        assert not calls and len(queue.snapshot()['pending'])==1
    finally:dispatcher.stop(timeout=None)


def test_pending_cancellation_waits_for_its_report_state_lock(state):
    from exactcut import cutting
    entry=submit(state,1);path=state/entry['report'];job=read_json(path)
    job['cancel_requested']=True;write_json_atomic(path,job)
    calls=[];dispatcher=queue.Dispatcher(run=lambda *args:calls.append(args),poll_seconds=.01)
    try:
        with cutting.lock_file(state/entry['state_lock'],blocking=True):
            assert dispatcher.start();time.sleep(.1)
            assert read_json(path)['status']=='queued','claim must not overwrite a report while its transaction owns state_lock'
            assert len(queue.snapshot()['pending'])==1 and not calls
        wait(lambda:not queue.snapshot()['pending'])
        assert read_json(path)['status']=='cancelled' and not calls
    finally:dispatcher.stop(timeout=None)
