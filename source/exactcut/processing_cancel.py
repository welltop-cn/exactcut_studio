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


def scoped():
    """True only inside an owned worker cancellation scope, never a bare request."""
    return getattr(_local,'callback',None) is not None


@contextlib.contextmanager
def scope(callback,leases=None):
    previous=getattr(_local,'callback',None);previous_leases=getattr(_local,'leases',())
    _local.callback=callback
    _local.leases=tuple(leases) if leases is not None else previous_leases
    try:yield
    finally:_local.callback=previous;_local.leases=previous_leases


@contextlib.contextmanager
def pause_scope(path):
    from .batch_control import suspended_seconds
    previous=getattr(_local,'clock',None)
    _local.clock=lambda:time.monotonic()-suspended_seconds(path)
    try:yield
    finally:_local.clock=previous


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
    owned_group=bool(getattr(_local,'callback',None) or process_options())
    clock=getattr(_local,'clock',None)
    if not owned_group and clock is None:return subprocess.run(command,timeout=timeout,**kwargs)
    clock=clock or time.monotonic
    check()
    if kwargs.pop('capture_output',False):kwargs.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    # Ordinary production stays in its executor group so batch SIGSTOP reaches
    # both the caller and media. Cancellable workers keep their owned group.
    process=spawn(command,**kwargs) if owned_group else subprocess.Popen(command,**kwargs)
    started=clock()
    try:
        while True:
            check()
            remaining=None if timeout is None else timeout-(clock()-started)
            if remaining is not None and remaining<=0:raise subprocess.TimeoutExpired(command,timeout)
            try:
                stdout,stderr=process.communicate(timeout=.2 if remaining is None else min(.2,remaining))
                check()
                return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
            except subprocess.TimeoutExpired:continue
    except BaseException:
        def stop(sig):
            if owned_group:
                try:os.killpg(process.pid,sig)
                except ProcessLookupError:pass
            else:
                process.send_signal(sig)
        stop(signal.SIGTERM)
        try:process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            stop(signal.SIGKILL)
            process.communicate()
        raise
