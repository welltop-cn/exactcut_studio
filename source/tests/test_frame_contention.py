"""Concurrent frame requests reuse windows instead of failing immediately."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import pytest
from exactcut import cutting, mosaic_session

@pytest.mark.parametrize('module', [cutting, mosaic_session])
def test_overlapping_requests_wait_and_reuse_window(tmp_path, monkeypatch, module):
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(module, 'read_session', lambda *a: {'metadata': {
        'frame_count': 100, 'fps_num': 30, 'fps_den': 1}})
    monkeypatch.setattr(module, 'session_path', lambda *a: tmp_path)
    monkeypatch.setattr(module, 'lock_root', lambda: tmp_path/'locks')
    def run(command, timeout, **kwargs):
        calls.append(command)
        entered.set()
        assert release.wait(3)
        start = int(command[command.index('-start_number')+1])
        for n in range(start, start+32):
            Path(command[-1].replace('%d', str(n))).write_bytes(b'frame')
    monkeypatch.setattr(module, 'run', run)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(module.frame_file, 'batch', 'session', 10)
        assert entered.wait(2)
        second = pool.submit(module.frame_file, 'batch', 'session', 11)
        try:
            time.sleep(.1)
            assert not second.done(), 'overlapping frame request rejected instead of waiting'
        finally:
            release.set()
        assert first.result().read_bytes() == b'frame'
        assert second.result().read_bytes() == b'frame'
    assert len(calls) == 1

def test_frame_wait_has_deadline_and_specific_message(tmp_path):
    lock = tmp_path/'frame.lock'
    with cutting.lock_file(lock):
        start = time.monotonic()
        with pytest.raises(cutting.CutError, match='准确帧') as error:
            with cutting.frame_lease(lock, timeout=.1):
                pytest.fail('acquired occupied frame lock')
        assert error.value.status == 429
        assert .08 <= time.monotonic()-start < .8
    with cutting.frame_lease(lock, timeout=.1):
        pass

@pytest.mark.parametrize('module', [cutting, mosaic_session])
def test_cached_frame_does_not_wait_for_busy_extractor(tmp_path, monkeypatch, module):
    monkeypatch.setattr(module, 'read_session', lambda *a: {'metadata': {'frame_count': 100}})
    monkeypatch.setattr(module, 'session_path', lambda *a: tmp_path)
    monkeypatch.setattr(module, 'lock_root', lambda: tmp_path/'locks')
    (tmp_path/'frames').mkdir()
    (tmp_path/'frames/10.jpg').write_bytes(b'cached')
    with cutting.lock_file(tmp_path/'locks/frame.lock'):
        assert module.frame_file('batch', 'session', 10).read_bytes() == b'cached'


def test_frame_lease_releases_after_extraction_error(tmp_path):
    lock = tmp_path/'frame.lock'
    with pytest.raises(RuntimeError):
        with cutting.frame_lease(lock):
            raise RuntimeError('extract failed')
    with cutting.frame_lease(lock, timeout=0):
        pass
