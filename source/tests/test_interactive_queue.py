"""Durable interactive waiting is independent of execution and account counts."""
import threading,time
import pytest
from exactcut import interactive_queue as queue,editor_resources
from exactcut.util import read_json,write_json_atomic


@pytest.fixture
def state(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    monkeypatch.setattr(editor_resources,'load_config',lambda:{'editor_resources':{'preview_tasks':2,'scan_tasks':2,'preprocess_tasks':2,'encoder_threads':2}})
    return tmp_path


def submit(root,number,actor='alpha',sid=None):
    session={'batch_id':f'{number:032x}','id':sid or f'{number:032x}','request_id':f'R{number}','source_sha256':'a'*64,'reset_at':''}
    path=root/f'session-{number}'/f'render-{number:032x}.json'
    job={'schema_version':1,'id':f'{number:032x}','status':'queued','actor':actor,'source_sha256':'a'*64,'reset_at':'','preview':True,'progress':'等待处理'}
    return queue.enqueue('clip',session,path,job,{},state_lock=path.parent/'render-state.lock')


def wait(predicate):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    assert predicate()


@pytest.mark.parametrize('failure, should_alert', [
    (RuntimeError('fixture render failed'), True),
    (queue.cutting.CutError('fixture version conflict', 409), False),
    (queue.cutting.ResourceBusy('frame'), False),
])
def test_failed_interactive_work_generates_application_alert(state, monkeypatch, failure, should_alert):
    from exactcut import diagnostics, util
    from exactcut.alerts import AlertQueue
    config = util.load_config()
    config['alerts']['enabled'] = True
    monkeypatch.setattr(util, 'load_config', lambda: config)
    diagnostics.configure(state)
    entry = submit(state, 1)
    def fail(entry, leases):
        raise failure
    dispatcher = queue.Dispatcher(run=fail, poll_seconds=.01)
    try:
        assert dispatcher.start()
        wait(lambda:not queue.snapshot()['pending'] and not queue.snapshot()['active'])
        assert read_json(state / entry['report'])['status'] == 'failed'
        sent = []
        AlertQueue(state, config['alerts']).flush(sender=sent.append)
        if should_alert:
            assert len(sent) == 1 and 'interactive.failed' in sent[0] and 'R1' in sent[0]
        else:
            assert sent == []
    finally:
        dispatcher.stop(timeout=None)
        diagnostics.disable()


def test_thread_launch_failure_generates_application_alert(state, monkeypatch):
    from exactcut import diagnostics, util
    from exactcut.alerts import AlertQueue
    config = util.load_config()
    config['alerts']['enabled'] = True
    monkeypatch.setattr(util, 'load_config', lambda: config)
    diagnostics.configure(state)
    entry = submit(state, 1)
    start = threading.Thread.start
    def fail_worker_launch(thread):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('fixture cannot launch worker thread')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_worker_launch)
    dispatcher = queue.Dispatcher(run=lambda *args: pytest.fail('worker must not run'), poll_seconds=.01)
    try:
        assert dispatcher.start()
        wait(lambda:not queue.snapshot()['pending'] and not queue.snapshot()['active'])
        assert read_json(state / entry['report'])['status'] == 'failed'
        sent = []
        AlertQueue(state, config['alerts']).flush(sender=sent.append)
        assert len(sent) == 1 and 'interactive.launch_failed' in sent[0] and 'R1' in sent[0]
    finally:
        dispatcher.stop(timeout=None)
        diagnostics.disable()


def test_admission_persists_more_than_execution_capacity_and_deduplicates(state):
    entries=[submit(state,number) for number in range(1,7)]
    assert len(queue.snapshot()['pending'])==6
    assert submit(state,1)['task_id']==entries[0]['task_id']
    assert len(queue.snapshot()['pending'])==6
    assert all(read_json(state/entry['report'])['status']=='queued' for entry in entries)
    assert not queue.snapshot()['active']


def test_two_same_account_execute_others_wait_without_threads_and_other_owner_is_next(state):
    for number in range(1,5):submit(state,number)
    condition=threading.Condition();entered=[];release=threading.Event()
    def run(entry,leases):
        with condition:entered.append(entry['actor']);condition.notify_all()
        assert release.wait(5)
        path=state/entry['report'];job=read_json(path);job['status']='completed';write_json_atomic(path,job)
    dispatcher=queue.Dispatcher(run=run,poll_seconds=.01)
    try:
        assert dispatcher.start()
        wait(lambda:len(entered)==2)
        assert entered==['alpha','alpha']
        submit(state,5,'beta')
        assert len(queue.snapshot()['pending'])==3 and len(queue.snapshot()['active'])==2
        assert dispatcher.active_workers==2
        release.set();wait(lambda:len(entered)==5)
        assert entered[2]=='beta'
        wait(lambda:not queue.snapshot()['active'])
    finally:release.set();dispatcher.stop(timeout=None)


def test_dispatcher_restart_keeps_pending_and_only_one_dispatcher_owns_it(state):
    first=submit(state,1)
    runs=[]
    def run(entry,leases):
        runs.append(entry['task_id']);path=state/entry['report'];job=read_json(path);job['status']='completed';write_json_atomic(path,job)
    one=queue.Dispatcher(run=run,poll_seconds=.01);other=queue.Dispatcher(run=run,poll_seconds=.01)
    try:
        assert one.start();assert other.start() is False
        wait(lambda:runs==[first['task_id']] and not queue.snapshot()['active'])
    finally:one.stop(timeout=None);other.stop(timeout=None)


def test_queued_cancel_never_starts_work(state):
    entry=submit(state,1);path=state/entry['report'];job=read_json(path);job.update(status='cancelled',cancel_requested=True);write_json_atomic(path,job)
    runs=[];dispatcher=queue.Dispatcher(run=lambda *a:runs.append(a),poll_seconds=.01)
    try:
        assert dispatcher.start();wait(lambda:not queue.snapshot()['pending'])
        assert not runs and read_json(path)['status']=='cancelled'
    finally:dispatcher.stop(timeout=None)


def test_full_initial_media_pool_keeps_work_pending_without_waiter_threads(state):
    from contextlib import ExitStack
    for number in range(1,8):submit(state,number)
    calls=[];dispatcher=queue.Dispatcher(run=lambda *args:calls.append(args),poll_seconds=.01)
    try:
        with ExitStack() as held:
            for _ in range(5):held.enter_context(editor_resources.acquire('media'))
            assert dispatcher.start()
            time.sleep(.1)
            assert not calls and dispatcher.active_workers==0
            assert len(queue.snapshot()['pending'])==7 and not queue.snapshot()['active']
    finally:dispatcher.stop(timeout=None)
