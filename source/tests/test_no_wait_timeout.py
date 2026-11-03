"""Resource waits remain cancellable; SAM deadlines cover the requested frames."""
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from exactcut import cutting, editor_resources, mosaic_inference, mosaic_tracking, processing_cancel
from exactcut.util import write_json_atomic


@pytest.fixture(autouse=True)
def isolated_resources(monkeypatch, tmp_path):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(editor_resources, 'load_config', lambda: {'editor_resources': {}})


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0, step=1801, sleeps=0, on_sleep=lambda: None)

    def sleep(seconds):
        assert seconds == .05
        clock.now += clock.step
        clock.sleeps += 1
        clock.on_sleep()

    monkeypatch.setattr(editor_resources.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(editor_resources.time, 'sleep', sleep)
    return clock


@pytest.fixture
def occupied_media():
    with ExitStack() as held:
        leases = [held.enter_context(editor_resources.acquire('media'))
                  for _ in range(editor_resources.settings()['media_tasks'])]
        yield leases


def test_scoped_wait_past_1800_remains_cancellable(clock, occupied_media):
    flag = False

    def cancel_after_two_polls():
        nonlocal flag
        assert clock.now > 1800
        if clock.sleeps == 2:
            flag = True

    clock.on_sleep = cancel_after_two_polls
    with processing_cancel.scope(lambda: flag):
        with pytest.raises(processing_cancel.Cancelled):
            editor_resources.wait('media')
    assert clock.sleeps == 2


def test_unscoped_wait_keeps_1800_second_limit(clock, occupied_media):
    assert not processing_cancel.scoped()
    with pytest.raises(cutting.CutError, match='等待处理资源超时，请重试') as error:
        editor_resources.wait('media')
    assert error.value.status == 503
    assert clock.now == 1801


def test_explicit_timeout_applies_inside_scope(clock, occupied_media):
    clock.step = 1.1
    with processing_cancel.scope(lambda: False):
        with pytest.raises(cutting.CutError, match='等待处理资源超时，请重试') as error:
            editor_resources.wait('media', timeout=1)
    assert error.value.status == 503
    assert clock.now == 1.1


def test_scoped_wait_acquires_released_slot_after_1800(clock, occupied_media):
    clock.on_sleep = occupied_media[0].close
    with processing_cancel.scope(lambda: False):
        with editor_resources.wait('media') as lease:
            assert isinstance(lease, editor_resources.Lease)
            assert lease.filenos()
            assert clock.now > 1800
    assert not lease.filenos()


@pytest.mark.parametrize('frame_count, expected', [(300, 1260), (1, 64), (0, 64), (-1, 64), ('2', 68)])
def test_sam_timeout(frame_count, expected):
    assert mosaic_tracking.sam_timeout(frame_count) == expected


@pytest.mark.parametrize('frame_count', [1, 300])
def test_run_tracker_passes_frame_based_timeout(monkeypatch, tmp_path, frame_count):
    directory = tmp_path / 'track'
    target = {'start': 10, 'end': 10 + frame_count}
    result = {'width': 16, 'height': 16,
              'frames': [{'frame': frame, 'box': None, 'area': 0}
                         for frame in range(target['start'], target['end'])]}
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs['timeout'])
        assert command[2] == 'exactcut.mosaic_sam2'
        write_json_atomic(directory / 'result.json', result)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mosaic_tracking.storage, 'require_space', lambda *args: None)
    monkeypatch.setattr(processing_cancel, 'run', run)
    assert mosaic_tracking.run_tracker(tmp_path / 'source.mp4', directory,
                                      {'width': 16, 'height': 16}, target,
                                      {'masks_only': True}) == result
    assert calls == [mosaic_tracking.sam_timeout(frame_count)]


def test_inference_key_deadline_covers_maximum_sam_timeout(clock, tmp_path):
    clock.step = 10
    with cutting.lock_file(tmp_path / 'inference-test.lock'):
        with pytest.raises(cutting.CutError, match='等待同一区域推理超时，请稍后重试') as error:
            with mosaic_inference._key_lease(tmp_path, 'test', lambda: None):
                pytest.fail('occupied inference key was acquired')
    assert error.value.status == 503
    assert clock.now == mosaic_tracking.sam_timeout(300) + 10
