"""Cancellation waits for the owned command and its media descendants, not the guard."""
import os
import signal
import sys
import threading
import time
import pytest
from exactcut import cutting,processing_cancel


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
