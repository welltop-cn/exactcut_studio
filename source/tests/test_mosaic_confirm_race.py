"""A client seeing a terminal render can immediately acquire its batch lock."""
from contextlib import contextmanager, nullcontext

import pytest
from exactcut import cutting, mosaic_render as render
from exactcut.util import read_json, write_json_atomic, file_sha256


@pytest.mark.parametrize('failed', [False, True])
def test_terminal_result_releases_batch_before_readers_can_confirm(tmp_path, monkeypatch, failed):
    path = tmp_path / 'render-job.json'
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'synthetic-source')
    job = {'id': 'job', 'status': 'running', 'targets': [], 'effect': {}, 'source_sha256': file_sha256(source)}
    write_json_atomic(path, job)
    batch_lock = tmp_path / 'batch.lock'
    leases = [cutting.lock_file(batch_lock, shared=True)]
    lock_file = cutting.lock_file
    observed = []

    @contextmanager
    def state_lock(file, **kwargs):
        with lock_file(file, **kwargs):
            yield
        state = read_json(path)
        if state['status'] in {'completed', 'failed'}:
            # A waiting confirm request reads terminal status under the state lock,
            # then needs this exclusive batch lock without returning a spurious 409.
            with lock_file(batch_lock):
                observed.append(state['status'])

    def process(*args, **kwargs):
        if failed:
            raise cutting.CutError('synthetic render failure')
        return {'sha256': 'a' * 64}

    monkeypatch.setattr(cutting, 'lock_file', state_lock)
    monkeypatch.setattr(render, 'current', lambda *a, **kw: {'request_id': 'R1', 'metadata': {}})
    monkeypatch.setattr(render.workflow, 'folder', lambda *a: tmp_path)
    monkeypatch.setattr(render.storage, 'require_space', lambda *a: None)
    monkeypatch.setattr(render.cutting_access, 'gate', lambda *a, **kw: nullcontext())
    monkeypatch.setattr(render, 'process_video', process)
    try:
        render._work_cancelable('batch', 'session', path, job, {}, leases)
    finally:
        for lease in leases:
            lease.close()
    assert observed == ['failed' if failed else 'completed']
