"""Actual main must run independent interactive work when the ordinary worker is off."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import pytest
from exactcut import cutting,editor_resources,interactive_queue
from exactcut.util import read_json,write_json_atomic,file_sha256
from test_phase2_preview_api import fixture,call
from test_cutting_api import ready
from test_blank_batch_api import call as binary_call

MAIN = '''
import os
from pathlib import Path
from exactcut import webserver
from exactcut.util import write_json_atomic
from http.server import ThreadingHTTPServer
class ObservableServer(ThreadingHTTPServer):
    def serve_forever(self,*args,**kwargs):
        write_json_atomic(Path(os.environ['MAIN_TEST_READY']),{'port':self.server_port,'ordinary_worker':hasattr(self,'worker_thread')})
        return super().serve_forever(*args,**kwargs)
webserver.ThreadingHTTPServer=ObservableServer
webserver.main()
'''

@pytest.mark.parametrize('worker_enabled',[False,True])
@pytest.mark.parametrize('family',['cuts','mosaics'])
def test_actual_main_interactive_lifecycle_independent_of_ordinary_worker(ready,tmp_path,worker_enabled,family):
    bid,rid,sha,original=ready;state=tmp_path/'.web-state';password='synthetic-main-interactive-password-only'
    (state/'password').write_text(password);empty_env=tmp_path/'empty.env';empty_env.write_text('')
    marker=tmp_path/'server-ready.json';ordinary_output=tmp_path/'ordinary-executed';job_path=state/'jobs'/('f'*32+'.json')
    write_json_atomic(job_path,{'id':'f'*32,'lane':'fetch','action':'fetch','status':'queued','created':1,'owner':'admin',
                              'command':[sys.executable,'-c',f'from pathlib import Path;Path({str(ordinary_output)!r}).touch()']})
    env={**os.environ,'EXACTCUT_WORKER_ENABLED':str(int(worker_enabled)),'EXACTCUT_BIND':'127.0.0.1','EXACTCUT_PORT':'0',
         'EXACTCUT_ENV_FILE':str(empty_env),'EXACTCUT_ALLOW_AI':'0','EXACTCUT_ALLOW_PUBLISH':'0',
         'EXACTCUT_ALLOW_EXTERNAL_WRITES':'0','EXACTCUT_ALLOW_MATERIAL_FETCH':'0','MAIN_TEST_READY':str(marker)}
    log=(tmp_path/'main.log').open('w');process=subprocess.Popen([sys.executable,'-c',MAIN],cwd=Path(__file__).resolve().parents[1],env=env,stdout=log,stderr=subprocess.STDOUT)
    def wait(condition,seconds=5):
        deadline=time.monotonic()+seconds
        while not condition():
            assert process.poll() is None,(tmp_path/'main.log').read_text()
            assert time.monotonic()<deadline,'actual main did not advance accepted interactive work'
            time.sleep(.02)
    try:
        wait(marker.exists);info=read_json(marker);assert info['ordinary_worker']==worker_enabled
        app=SimpleNamespace(server_address=('127.0.0.1',info['port']),password=password)
        root=f'/api/batches/{bid}/{family}'
        with ExitStack() as held:
            for _ in range(2):held.enter_context(editor_resources.acquire('preprocess'))
            code,job=call(app,'POST',root+'/open',{'request_id':rid,'source_sha256':sha});assert code==202 and job['status']=='queued'
            maintenance=state/'maintenance.json';write_json_atomic(maintenance,{'reason':'actual main regression'})
        time.sleep(.15);assert call(app,'GET',root+'/preparation/'+job['id'])[1]['status']=='queued'
        assert not interactive_queue.snapshot()['active']
        maintenance.unlink()
        report={}
        def prepared():
            nonlocal report
            code,report=call(app,'GET',root+'/preparation/'+job['id']);assert code==200
            return report['status'] not in {'queued','running'}
        wait(prepared,3);assert report['status']=='completed',report
        session=report['result']['session'];assert session['source_sha256']==sha
        assert call(app,'POST',root+'/open',{'request_id':rid,'source_sha256':sha})[0]==200
        frame=root+'/'+session['id']+'/frame/5'
        with ExitStack() as held:
            for _ in range(2):held.enter_context(editor_resources.acquire('frame'))
            code,receipt=call(app,'GET',frame);assert code==202 and receipt['status']=='queued'
        result=None
        def rendered():
            nonlocal result
            result=binary_call(app,'GET',frame);return result[0]!=202
        wait(rendered);assert result[0]==200 and result[2][:2]==b'\xff\xd8'
        assert file_sha256(original)==sha
        if worker_enabled:wait(ordinary_output.exists)
        else:
            assert not ordinary_output.exists() and read_json(job_path)['status']=='queued'
            assert not (state/'worker.lock').exists()
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait();pytest.fail('actual main did not shut down')
        log.close()
    assert process.returncode==0
    assert not interactive_queue.snapshot()['pending'] and not interactive_queue.snapshot()['active']
    lease=cutting.lock_file(interactive_queue.directory()/'dispatcher.lock',blocking=False)
    lease.close()
