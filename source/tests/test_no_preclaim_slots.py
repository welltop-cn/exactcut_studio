"""Claim only reserves resources needed at the start of each task."""
from contextlib import ExitStack

import pytest
from exactcut import cutting, editor_resources, interactive_queue as queue


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(editor_resources, 'load_config', lambda: {
        'editor_resources': {'preview_tasks': 2, 'scan_tasks': 2,
                             'preprocess_tasks': 2, 'encoder_threads': 2}})
    monkeypatch.setattr(cutting, 'lock_root', lambda: tmp_path/'locks')
    return tmp_path


def submit(root, kind):
    session = {'batch_id': '1'*32, 'id': '2'*32, 'request_id': 'R1',
               'source_sha256': 'a'*64, 'reset_at': ''}
    path = root/'session'/'job.json'
    job = {'schema_version': 1, 'id': '3'*32, 'status': 'queued',
           'actor': 'alpha', 'source_sha256': 'a'*64, 'reset_at': '',
           'seed_frame': 0, 'preview': True, 'progress': '等待处理'}
    return queue.enqueue(kind, session, path, job, {},
                         state_lock=path.parent/'state.lock')


@pytest.mark.parametrize(('kind', 'resource'), [
    ('track', 'media'),
    ('locate', 'frame'),
    ('mosaic_scan', 'preprocess'),
    ('cut_scan', 'preprocess'),
])
def test_claim_succeeds_when_later_stage_slots_are_full(state, kind, resource):
    entry = submit(state, kind)
    if kind == 'locate':
        assert not (queue.report(entry).parent/'frames'/'0.jpg').exists()
    with ExitStack() as held:
        for _ in range(editor_resources.settings()[resource+'_tasks']):
            held.enter_context(editor_resources.acquire(resource))
        claimed = queue.Dispatcher().claim()
        assert claimed is not None
        actual, leases, initial = claimed
        for lease in leases:
            held.callback(lease.close)
        assert actual['task_id'] == entry['task_id']
        if kind in {'track', 'locate'}:
            assert initial is None
        else:
            assert initial[0] == 'media'


def test_clip_still_waits_when_media_slots_are_full(state):
    entry = submit(state, 'clip')
    with ExitStack() as held:
        for _ in range(editor_resources.settings()['media_tasks']):
            held.enter_context(editor_resources.acquire('media'))
        claimed = queue.Dispatcher().claim()
        if claimed is not None:
            for lease in claimed[1]:
                held.callback(lease.close)
        assert claimed is None
        assert queue.snapshot()['pending'] == [entry]
