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


def check():
    callback=getattr(_local,'callback',None)
    if callback and callback():raise Cancelled()


@contextlib.contextmanager
def scope(callback,leases=None):
    previous=getattr(_local,'callback',None);previous_fds=getattr(_local,'lease_fds',())
    _local.callback=callback
    _local.lease_fds=tuple(lease.fileno() for lease in leases) if leases is not None else previous_fds
    try:yield
    finally:_local.callback=previous;_local.lease_fds=previous_fds


def process_options():
    """Children retain compute/worker leases across an abrupt server death."""
    fds=getattr(_local,'lease_fds',())
    return {'pass_fds':fds} if fds else {}


def spawn(command,**kwargs):
    options=process_options()
    if options:
        command=[sys.executable,'-m','exactcut.processing_child',str(os.getpid()),json.dumps([os.fsdecode(arg) for arg in command]),json.dumps(options['pass_fds'])]
    return subprocess.Popen(command,start_new_session=True,**{**options,**kwargs})


def terminate(process,sig=signal.SIGKILL):
    try:os.killpg(process.pid,sig)
    except ProcessLookupError:pass


def run(command,timeout=None,**kwargs):
    if not getattr(_local,'callback',None):return subprocess.run(command,timeout=timeout,**kwargs)
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
