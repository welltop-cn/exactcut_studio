"""Thread-scoped cooperative cancellation for owned rendering subprocess groups."""
import contextlib
import os
import signal
import subprocess
import threading
import time

_local=threading.local()


class Cancelled(Exception):pass


def check():
    callback=getattr(_local,'callback',None)
    if callback and callback():raise Cancelled()


@contextlib.contextmanager
def scope(callback):
    previous=getattr(_local,'callback',None);_local.callback=callback
    try:yield
    finally:_local.callback=previous


def run(command,timeout=None,**kwargs):
    if not getattr(_local,'callback',None):return subprocess.run(command,timeout=timeout,**kwargs)
    check()
    if kwargs.pop('capture_output',False):kwargs.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    process=subprocess.Popen(command,start_new_session=True,**kwargs)
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
