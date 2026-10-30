"""Preview recognition must participate in the existing release maintenance barrier."""
from contextlib import contextmanager
import threading
import time

import pytest

from exactcut import preview_subtitle_queue as queue, preview_subtitles_http as http
from exactcut.util import write_json_atomic
from test_linux_release import ops, initialize, release, Engine
from test_preview_subtitles import configured, dispatcher, ready, fixture, call
from test_preview_subtitle_queue import descriptor
from test_phase2_preview_api import serve


@pytest.mark.parametrize('record',['active','extra','running','corrupt'])
def test_release_cannot_stop_service_during_preview_recognition(ops,tmp_path,record):
    initialize(ops,tmp_path);release(ops,tmp_path,'candidate')
    state=tmp_path/'data/.web-state'
    if record=='running':
        write_json_atomic(state/'batches'/('a'*32)/'preview-subtitles'/('b'*64)/'report.json',{'status':'running'})
    elif record=='corrupt':
        (state/'queue.json').parent.mkdir(parents=True,exist_ok=True)
        (state/'queue.json').write_text('{broken')
    else:
        data={'schema_version':1,'sequence':2,'active':None,'pending':[]}
        data['active' if record=='active' else 'active_extra']=descriptor(1) if record=='active' else [descriptor(2)]
        write_json_atomic(state/'queue.json',data)
    engine=Engine()
    assert ops.active_records(tmp_path)
    with pytest.raises(RuntimeError,match='任务'):
        ops.transition(tmp_path,'candidate',engine=engine,timeout=0)
    assert engine.calls==[]


def test_pending_preview_tasks_survive_a_code_switch(ops,tmp_path):
    initialize(ops,tmp_path);release(ops,tmp_path,'candidate')
    data={'schema_version':1,'sequence':1,'active':None,'pending':[descriptor(1)]}
    path=tmp_path/'data/.web-state/queue.json';write_json_atomic(path,data)
    # Real admission always also writes a queued report; it must not wedge drain.
    report=tmp_path/'data/.web-state/batches'/('a'*32)/'preview-subtitles'/descriptor(1)['task_id']/'report.json'
    write_json_atomic(report,{'status':'queued','id':descriptor(1)['task_id']})
    before=path.read_bytes()
    assert ops.active_records(tmp_path)==[]
    ops.transition(tmp_path,'candidate',engine=Engine(),timeout=0)
    assert path.read_bytes()==before


def test_maintenance_pauses_pending_dispatch_without_losing_tasks(configured,dispatcher):
    maintenance=queue.state_dir()/'maintenance.json'
    write_json_atomic(maintenance,{'reason':'release'})
    queue.enqueue(descriptor(1),20)
    ran=threading.Event()
    dispatcher(lambda *args:ran.set())
    assert not ran.wait(.15)
    assert queue.membership(descriptor(1)['task_id'])['position']==1
    maintenance.unlink();queue.wake()
    assert ran.wait(2)


def test_admission_rechecks_maintenance_under_shared_release_barrier(configured,monkeypatch):
    bid,rid,sha,_=configured
    @contextmanager
    def concurrent_release():
        # Simulate release acquiring the barrier after initial HTTP validation.
        write_json_atomic(queue.state_dir()/'maintenance.json',{'reason':'release'})
        yield
    monkeypatch.setattr(http,'mutation_lock',concurrent_release,raising=False)
    app=serve()
    try:
        code,body=call(app,'POST',f'/api/batches/{bid}/preview-subtitles/{rid}',{'source_sha256':sha,'version_id':None})
        assert code==503
        assert queue.snapshot()['pending']==[]
    finally:app.shutdown();app.server_close()
