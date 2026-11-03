"""Hook and locate claims use independent execution slots, including old entries."""
from contextlib import ExitStack

import pytest

from exactcut import cutting, editor_resources, interactive_queue as queue
from exactcut.util import read_json, write_json_atomic


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(cutting, 'lock_root', lambda: tmp_path / 'batch-locks')
    monkeypatch.setattr(editor_resources, 'load_config', lambda: {
        'editor_resources': {
            'preview_tasks': 2, 'scan_tasks': 2,
            'preprocess_tasks': 2, 'encoder_threads': 2,
        },
    })
    return tmp_path


@pytest.fixture
def leases():
    with ExitStack() as stack:
        yield stack


def submit(root, number, kind='hook'):
    session = {
        'batch_id': f'{number:032x}', 'id': f'{number:032x}',
        'request_id': f'R{number}', 'source_sha256': 'a' * 64, 'reset_at': '',
    }
    path = root / f'session-{number}' / f'render-{number:032x}.json'
    job = {
        'schema_version': 1, 'id': f'{number:032x}', 'status': 'queued',
        'actor': 'alpha', 'source_sha256': 'a' * 64, 'reset_at': '',
        'preview': True, 'progress': '等待处理',
    }
    if kind == 'locate':
        job['seed_frame'] = 7
        frame = path.parent / 'frames' / f'{job["seed_frame"]}.jpg'
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b'cached-frame-fixture')
    return queue.enqueue(
        kind, session, path, job, {}, state_lock=path.parent / 'render-state.lock',
    )


def fill_role(role, leases):
    capacity = editor_resources.settings()[f'{role}_tasks']
    for _ in range(capacity):
        leases.enter_context(editor_resources.acquire(role))
    with pytest.raises(cutting.ResourceBusy):
        leases.enter_context(editor_resources.acquire(role))


def claim(dispatcher, leases):
    claimed = dispatcher.claim()
    if claimed is not None:
        entry, acquired, initial = claimed
        for lease in acquired:
            leases.callback(lease.close)
        return entry, acquired, initial
    return None


def test_hook_claims_when_preview_is_full(state, leases):
    fill_role('preview', leases)
    submitted = submit(state, 1)

    claimed = claim(queue.Dispatcher(), leases)

    assert claimed is not None
    entry, _, initial = claimed
    assert entry['task_id'] == submitted['task_id']
    assert entry['role'] == 'hook'
    assert initial is None


def test_locate_claims_when_scan_is_full(state, leases):
    fill_role('scan', leases)
    submitted = submit(state, 1, kind='locate')

    claimed = claim(queue.Dispatcher(), leases)

    assert claimed is not None
    entry, _, initial = claimed
    assert entry['task_id'] == submitted['task_id']
    assert entry['role'] == 'locate'
    assert initial is None


def test_hook_default_capacity_allows_only_three_claims(state, leases):
    submitted = [submit(state, number) for number in range(1, 5)]
    dispatcher = queue.Dispatcher()
    claimed_ids = []
    for _ in range(3):
        claimed = claim(dispatcher, leases)
        assert claimed is not None
        entry, _, initial = claimed
        assert entry['role'] == 'hook'
        assert initial is None
        claimed_ids.append(entry['task_id'])

    assert claimed_ids == [entry['task_id'] for entry in submitted[:3]]
    assert claim(dispatcher, leases) is None
    snapshot = queue.snapshot()
    assert [entry['task_id'] for entry in snapshot['pending']] == [submitted[3]['task_id']]
    assert len(snapshot['active']) == 3


def test_legacy_preview_role_hook_claims_when_preview_is_full(state, leases):
    submitted = submit(state, 1)
    queue_path = queue.directory() / 'queue.json'
    pending = read_json(queue_path)
    pending['pending'][0]['role'] = 'preview'
    write_json_atomic(queue_path, pending)
    fill_role('preview', leases)

    claimed = claim(queue.Dispatcher(), leases)

    assert claimed is not None
    entry, _, initial = claimed
    assert entry['task_id'] == submitted['task_id']
    assert entry['role'] == 'hook'
    assert initial is None
    snapshot = queue.snapshot()
    assert snapshot['pending'] == []
    assert snapshot['active'][0]['role'] == 'hook'
