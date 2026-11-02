"""Retain job leases until the complete owned process group actually stops.

A graceful cancellation must not acknowledge the death of this guard while its
media command is still flushing. Parent death still kills the owned group at once.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def live_members(group):
    if Path('/proc/self/stat').is_file():
        members=[]
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name)==os.getpid():continue
            try:
                pid=int(entry.name)
                if os.getpgid(pid)!=group:continue
                # Linux stat places state immediately after the parenthesized comm.
                state=(entry/'stat').read_text().rsplit(')',1)[1].strip().split()[0]
                if state!='Z':members.append(pid)
            except (FileNotFoundError,ProcessLookupError,PermissionError):continue
        return members
    # macOS has no procfs. The short-lived ps command has exited by inspection;
    # read only process IDs/group/state and never signal unrelated groups.
    result=subprocess.run(['ps','-axo','pid=,pgid=,stat='],capture_output=True,text=True,timeout=1)
    if result.returncode:raise RuntimeError('cannot inspect owned media process group')
    members=[]
    for line in result.stdout.splitlines():
        fields=line.split()
        if len(fields)!=3:continue
        pid,pgid,state=fields
        if int(pgid)!=group or int(pid)==os.getpid() or state.startswith('Z'):continue
        try:os.kill(int(pid),0)
        except ProcessLookupError:continue
        members.append(int(pid))
    return members


def main():
    parent=int(sys.argv[1]);command=json.loads(sys.argv[2]);leases=json.loads(sys.argv[3])
    group=os.getpgrp()
    if group!=os.getpid():raise RuntimeError('render child requires its own process group')
    if os.getppid()!=parent:return 137
    stopping=[None]
    def stopping_signal(*args):
        if stopping[0] is None:stopping[0]=time.monotonic()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stopping_signal)
    process=subprocess.Popen(command,pass_fds=leases)
    while True:
        if os.getppid()!=parent:os.killpg(group,signal.SIGKILL)
        if stopping[0] is not None and time.monotonic()-stopping[0]>=1.5:os.killpg(group,signal.SIGKILL)
        code=process.poll()
        if code is not None:
            try:members=live_members(group)
            except (OSError,RuntimeError,subprocess.TimeoutExpired):os.killpg(group,signal.SIGKILL)
            if not members:return code
            if stopping[0] is None:
                stopping[0]=time.monotonic()
                for pid in members:
                    try:os.kill(pid,signal.SIGTERM)
                    except ProcessLookupError:pass
        time.sleep(.05)


if __name__=='__main__':
    code=main()
    sys.exit(code if code>=0 else 128-code)
