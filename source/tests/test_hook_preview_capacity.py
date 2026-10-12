"""Subtitle previews have an independent budget from background production."""
import pytest
from exactcut import batch_control, concurrency, hook_preview, webserver as web, workflow
from exactcut.runtime import instance_marker
from exactcut.util import write_json_atomic
from test_phase2_preview_api import fixture, call, serve


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
        assert status == 200, result
        assert len(computed) == 1
        counts, residents, _, _ = usage_during_preview[0]
        assert counts['render'] == running
        assert len(residents) == running
        assert not web.INLINE_RENDERS
    finally:
        app.shutdown()
        app.server_close()


def test_three_preview_requests_run_together_and_release_slots(fixture, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(web, 'JOBS', tmp_path / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web, 'INLINE_RENDERS', set())
    monkeypatch.setattr(concurrency, 'load_config', lambda: {
        'concurrency': {'hook_previews': 3, 'render': 1, 'max_active_children': 1}})
    entered, finish = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = []

    def compute(project, request, work):
        with lock:
            calls.append(project)
            if len(calls) == 3:
                entered.set()
        if not finish.wait(5):
            raise RuntimeError('test did not release preview barrier')
        cache = work / 'cache'
        cache.mkdir()
        return {'preview_key': 'a' * 64}, {'decision': {}}, cache

    monkeypatch.setattr(hook_preview, 'compute', compute)
    app = serve()
    endpoint = f"/api/batches/{fixture['id']}/hook-preview"
    body = {'revision': fixture['revision'], 'index': 0}
    try:
        with ThreadPoolExecutor(max_workers=3) as clients:
            futures = [clients.submit(call, app, 'POST', endpoint, body) for _ in range(3)]
            try:
                assert entered.wait(2), 'three previews must enter compute concurrently'
                assert call(app, 'POST', endpoint, body)[0] == 429
                assert len(calls) == 3
                assert web.resource_usage()[0]['render'] == 0
            finally:
                finish.set()
            assert [future.result()[0] for future in futures] == [200, 200, 200]
        assert not web.INLINE_RENDERS
        assert call(app, 'POST', endpoint, body)[0] == 200
        assert not web.INLINE_RENDERS
    finally:
        finish.set()
        app.shutdown()
        app.server_close()
