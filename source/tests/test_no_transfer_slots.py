"""Preparation needs no transfer lease; OSS object locks still serialize work."""
import threading

import pytest

from exactcut import cutting, editor_resources, interactive_queue as queue, resource_locks


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(editor_resources, 'load_config', lambda: {
        'editor_resources': {'preprocess_tasks': 2, 'encoder_threads': 2}})
    monkeypatch.setattr(cutting, 'lock_root', lambda: tmp_path / 'locks')
    return tmp_path


def test_transfer_resource_is_removed(state):
    assert 'transfer_tasks' not in editor_resources.settings()
    with pytest.raises(ValueError):
        editor_resources.acquire('transfer')


@pytest.mark.parametrize('kind', ['cut_prepare', 'mosaic_prepare', 'preview_oss'])
def test_preparation_claims_without_initial_resource(state, kind):
    session = {'batch_id': 'b' * 32, 'id': 'c' * 32, 'request_id': 'R1',
               'source_sha256': 'a' * 64, 'reset_at': ''}
    path = state / 'session' / 'prepare.json'
    job = {'id': 'd' * 32, 'status': 'queued', 'actor': 'alpha'}
    entry = queue.enqueue(kind, session, path, job, {},
                          state_lock=path.parent / 'state.lock')
    claimed = queue.Dispatcher().claim()
    assert claimed is not None
    actual, leases, initial = claimed
    try:
        assert actual['task_id'] == entry['task_id']
        assert initial is None
    finally:
        for lease in reversed(leases):
            lease.close()


def test_same_oss_object_lock_waits_until_released(state):
    identity = ['fixture-endpoint', 'fixture-bucket', 'preview.mp4']
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    errors = []

    def first():
        try:
            with resource_locks.hold('oss-object', identity, timeout=5):
                first_entered.set()
                assert release_first.wait(5)
        except BaseException as exc:
            errors.append(exc)

    def second():
        try:
            second_attempted.set()
            with resource_locks.hold('oss-object', identity, timeout=5):
                second_entered.set()
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=first, daemon=True)
    waiter = threading.Thread(target=second, daemon=True)
    owner.start()
    try:
        assert first_entered.wait(3)
        waiter.start()
        assert second_attempted.wait(3)
        assert not second_entered.wait(.2)
        release_first.set()
        assert second_entered.wait(3)
    finally:
        release_first.set()
        owner.join(timeout=6)
        if waiter.ident is not None:
            waiter.join(timeout=6)
    assert not owner.is_alive() and not waiter.is_alive()
    assert not errors
