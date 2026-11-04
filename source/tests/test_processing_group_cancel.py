"""Cancellation waits for the owned command and its media descendants, not the guard."""
import os
import signal
import subprocess
import uuid
from pathlib import Path
import sys
import threading
import time
import pytest
from exactcut import cutting,processing_cancel
from exactcut import batch_control
from exactcut.util import write_json_atomic


@pytest.mark.parametrize('scope_kind', ['ordinary', 'cancellable', 'leased'])
@pytest.mark.parametrize('action', ['pause', 'archive'])
def test_media_timeout_excludes_batch_pause(tmp_path, scope_kind, action):
    batch=tmp_path/('a'*32);batch.mkdir()
    write_json_atomic(batch/'runtime.json', {'status':'producing','events':[]})
    ready=tmp_path/'media-ready'
    command=f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(.25); print('finished')"
    script=f'''
from contextlib import nullcontext
from pathlib import Path
import sys
from exactcut import workflow, processing_cancel, cutting
from exactcut.media import run_checked
with workflow.execution(Path({str(batch)!r})):
    with cutting.lock_file(Path({str(tmp_path/'lease.lock')!r})) as lease:
        scope = nullcontext() if {scope_kind!r} == 'ordinary' else processing_cancel.scope(lambda:False, leases=[lease] if {scope_kind!r} == 'leased' else [])
        with scope:
            print(run_checked([sys.executable,'-c',{command!r}],timeout=.7,light=True).stdout,flush=True)
'''
    token=uuid.uuid4().hex
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    process=subprocess.Popen([sys.executable,'-X','exactcut_process_token='+token,'-c',script],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True,env=env)
    try:
        identity=batch_control.identify(process,process_token=token,lane='render')
        assert identity
        batch_control.remember_child(batch,identity)
        wait_for(ready)
        batch_control.change(batch,action)
        time.sleep(1.1)
        if action=='archive':batch_control.change(batch,'unarchive')
        batch_control.change(batch,'resume')
        batch_control.reconcile(tmp_path)
        stdout,stderr=process.communicate(timeout=6)
        assert process.returncode==0,stderr
        assert 'finished' in stdout
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGCONT)
            os.killpg(process.pid,signal.SIGKILL)
            process.communicate()


@pytest.mark.parametrize('scope_kind', ['ordinary', 'cancellable', 'leased'])
def test_media_still_times_out_and_reaps_process_in_batch_scope(tmp_path, scope_kind):
    from contextlib import nullcontext
    from exactcut import workflow
    from exactcut.media import run_checked
    batch=tmp_path/('a'*32);batch.mkdir()
    write_json_atomic(batch/'runtime.json', {'status':'producing','events':[]})
    ready=tmp_path/'media-pid'
    command=f"from pathlib import Path; import os,time; Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    with workflow.execution(batch):
        with cutting.lock_file(tmp_path/'worker.lock') as lease:
            scope=nullcontext() if scope_kind=='ordinary' else processing_cancel.scope(lambda:False,leases=[lease] if scope_kind=='leased' else [])
            with scope, pytest.raises(RuntimeError,match='媒体处理超时'):
                run_checked([sys.executable,'-c',command],timeout=.5,light=True)
    assert ready.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()),0)
    with cutting.lock_file(tmp_path/'worker.lock'):pass


def wait_for(path):
    deadline=time.monotonic()+4
    while not path.exists() and time.monotonic()<deadline:time.sleep(.01)
    assert path.exists()


@pytest.mark.parametrize('grandchild',[False,True])
def test_guard_cancellation_waits_for_actual_owned_processes(tmp_path,grandchild):
    ready=tmp_path/'ready';stopping=tmp_path/'stopping';exited=tmp_path/'exited'
    command=f'''
import os,signal,time,sys
from pathlib import Path
def stop(*args):
 Path({str(stopping)!r}).write_text(str(os.getpid()))
 time.sleep(.6)
 Path({str(exited)!r}).write_text('done')
 sys.exit(0)
signal.signal(signal.SIGTERM,stop)
Path({str(ready)!r}).write_text(str(os.getpid()))
while True:time.sleep(.01)
'''
    if grandchild:
        command=f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{command!r}])\nwhile True:time.sleep(.01)"
    cancel=threading.Event();done=threading.Event();observed=[]
    def worker():
        lease=cutting.lock_file(tmp_path/'worker.lock')
        try:
            with processing_cancel.scope(cancel.is_set,leases=[lease]):
                import subprocess
                processing_cancel.run([sys.executable,'-c',command],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
        except processing_cancel.Cancelled:observed.append('cancelled')
        finally:lease.close();done.set()
    thread=threading.Thread(target=worker);thread.start()
    try:
        wait_for(ready);cancel.set();wait_for(stopping)
        assert not done.wait(.2),'cancel acknowledged while actual media process was still alive'
        assert done.wait(4)
        assert exited.exists() and observed==['cancelled']
        with cutting.lock_file(tmp_path/'worker.lock'):pass
    finally:
        cancel.set();thread.join(5)
        if ready.exists():
            try:os.kill(int(ready.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass
