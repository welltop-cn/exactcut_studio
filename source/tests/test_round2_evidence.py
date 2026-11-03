"""Transient evidence failures retry; bounded readers really release on timeout."""
from functools import partial
import os
import time
import pytest
from exactcut import stage_results as evidence, workflow as w, webserver as web
from exactcut.util import read_json
from test_concurrency import runner, queued, close_logs
from test_round1_evidence import result_fixture


def failing_once(file):
    if not file.exists():
        file.write_text('failed')
        raise ValueError('temporary read failure')
    return 'recomputed'


def gated_read(gate):
    while not gate.exists(): time.sleep(.01)
    return 'released'


def immediate(): return 'new slot'


def await_value(key, operation, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        try: return evidence.background(key, operation)
        except evidence.EvidencePending:
            assert time.monotonic() < deadline
            time.sleep(.01)


def test_failed_future_is_not_reusable_cache(tmp_path):
    key = ('round2-failure', str(tmp_path))
    operation = partial(failing_once, tmp_path / 'once')
    with pytest.raises(ValueError, match='temporary'): await_value(key, operation)
    assert await_value(key, operation) == 'recomputed'


@pytest.mark.parametrize('error', [ValueError, OSError])
def test_completed_fetch_retries_transient_read_next_tick(runner, monkeypatch, error):
    job, path = result_fixture(runner, 'fetch')
    evidence.complete(job, web.JOBS, w.BATCHES, w.ROOT, 0)
    result_file = evidence.result_path(job, web.JOBS)
    original = evidence.read_json
    failing = True
    def flaky(file):
        if file == result_file and failing: raise error('temporary filesystem read error')
        return original(file)
    monkeypatch.setattr(evidence, 'read_json', flaky)
    web.recover_jobs(scheduler=True)
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'completed'
    assert not [j for j in web.stored_jobs() if j.get('parent_id') == job['id']]
    failing = False
    web.recover_jobs(scheduler=True)
    assert len([j for j in web.stored_jobs() if j.get('parent_id') == job['id'] and j['phase'] == 'render']) == 1


def test_two_blocked_readers_timeout_and_reclaim_real_slots(runner, monkeypatch):
    monkeypatch.setattr(evidence, 'EVIDENCE_TIMEOUT_SECONDS', 1, raising=False)
    gate = runner / 'release-readers'
    prefix = ('round2-timeout', str(runner))
    operation = partial(gated_read, gate)
    reader_pids = []
    try:
        for index in range(2):
            with pytest.raises(evidence.EvidencePending): evidence.background((*prefix, index), operation)
            reader_pids.append(evidence._PENDING[(*prefix, index)].process.pid)
        queued('c', 'upload', owner='charlie')
        owned = {}
        try:
            web.worker_tick([], owned)
            assert 'c' * 32 in owned
        finally: close_logs(owned)
        with pytest.raises(evidence.EvidencePending): evidence.background((*prefix, 3), immediate)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if evidence.background((*prefix, 3), immediate) == 'new slot': break
            except evidence.EvidencePending: pass
            time.sleep(.02)
        else: pytest.fail('timed out readers still hold the two execution slots')
        assert not gate.exists(), 'the blocked readers were not released by the fixture'
        for pid in reader_pids:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally: gate.write_text('cleanup')


def test_late_timeout_callback_after_close_is_safe():
    read = evidence.EvidenceRead(immediate)
    deadline = time.monotonic() + 3
    try:
        while not read.done():
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert read.result() == 'new slot'
    finally: read.close()
    read.expire()  # Timer.cancel can race with an already entering callback.


@pytest.mark.integration
def test_transient_source_stat_defers_fingerprint_instead_of_failing(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'; source.write_bytes(b'original')
    monkeypatch.setattr(evidence, 'source_paths', lambda *args: {'01': source})
    def failing_stat(path): raise OSError('temporary stat error')
    monkeypatch.setattr(evidence, 'file_identity', failing_stat)
    with pytest.raises(evidence.EvidencePending):
        evidence.scheduler_fingerprint({'phase': 'render'}, tmp_path / 'batches', tmp_path)


def test_reader_timeout_survives_parent_timer_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, 'EVIDENCE_TIMEOUT_SECONDS', .8)
    gate = tmp_path / 'never-released'
    read = evidence.EvidenceRead(partial(gated_read, gate))
    read.timer.cancel()  # Models the scheduler dying before its timer can fire.
    deadline = time.monotonic() + 3
    try:
        while not read.done():
            assert time.monotonic() < deadline, 'orphaned reader has no independent deadline'
            time.sleep(.02)
        with pytest.raises(OSError): read.result()
        assert not gate.exists()
    finally: read.close()
