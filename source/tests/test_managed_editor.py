"""Existing editor helpers remain available after lifecycle enrollment."""
import queue
import threading
import uuid

import pytest

from exactcut import material_versions as m, webserver as web, workflow as w, hook_preview
from test_phase2_preview_api import fixture, serve, call


@pytest.fixture(params=[False, True], ids=["draft", "existing-preview"])
def managed_editor(fixture, monkeypatch, request):
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    monkeypatch.setattr(web, 'JOBS', w.ROOT / '.web-state/jobs')
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    path = w.folder(fixture['id'])
    if request.param:
        from test_workflow import complete_asset
        complete_asset(fixture)
    m.enroll(path)
    app = serve()
    try:
        yield app, path
    finally:
        app.shutdown(); app.server_close()


def test_managed_hook_preview_keeps_authority_unchanged(managed_editor, monkeypatch):
    app, path = managed_editor
    def compute(project, request, work):
        return {'preview_key': 'a' * 64}, {'decision': {}}, work / 'cache'
    monkeypatch.setattr(hook_preview, 'compute', compute)
    before = (path / 'media-state.json').read_bytes()
    status, result = call(app, 'POST', f'/api/batches/{path.name}/hook-preview',
                          {'revision': m.read(path)['revision'], 'index': 0})
    assert status == 200, result
    assert (path / 'media-state.json').read_bytes() == before


def test_managed_prefetch_queues_without_changing_authority(managed_editor, monkeypatch):
    app, path = managed_editor
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    before = (path / 'media-state.json').read_bytes()
    status, result = call(app, 'POST', f'/api/batches/{path.name}/sources',
                          {'draft': m.read(path)['draft']})
    assert status == 200, result
    assert result['episodes']
    assert list(web.stored_jobs())[0]['action'] == 'prefetch'
    assert (path / 'media-state.json').read_bytes() == before


def test_managed_preview_rejects_concurrent_edit(managed_editor, monkeypatch):
    app, path = managed_editor
    entered, finish = threading.Event(), threading.Event()
    def compute(project, request, work):
        entered.set()
        assert finish.wait(2)
        return {'preview_key': 'b' * 64}, {}, work / 'cache'
    monkeypatch.setattr(hook_preview, 'compute', compute)
    state = m.read(path)
    responses = []
    worker = threading.Thread(target=lambda: responses.append(call(app, 'POST',
        f'/api/batches/{path.name}/hook-preview', {'revision': state['revision'], 'index': 0})))
    try:
        worker.start()
        assert entered.wait(1)
        state['draft']['rows'][0]['hook_text'] = 'Changed during preview'
        status, result = call(app, 'POST', f'/api/batches/{path.name}/save',
            {'revision': state['revision'], 'mutation_id': uuid.uuid4().hex, 'draft': state['draft']})
        assert status == 200, result
        finish.set(); worker.join(3)
        assert responses[0][0] == 409
        assert not (w.ROOT / 'projects/101460/cache/hook-previews' / ('b' * 64 + '.json')).exists()
    finally:
        finish.set(); worker.join(3)


def test_managed_prefetch_worker_and_archive_fence(managed_editor, monkeypatch):
    from exactcut import source_prefetch, source_cache
    app, path = managed_editor
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    state = m.read(path)
    assert call(app, 'POST', f'/api/batches/{path.name}/sources', {'draft': state['draft']})[0] == 200
    job = list(web.stored_jobs())[0]
    calls = []
    monkeypatch.setattr(source_cache, 'ensure', lambda drama, episodes, **kw: calls.extend(episodes))
    assert source_prefetch.run(path.name, job['id']) == 0
    assert calls == job['episodes']
    m.control(path, 'archive', {'revision': state['revision'], 'mutation_id': uuid.uuid4().hex})
    calls.clear()
    assert source_prefetch.run(path.name, job['id']) == 0
    assert calls == []
    assert call(app, 'POST', f'/api/batches/{path.name}/sources', {'draft': state['draft']})[0] == 400
    assert call(app, 'POST', f'/api/batches/{path.name}/hook-preview',
                {'revision': m.read(path)['revision'], 'index': 0})[0] == 400


def test_global_enable_enrolls_new_blank_before_prefetch(managed_editor, monkeypatch):
    app, _ = managed_editor
    monkeypatch.setattr(m, 'enabled', lambda: True)
    status, result = call(app, 'POST', '/api/batches/blank', {})
    assert status == 201, result
    assert result.get('media_managed') is True
    assert m.read(w.folder(result['id']))['draft'] == result['draft']
