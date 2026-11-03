import os
import subprocess
import sys
import pytest
from exactcut import cutting, processing_cancel


def test_cancel_scope_child_holds_worker_lease_until_actual_exit(tmp_path):
    lease=cutting.lock_file(tmp_path/'worker.lock')
    marker=tmp_path/'child-pid'
    script="import os,time;from pathlib import Path;Path(%r).write_text(str(os.getpid()));time.sleep(.5)" % str(marker)
    with processing_cancel.scope(lambda:False,leases=[lease]):
        options=processing_cancel.process_options()
        child=subprocess.Popen([sys.executable,'-c',script],**options)
    lease.close()
    try:
        with pytest.raises(cutting.CutError):cutting.lock_file(tmp_path/'worker.lock')
        child.wait(timeout=3)
        with cutting.lock_file(tmp_path/'worker.lock'):pass
    finally:
        if child.poll() is None:child.kill();child.wait()


@pytest.mark.integration
def test_abrupt_owner_exit_cannot_recover_over_live_ffmpeg(tmp_path,monkeypatch):
    import json,time
    from exactcut import workflow,mosaic_queue as queue
    monkeypatch.setattr(workflow,'BATCHES',tmp_path/'batches')
    item=queue.enqueue('a'*32,'b'*32,'R1','admin',{'id':'c'*32,'status':'queued'})
    queue.claim();worker=queue.job_path(item).with_suffix('.worker.lock')
    compute=tmp_path/'compute.lock';output=tmp_path/'orphan.mp4'
    script='''import os,subprocess
from pathlib import Path
from exactcut import cutting,processing_cancel
worker=cutting.lock_file(Path(%r));compute=cutting.lock_file(Path(%r))
with processing_cancel.scope(lambda:False,leases=[worker,compute]):
    subprocess.Popen(['ffmpeg','-y','-v','error','-re','-f','lavfi','-i','color=red:s=32x32:r=30','-t','2','-c:v','libx264',%r],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**processing_cancel.process_options())
os._exit(0)
''' % (str(worker),str(compute),str(output))
    subprocess.run([sys.executable,'-c',script],check=True)
    queue.recover()
    assert queue.load()['tasks'][0]['state']=='active'
    assert queue.claim() is None
    with pytest.raises(cutting.CutError):cutting.lock_file(compute)
    deadline=time.monotonic()+6
    while True:
        try:lease=cutting.lock_file(compute);lease.close();break
        except cutting.CutError:
            assert time.monotonic()<deadline
            time.sleep(.05)
    assert cutting.probe_source(output)['frame_count']==60
    queue.recover()
    assert queue.load()['tasks'][0]['state']=='pending'
    assert queue.claim()['id']==item['id']


@pytest.mark.integration
def test_actual_worker_child_stops_when_server_owner_dies(tmp_path):
    import time
    worker=tmp_path/'owned.lock';pidfile=tmp_path/'pid';output=tmp_path/'partial.mp4'
    script='''import os,time,subprocess
from pathlib import Path
from exactcut import cutting,processing_cancel
lease=cutting.lock_file(Path(%r))
with processing_cancel.scope(lambda:False,leases=[lease]):
    child=processing_cancel.spawn(['ffmpeg','-y','-v','error','-re','-f','lavfi','-i','color=red:s=32x32:r=30','-t','60','-c:v','libx264',%r],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path(%r).write_text(str(child.pid))
time.sleep(.3)
os._exit(0)
''' % (str(worker),str(output),str(pidfile))
    subprocess.run([sys.executable,'-c',script],check=True,timeout=5)
    pid=int(pidfile.read_text());deadline=time.monotonic()+4
    try:
        while True:
            try:lease=cutting.lock_file(worker);lease.close();break
            except cutting.CutError:
                assert time.monotonic()<deadline,'orphan retained the lease after owner death'
                time.sleep(.05)
        rows=subprocess.run(['ps','-A','-o','pgid=','-o','stat=','-o','comm='],capture_output=True,text=True,check=True).stdout.splitlines()
        assert not any(parts[0]==str(pid) and not parts[1].startswith('Z') for row in rows if len(parts:=row.split())>=3)
    finally:
        try:os.killpg(pid,9)
        except ProcessLookupError:pass
