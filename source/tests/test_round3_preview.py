"""Expose the reply/finally admission race without sleeps or test-order coupling."""
import threading

import pytest
from exactcut import hook_preview, stage_results, webserver as web
from exactcut.util import write_json_atomic
from test_phase2_preview_api import fixture, call, serve


@pytest.mark.parametrize('first', ['deferred', 'success', 'error'])
def test_preview_reply_releases_slot_before_client_retry(fixture, tmp_path, monkeypatch, first):
    monkeypatch.setattr(web, 'JOBS', tmp_path / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web, 'INLINE_RENDERS', set())
    clock = [1000.]
    monkeypatch.setattr(web.time, 'time', lambda: clock[0])
    replied, return_from_reply = threading.Event(), threading.Event()
    attempts = []
    def compute(project, request, work):
        attempts.append(project)
        cache = work / 'cache'
        for name in ('hook-emphasis', 'hook-placement'): (cache / name).mkdir(parents=True)
        if len(attempts) == 1:
            if first == 'deferred':
                write_json_atomic(cache / 'hook-emphasis' / ('b' * 64 + '.json'), {'complete': True})
                raise stage_results.ResourceDeferred(1010.)
            if first == 'error': raise OSError('fixture compute failure')
        return {'preview_key': 'a' * 64}, {'decision': {}}, cache
    monkeypatch.setattr(hook_preview, 'compute', compute)
    respond = web.Handler.respond
    paused = []
    def pause_first_reply(self, code, data):
        respond(self, code, data)
        if not paused:
            paused.append(code)
            replied.set()
            assert return_from_reply.wait(5), 'test must release server response barrier'
    monkeypatch.setattr(web.Handler, 'respond', pause_first_reply)
    app = serve()
    path = f"/api/batches/{fixture['id']}/hook-preview"
    body = {'revision': fixture['revision'], 'index': 0}
    response = []
    client = threading.Thread(target=lambda: response.append(call(app, 'POST', path, body)))
    try:
        client.start()
        assert replied.wait(3)
        # Old code deterministically leaves its render token held here: the
        # client already has a complete response but finally has not run yet.
        assert not web.INLINE_RENDERS, 'response exposed before admission cleanup'
        clock[0] = 1010.
        assert call(app, 'POST', path, body)[0] == 200
        assert len(attempts) == 2
    finally:
        return_from_reply.set(); client.join(3)
        app.shutdown(); app.server_close()
    assert response[0][0] == {'deferred': 429, 'success': 200, 'error': 400}[first]
    assert not web.INLINE_RENDERS
