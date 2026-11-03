"""Owned local processes and a filesystem OSS double; no production side effects."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest
from exactcut import resource_locks,workflow
from test_source_frames import source
from test_web_base_path import app


def wait_file(path,seconds=5):
    deadline=time.monotonic()+seconds
    while not path.exists() and time.monotonic()<deadline:time.sleep(.02)
    assert path.exists()


def alive(pid):
    row=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
    return bool(row.stdout.strip() and not row.stdout.strip().startswith('Z'))


def test_media_probe_child_retains_identity_until_dead_after_parent_kill(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    mark=tmp_path/'child.pid';script=tmp_path/'owner.py'
    payload="import os,time;from pathlib import Path;Path("+repr(str(mark))+").write_text(str(os.getpid()));time.sleep(30)"
    script.write_text("from exactcut import resource_locks,media\nimport sys\nwith resource_locks.hold('probe','same'):\n media.run_checked([sys.executable,'-c',"+repr(payload)+"],timeout=31)\n")
    worker=subprocess.Popen([sys.executable,str(script)],env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])},start_new_session=True);pid=None
    try:
        wait_file(mark);pid=int(mark.read_text());assert alive(pid)
        worker.kill();worker.wait(timeout=3)
        with resource_locks.hold('probe','same',timeout=4):
            assert not alive(pid),'a live ffprobe-like child must not outlive its shared resource ownership'
    finally:
        if worker.poll() is None:worker.kill();worker.wait(timeout=3)
        if pid and alive(pid):
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass


def test_source_index_same_video_is_probed_once_across_processes(source,tmp_path):
    _,item,_,_=source;other=workflow.create_blank();log=tmp_path/'probe.log';script=tmp_path/'indexer.py'
    script.write_text("from exactcut import source_frames,workflow\nfrom test_source_prefetch import draft_with\nimport sys,os,time\noriginal=source_frames.media.run_checked\ndef probe(*a,**k):\n fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.write(fd,b'probe\\n');os.close(fd);time.sleep(.3);return original(*a,**k)\nsource_frames.media.run_checked=probe\nsource_frames.prepare(workflow.folder(sys.argv[1]),draft_with('EP1'),'EP1')\n")
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(Path(__file__).parents[1]),str(Path(__file__).parent)])}
    processes=[subprocess.Popen([sys.executable,str(script),row['id'],str(log)],env=env) for row in (item,other)]
    try:
        assert [process.wait(timeout=10) for process in processes]==[0,0]
        assert log.read_text().splitlines()==['probe']
        indexes=[next(workflow.folder(row['id']).glob('source-picker/*/index.json')) for row in (item,other)]
        assert indexes[0].read_bytes()==indexes[1].read_bytes()
    finally:
        for process in processes:
            if process.poll() is None:process.kill();process.wait(timeout=3)


def test_oss_same_object_has_one_put_across_processes(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    video=tmp_path/'preview.mp4';video.write_bytes(b'isolated fixture video');script=tmp_path/'uploader.py';log=tmp_path/'put.log'
    script.write_text("from exactcut import preview_oss\nfrom pathlib import Path\nfrom types import SimpleNamespace\nimport os,sys,time,json,hashlib\nroot=Path(sys.argv[1]);video=root/'preview.mp4'\nclass Already(Exception):code='FileAlreadyExists'\nclass Bucket:\n bucket_name='fixture';endpoint='https://fixture.invalid'\n def target(self,key):return root/(hashlib.sha256(key.encode()).hexdigest()+'.object')\n def object_exists(self,key):return self.target(key).exists()\n def put_object_from_file(self,key,filename,headers):\n  fd=os.open(str(root/'put.log'),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.write(fd,b'put\\n');os.close(fd);time.sleep(.3)\n  try:\n   with self.target(key).open('x') as stream:json.dump({'bytes':Path(filename).stat().st_size,'headers':headers},stream)\n  except FileExistsError:raise Already()\n  return SimpleNamespace(status=200)\n def head_object(self,key):\n  doc=json.loads(self.target(key).read_text());return SimpleNamespace(content_length=doc['bytes'],headers=doc['headers'])\n def get_object_acl(self,key):return SimpleNamespace(acl='private')\npreview_oss._ensure_uploaded(video,'a'*32,'R1',bucket=Bucket())\n")
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])}
    processes=[subprocess.Popen([sys.executable,str(script),str(tmp_path)],env=env) for _ in range(3)]
    try:
        assert [process.wait(timeout=10) for process in processes]==[0,0,0]
        assert log.read_text().splitlines()==['put']
    finally:
        for process in processes:
            if process.poll() is None:process.kill();process.wait(timeout=3)
