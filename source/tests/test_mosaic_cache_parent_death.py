"""The inference lease survives its Python owner until its media child dies."""
import os,sys,json,time,signal,subprocess
from pathlib import Path
import pytest
from exactcut import mosaic_inference as inference,mosaic_tracking as tracking,mosaic_session as sessions
from exactcut.util import write_json_atomic
from test_cutting_api import ready
from test_phase2_preview_api import fixture
from test_mosaic_inference_cache_concurrency import setup
from test_mosaic_render import make_track


@pytest.mark.parametrize('complete',[False,True])
def test_parent_death_retains_key_until_child_exit_and_recovers_verified_work(ready,tmp_path,monkeypatch,complete):
    bid,session,target,cfg=setup(ready,tmp_path,monkeypatch);root=sessions.session_path(bid,session['id'])
    params=root/'crash-test.json';write_json_atomic(params,{'session':session,'target':target,'cfg':cfg,'complete':complete})
    code=r'''
import sys,json
from pathlib import Path
from exactcut import cutting,mosaic_inference as inference,processing_cancel,mosaic_tracking as tracking
from exactcut.util import read_json
from test_mosaic_render import make_track
root=Path(sys.argv[1]);p=read_json(root/'crash-test.json')
def infer(source,directory,meta,target,cfg):
 if p['complete']:make_track(directory,target,meta)
 command=[sys.executable,'-c','import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(60)',str(root/'media-pid')]
 processing_cancel.run(command,timeout=65)
 raise AssertionError('owner should have died')
tracking.run_tracker=infer
with cutting.lock_file(root/'test-owner.worker.lock') as lease,processing_cancel.scope(lambda:False,leases=[lease]):
 inference.prepare(root,p['session'],p['target'],p['cfg'],root/'old-target')
'''
    source=Path(__file__).resolve().parents[1];env=os.environ.copy();env['PYTHONPATH']=os.pathsep.join([str(source),str(source/'tests')])
    process=subprocess.Popen([sys.executable,'-c',code,str(root)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    def live(pid):
        result=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
        return result.returncode==0 and bool(result.stdout.strip()) and not result.stdout.lstrip().startswith('Z')
    try:
        deadline=time.monotonic()+5
        while not (root/'media-pid').exists():
            assert process.poll() is None,process.stderr.read().decode()
            assert time.monotonic()<deadline
            time.sleep(.01)
        child=int((root/'media-pid').read_text());assert live(child)
        process.kill();process.wait(3)
        calls=[]
        def fresh(source,directory,meta,target,cfg):
            assert not live(child),'key was released while its inherited media child was alive'
            calls.append(directory);return make_track(directory,target,meta)['result']
        monkeypatch.setattr(tracking,'run_tracker',fresh)
        track=inference.prepare(root,session,target,cfg,root/'new-target')
        assert not live(child)
        assert len(calls)==(0 if complete else 1)
        assert (track['directory']/'result.json').is_file()
        assert not list(root.glob('inference-*-work-*'))
    finally:
        if process.poll() is None:process.kill();process.wait(3)
        process.stderr.close()
