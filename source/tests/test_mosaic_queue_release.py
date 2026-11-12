"""Final editing queue participates in release drain without consuming subtitles."""
import threading
import pytest
from exactcut import mosaic_queue as queue, workflow
from exactcut.util import write_json_atomic
from test_linux_release import ops, initialize, release, Engine


@pytest.mark.parametrize('kind',['active','corrupt','pending'])
def test_release_observes_independent_mosaic_claims(ops,tmp_path,kind):
    initialize(ops,tmp_path);release(ops,tmp_path,'candidate')
    path=tmp_path/'data/.web-state/mosaic-queue.json'
    if kind=='corrupt':path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{broken')
    else:write_json_atomic(path,{'schema_version':1,'sequence':1,'tasks':[{'state':kind}]})
    before=path.read_bytes();engine=Engine()
    if kind=='pending':
        assert ops.active_records(tmp_path)==[]
        ops.transition(tmp_path,'candidate',engine=engine,timeout=0)
        assert path.read_bytes()==before
    else:
        assert ops.active_records(tmp_path)
        with pytest.raises(RuntimeError,match='任务'):ops.transition(tmp_path,'candidate',engine=engine,timeout=0)
        assert engine.calls==[]


def test_maintenance_keeps_tasks_pending(tmp_path,monkeypatch):
    monkeypatch.setattr(workflow,'BATCHES',tmp_path/'.web-state/batches')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    write_json_atomic(tmp_path/'.web-state/maintenance.json',{'reason':'test'})
    queue.enqueue('a'*32,'b'*32,'R1','admin',{'id':'c'*32,'status':'queued'})
    dispatcher=queue.Dispatcher(.01)
    try:
        dispatcher.start()
        assert not dispatcher.stop_event.wait(.1)
        assert queue.load()['tasks'][0]['state']=='pending'
        assert not (tmp_path/'.web-state/queue.json').exists()
    finally:dispatcher.stop(timeout=None)


def test_startup_failure_cannot_leave_dispatcher_writing_after_shutdown(tmp_path,monkeypatch):
    from unittest.mock import Mock
    from exactcut import webserver,preview_subtitle_queue,alerts
    created=[]
    class Stub:
        def __init__(self):self.started=False;self.stopped=False;created.append(self)
        def start(self):self.started=True;return True
        def stop(self,**kwargs):self.stopped=True
    monkeypatch.setattr(queue,'Dispatcher',Stub);monkeypatch.setattr(preview_subtitle_queue,'Dispatcher',Stub)
    monkeypatch.setattr(alerts,'Dispatcher',Stub)
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test');monkeypatch.setenv('EXACTCUT_WORKER_ENABLED','1')
    state=tmp_path/'state';state.mkdir();(state/'password').write_text('synthetic-startup-password-only')
    monkeypatch.setattr(webserver,'STATE',state);monkeypatch.setattr(webserver,'JOBS',state/'jobs')
    monkeypatch.setattr(workflow,'BATCHES',tmp_path/'batches')
    monkeypatch.setattr(webserver,'claim_data_root',lambda:None);monkeypatch.setattr(webserver,'load_env_file',lambda *a:None)
    monkeypatch.setattr(webserver.accounts,'initialize',lambda *a:None);monkeypatch.setattr(webserver,'ThreadingHTTPServer',Mock())
    def fail(**kwargs):raise RuntimeError('startup recovery failure')
    monkeypatch.setattr(webserver,'recover_jobs',fail)
    with pytest.raises(RuntimeError,match='startup recovery failure'):webserver.main()
    assert all(not item.started or item.stopped for item in created)
