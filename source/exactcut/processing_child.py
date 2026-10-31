"""Own a rendering process group and stop it if its server disappears.

The server starts this helper in a new session and passes worker/compute leases.
The helper retains those leases until its command exits. Its child inherits the
same process group and standard streams. Checking getppid avoids signalling a
reused server PID; group identity is checked before sending any signal.
"""
import json
import os
import signal
import subprocess
import sys


def main():
    parent=int(sys.argv[1]);command=json.loads(sys.argv[2]);leases=json.loads(sys.argv[3])
    if os.getpgrp()!=os.getpid():raise RuntimeError('render child requires its own process group')
    if os.getppid()!=parent:return 137
    process=subprocess.Popen(command,pass_fds=leases)
    while True:
        if os.getppid()!=parent:
            # The server cannot supervise graceful termination anymore. Kill the
            # complete owned group before inherited resource leases are released.
            os.killpg(os.getpgrp(),signal.SIGKILL)
        try:return process.wait(timeout=.1)
        except subprocess.TimeoutExpired:pass


if __name__=='__main__':
    code=main()
    sys.exit(code if code>=0 else 128-code)
