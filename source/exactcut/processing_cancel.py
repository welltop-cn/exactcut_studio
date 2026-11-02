"""Thread-scoped cooperative cancellation for owned rendering subprocess groups."""
import contextlib
import os
import signal
import subprocess
import threading
import time
import json
import sys

_local=threading.local()


class Cancelled(Exception):pass


def recover_interrupted(job,error):
    """A lost worker never revives a persisted cancellation intent."""
    if job.get('cancel_requested'):
        job.update(status='cancelled',progress='已停止处理，当前选择已保留');job.pop('error',None);job.pop('result',None)
    else:job.update(status='failed',error=error)
    return job


def check():
    callback=getattr(_local,'callback',None)
    if callback and callback():raise Cancelled()


@contextlib.contextmanager
def scope(callback,leases=None):
    previous=getattr(_local,'callback',None);previous_leases=getattr(_local,'leases',())
    _local.callback=callback
    _local.leases=tuple(leases) if leases is not None else previous_leases
    try:yield
    finally:_local.callback=previous;_local.leases=previous_leases


@contextlib.contextmanager
def retain(leases):
    """Add stage ownership without dropping job leases or retaining closed FDs."""
    previous=getattr(_local,'leases',())
    _local.leases=previous+tuple(leases)
    try:yield
    finally:_local.leases=previous


def process_options():
    """Resolve live lease objects at spawn; descriptor numbers may be reused."""
    fds=[]
    for lease in getattr(_local,'leases',()):
        if hasattr(lease,'filenos'):fds.extend(lease.filenos())
        else:
            try:fds.append(lease.fileno())
            except ValueError:pass  # A completed stage closed this particular object.
    return {'pass_fds':tuple(dict.fromkeys(fds))} if fds else {}


def spawn(command,**kwargs):
    options=process_options()
    if options:
        command=[sys.executable,'-m','exactcut.processing_child',str(os.getpid()),json.dumps([os.fsdecode(arg) for arg in command]),json.dumps(options['pass_fds'])]
    return subprocess.Popen(command,start_new_session=True,**{**options,**kwargs})


def terminate(process,sig=signal.SIGKILL):
    try:os.killpg(process.pid,sig)
    except ProcessLookupError:pass


def run(command,timeout=None,**kwargs):
    if not getattr(_local,'callback',None) and not process_options():return subprocess.run(command,timeout=timeout,**kwargs)
    check()
    if kwargs.pop('capture_output',False):kwargs.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    process=spawn(command,**kwargs)
    started=time.monotonic()
    try:
        while True:
            check()
            if timeout is not None and time.monotonic()-started>=timeout:raise subprocess.TimeoutExpired(command,timeout)
            try:
                stdout,stderr=process.communicate(timeout=.2)
                check()
                return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
            except subprocess.TimeoutExpired:continue
    except BaseException:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.communicate()
        raise
