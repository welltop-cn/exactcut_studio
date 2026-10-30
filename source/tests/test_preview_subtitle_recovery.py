"""Recovery must execute the real worker after a queue/report split write."""
import shutil
import time
import pytest
from exactcut import preview_subtitle_queue as queue, preview_subtitles as subtitles
from exactcut import preview_subtitle_model as model
from exactcut.util import read_json
from test_phase2_preview_api import fixture
from test_cutting_api import ready
from test_preview_subtitles import configured, dispatcher, CUE

@pytest.mark.parametrize('remove_directory', [False, True])
def test_pending_without_report_recovers_through_real_worker(configured, dispatcher, monkeypatch, remove_directory):
    bid, rid, sha, _ = configured
    subtitles.start(bid, rid, sha, None, 'admin')
    task = queue.snapshot()['pending'][0]
    path = subtitles.report_dir(bid, task['task_id']) / 'report.json'
    if remove_directory:
        shutil.rmtree(path.parent)
    else:
        path.unlink()
    calls = []
    monkeypatch.setattr(model, 'recognize', lambda *args: calls.append(True) or [CUE])
    dispatcher()
    for _ in range(400):
        view = queue.snapshot()
        if not view['pending'] and view['active'] is None:
            break
        time.sleep(.01)
    assert read_json(path)['status'] == 'completed'
    assert read_json(path)['cues'] == [CUE]
    assert calls == [True]
