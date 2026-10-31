"""Durable final-edit queue contracts; isolated synthetic data only."""
import pytest
from exactcut import workflow, cutting
from exactcut import mosaic_queue as queue
from exactcut.util import read_json, write_json_atomic


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, 'BATCHES', tmp_path/'batches')
    return tmp_path


def submit(actor, rid):
    return queue.enqueue('a'*32, 'b'*32, rid, actor,
                         {'id':rid,'schema_version':1,'status':'queued','reset_at':'',
                          'source_sha256':'c'*64,'targets':[],'ranges':[[1,2]],'config':{}})


def test_durable_round_robin_and_per_user_fifo(isolated):
    for actor,rid in [('a','1'),('a','2'),('a','3'),('b','4'),('b','5')]:submit(actor,rid)
    sequence=[]
    while item:=queue.claim():
        sequence.append(item['id'])
        queue.finish(item['id'])
    assert sequence==['1','4','2','5','3']
    assert read_json(queue.queue_path())['schema_version']==1


def test_slots_and_same_user_serial(isolated):
    submit('a','1')
    queue.enqueue('d'*32,'b'*32,'2','a',{'id':'2','status':'queued'})
    submit('b','3')
    assert queue.claim(2)['id']=='1'
    assert queue.claim(2)['id']=='3'
    assert queue.claim(2) is None
    queue.finish('1')
    assert queue.claim(2)['id']=='2'


def test_enqueue_commit_recovers_missing_projection(isolated):
    item=submit('a','1');queue.job_path(item).unlink()
    queue.recover()
    assert read_json(queue.job_path(item))['status']=='queued'
    assert queue.claim()['id']=='1'


def test_cancel_intent_survives_recovery_and_no_resurrection(isolated):
    item=submit('a','1');queue.claim()
    job=read_json(queue.job_path(item));job.update(cancel_requested=True,status='cancelling')
    write_json_atomic(queue.job_path(item),job)
    queue.recover()
    assert read_json(queue.job_path(item))['status']=='cancelled'
    assert queue.claim() is None


def test_recovery_bounded_and_completed_never_repeated(isolated):
    item=submit('a','1')
    for _ in range(3):
        assert queue.claim()
        queue.recover()
    assert queue.claim() is None
    assert read_json(queue.job_path(item))['status']=='failed'


def test_final_result_locks_until_decision(isolated):
    item=submit('a','1');path=queue.job_path(item)
    assert queue.blocked('a'*32,'1')
    write_json_atomic(path,{**read_json(path),'status':'completed'})
    assert queue.blocked('a'*32,'1')
    write_json_atomic(path,{**read_json(path),'discarded_at':'now'})
    assert not queue.blocked('a'*32,'1')


def test_admission_survives_projection_write_failure(isolated,monkeypatch):
    project=queue._project
    def fail(item):raise OSError('simulated projection write failure')
    monkeypatch.setattr(queue,'_project',fail)
    with pytest.raises(OSError):submit('a','1')
    assert len(queue.load()['tasks'])==1
    assert queue.blocked('a'*32,'1')
    monkeypatch.setattr(queue,'_project',project)
    queue.recover()
    item=queue.claim();assert item['id']=='1'
    assert read_json(queue.job_path(item))['ranges']==[[1,2]]


def test_completed_report_before_release_is_not_reexecuted(isolated):
    item=submit('a','1');queue.claim()
    write_json_atomic(queue.job_path(item),{**queue.report(item),'status':'completed','result':{'sha256':'d'*64}})
    queue.recover()
    assert queue.load()['tasks'][0]['state']=='done'
    assert queue.claim() is None
    assert queue.blocked('a'*32,'1')
