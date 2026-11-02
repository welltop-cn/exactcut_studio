"""Identity locks for shared media resources, with live child ownership FDs."""
import fcntl
import time
from contextlib import contextmanager
from .runtime import data_root
from .util import json_sha256
from . import processing_cancel


@contextmanager
def hold(namespace,identity,*,timeout=180):
    """Serialize one actual resource across processes sharing this data root.

    Lock files are stable: removing them could split waiters across two inodes.
    Different identities never compete for a capacity slot or a hash shard.
    """
    path=data_root()/'.web-state/resource-locks'/namespace/(json_sha256(identity)+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a');deadline=time.monotonic()+timeout
    try:
        while True:
            processing_cancel.check()
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise TimeoutError('等待同一媒体资源超时，请重试')
                time.sleep(.05)
        with processing_cancel.retain([stream]):
            processing_cancel.check();yield
    finally:stream.close()
