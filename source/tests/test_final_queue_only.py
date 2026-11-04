"""Final renders wait for media capacity without a second admission lock."""
from contextlib import ExitStack
from pathlib import Path
import threading
import time

import pytest
from exactcut import cutting, editor_resources
from exactcut.util import read_json
from test_cutting_api import ready
from test_phase2_preview_api import fixture


def test_production_source_has_no_compute_lock():
    source = Path(__file__).resolve().parents[1]/'exactcut'
    assert not [path.relative_to(source) for path in source.rglob('*.py')
                if 'compute.lock' in path.read_text()]


@pytest.mark.integration
def test_legacy_render_waits_for_final_media_and_becomes_ready(ready, monkeypatch):
    bid, rid, sha, source = ready
    session = cutting.open_session(bid, rid, sha)
    path = cutting.session_path(bid, session['id'])/'session.json'
    waiting = threading.Event()
    original_wait = editor_resources.wait

    def wait(kind, *args, **kwargs):
        if kind == 'final_media':
            waiting.set()
        return original_wait(kind, *args, **kwargs)

    monkeypatch.setattr(editor_resources, 'wait', wait)
    with ExitStack() as stack:
        for _ in range(editor_resources.settings()['final_media_tasks']):
            stack.enter_context(editor_resources.acquire('final_media'))
        # Direct legacy entry: submission succeeds while every final slot is held.
        job = cutting.start_render(bid, session['id'], [[1, 3]])
        assert job['status'] == 'running'
        assert waiting.wait(5), 'worker did not reach the final media stage'
        assert cutting.get_job(read_json(path), job['id'])['status'] == 'running'
        assert not path.with_name(job['id']+'.mp4').exists()

    deadline = time.monotonic()+10
    while True:
        saved = cutting.get_job(read_json(path), job['id'])
        if saved['status'] != 'running':
            break
        assert time.monotonic() < deadline, 'legacy render did not finish'
        time.sleep(.02)
    assert saved['status'] == 'ready', saved


@pytest.mark.integration
def test_claimed_final_job_stays_queued_until_a_final_slot_frees(ready):
    from exactcut import accounts, workflow, mosaic_queue as queue, mosaic_render as render
    from test_cutting_api import call, prepared
    from test_phase2_preview_api import serve
    from test_mosaic_queue_integration import wait
    bid, rid, sha, source = ready
    accounts.initialize(workflow.BATCHES.parent/'accounts.sqlite3', 'final-queue-password')
    app = serve(); root = f'/api/batches/{bid}/mosaics'
    dispatcher = queue.Dispatcher(.01)
    try:
        with ExitStack() as stack:
            for _ in range(editor_resources.settings()['final_media_tasks']):
                stack.enter_context(editor_resources.acquire('final_media'))
            dispatcher.start()
            code, session = call(app, 'POST', root+'/open', {'request_id': rid, 'source_sha256': sha})
            assert code in {200, 202}; session = prepared(app, root, session)
            path = root+'/'+session['id']
            code, job = call(app, 'POST', path+'/render', {'targets': [], 'ranges': [[1, 3]], 'reset_at': ''})
            assert code == 202, job
            deadline = time.monotonic()+5
            while call(app, 'GET', path+'/render')[1]['job']['progress'] != render.QUEUED_PROGRESS:
                assert time.monotonic() < deadline, 'claimed job never reported its queue wait'
                time.sleep(.02)
            shown = call(app, 'GET', path+'/render')[1]['job']
            assert shown['status'] == 'queued' and shown['progress'].startswith('排队中')
            # The waiting worker holds no asset gate: same-material edits get a 409 conflict.
            assert call(app, 'POST', path+'/render', {'targets': [], 'ranges': [[4, 6]], 'reset_at': ''})[0] == 409
            assert call(app, 'POST', f'/api/batches/{bid}/cuts/open', {'request_id': rid, 'source_sha256': sha})[0] == 409
        assert wait(bid, session['id'], job['id'])['status'] == 'completed'
    finally:
        dispatcher.stop(); app.shutdown(); app.server_close()


from interactive_test_support import interactive_dispatcher  # noqa: E402,F401
