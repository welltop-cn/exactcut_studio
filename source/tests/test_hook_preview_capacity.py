"""Subtitle previews have an independent budget from background production."""
import pytest
from exactcut import batch_control, concurrency, hook_preview, webserver as web, workflow
from exactcut.runtime import instance_marker
from exactcut.util import write_json_atomic
from test_phase2_preview_api import fixture, call, serve, finished_hook


@pytest.mark.parametrize('running,enabled', [(0, True), (1, True), (2, True), (3, True), (1, False)])
def test_preview_works_while_background_tasks_are_running(fixture, tmp_path, monkeypatch, running, enabled):
    monkeypatch.setattr(web, 'JOBS', tmp_path / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web, 'INLINE_RENDERS', set())
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': {
        'enabled': enabled, 'fetch': 2, 'render': 3, 'upload': 2, 'poll': 4,
        'max_active_children': 3,
        'per_project': 1, 'hook_previews': 3,
        'ai': {'requests_per_minute': 6, 'burst': 1}}})
    processes = batch_control.FakeProcessTable({})
    monkeypatch.setattr(batch_control, 'PROCESSES', processes)
    for index in range(running):
        token = str(index + 1) * 32
        path = workflow.BATCHES / token
        path.mkdir()
        pid = 9000 + index
        child = dict(pid=pid, pgid=pid, process_token=token, lane='render', instance=instance_marker())
        write_json_atomic(path / 'runtime.json', {'status': 'producing'})
        write_json_atomic(path / 'control.json', {'children': [child]})
        processes.rows[pid] = dict(pgid=pid, state='S', arguments=[
            'python', '-X', 'exactcut_process_token=' + token])
    computed = []
    usage_during_preview = []

    def compute(project, request, work):
        computed.append(project)
        usage_during_preview.append(web.resource_usage())
        cache = work / 'cache'
        cache.mkdir()
        return {'preview_key': 'a' * 64}, {'decision': {}}, cache

    monkeypatch.setattr(hook_preview, 'compute', compute)
    assert web.resource_usage()[0]['render'] == running
    app = serve()
    try:
        endpoint = f"/api/batches/{fixture['id']}/hook-preview"
        body = {'revision': fixture['revision'], 'index': 0}
        status, result = call(app, 'POST', endpoint, body)
        assert status==202,result
        assert finished_hook(app,fixture['id'],result)['status']=='completed'
        assert len(computed) == 1
        counts, residents, _, _ = usage_during_preview[0]
        assert counts['render'] == running
        assert len(residents) == running
        assert not web.INLINE_RENDERS
    finally:
        app.shutdown()
        app.server_close()


def test_duplicate_hook_clicks_reuse_one_active_job_and_release_its_disk(fixture,tmp_path,monkeypatch):
    import threading,time
    from concurrent.futures import ThreadPoolExecutor
    entered,finish=threading.Event(),threading.Event();computed=[]
    def compute(project,request,work):
        computed.append(project);entered.set();assert finish.wait(5)
        cache=work/'cache';cache.mkdir();return {'preview_key':'a'*64},{'decision':{}},cache
    monkeypatch.setattr(hook_preview,'compute',compute)
    app=serve();endpoint=f"/api/batches/{fixture['id']}/hook-preview";body={'revision':fixture['revision'],'index':0}
    try:
        with ThreadPoolExecutor(max_workers=3) as clients:
            receipts=[future.result() for future in [clients.submit(call,app,'POST',endpoint,body) for _ in range(3)]]
        assert entered.wait(2)
        assert all(code==202 for code,_ in receipts)
        assert len({job['id'] for _,job in receipts})==1 and len(computed)==1
        assert call(app,'POST',endpoint,body)[1]['id']==receipts[0][1]['id']
        finish.set();assert finished_hook(app,fixture['id'],receipts[0][1])['status']=='completed'
        deadline=time.monotonic()+2
        while web.INLINE_RENDERS:assert time.monotonic()<deadline;time.sleep(.01)
        assert call(app,'POST',endpoint,body)[0]==200 and len(computed)==1
    finally:finish.set();app.shutdown();app.server_close()


def test_distinct_hook_jobs_use_two_workers_and_leave_third_persistently_queued(fixture,tmp_path,monkeypatch):
    import threading,time
    from test_phase2_preview_api import fixture as factory
    from exactcut import interactive_queue, editor_resources
    config=editor_resources.load_config()
    config.setdefault('editor_resources',{})['hook_tasks']=2
    monkeypatch.setattr(editor_resources,'load_config',lambda:config)
    batches=[fixture]+[factory.__wrapped__(tmp_path,monkeypatch) for _ in range(2)]
    for number,batch in enumerate(batches):
        path=workflow.folder(batch['id'])/'draft.json';doc=workflow.read_json(path)
        doc['draft']['rows'][0]['hook_text']=f'Independent hook {number}'
        write_json_atomic(path,doc)
    entered,finish=threading.Event(),threading.Event();lock=threading.Lock();calls=[];live=0;maximum=0
    def compute(project,request,work):
        nonlocal live,maximum
        with lock:
            calls.append(request.hook_text);live+=1;maximum=max(maximum,live)
            if live==2:entered.set()
        try:
            assert finish.wait(5)
            cache=work/'cache';cache.mkdir();return {'preview_key':'a'*64},{'decision':{}},cache
        finally:
            with lock:live-=1
    monkeypatch.setattr(hook_preview,'compute',compute);app=serve()
    try:
        jobs=[]
        for batch in batches:
            latest=workflow.detail(batch['id']);code,job=call(app,'POST',f"/api/batches/{batch['id']}/hook-preview",{'revision':latest['revision'],'index':0})
            assert code==202;jobs.append(job)
        assert entered.wait(2) and len(calls)==2
        snapshot=interactive_queue.snapshot();assert len(snapshot['active'])==2 and len(snapshot['pending'])==1
        assert web.resource_usage()[0]['render']==0
        finish.set()
        for batch,job in zip(batches,jobs):assert finished_hook(app,batch['id'],job)['status']=='completed'
        assert len(calls)==3 and maximum==2
    finally:finish.set();app.shutdown();app.server_close()


from interactive_test_support import interactive_dispatcher
