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
