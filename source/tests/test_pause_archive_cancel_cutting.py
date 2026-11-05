"""Pause is refused while cut/mosaic runs; archive cancels those tasks. Neither is frozen by completed results."""
import threading
import time
import uuid

import pytest

from exactcut import accounts, batch_control, cutting, cutting_access, material_versions, workflow
from exactcut import mosaic_queue as queue, mosaic_render as render, processing_cancel
from exactcut.util import read_json, write_json_atomic, file_sha256
from test_cutting_api import ready, call, prepared
from test_phase2_preview_api import fixture, serve
from test_mosaic_queue_integration import second_asset
from interactive_test_support import interactive_dispatcher

pytestmark = pytest.mark.integration

MOSAIC = {'targets': [{'motion': 'fixed', 'start': 1, 'end': 5, 'seed_frame': 1, 'x': 0, 'y': 0, 'w': 40, 'h': 40}],
          'ranges': [], 'reset_at': ''}
CUT = {'targets': [], 'ranges': [[1, 3]], 'reset_at': ''}
KINDS = {'cut': CUT, 'mosaic': MOSAIC}


def submit(app, bid, rid, sha, body):
    root = f'/api/batches/{bid}/mosaics'
    code, session = call(app, 'POST', root + '/open', {'request_id': rid, 'source_sha256': sha})
    assert code in {200, 202}, session
    session = prepared(app, root, session)
    code, job = call(app, 'POST', f"{root}/{session['id']}/render", body)
    assert code == 202, job
    return session['id'], job


def job_file(jid):
    item = next(i for i in queue.load()['tasks'] if i['id'] == jid)
    return queue.job_path(item)


def job(jid):
    return read_json(job_file(jid))


def wait_for(jid, statuses, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = job(jid)
        if current['status'] in statuses:
            return current
        time.sleep(.02)
    pytest.fail(f'task stayed {job(jid)["status"]}')


def enroll(batch, managed):
    if managed:
        material_versions.enroll(batch)


def production_running(batch):
    """Scenario: a legacy production is also running (completed results alone are not pausable work)."""
    if not material_versions.managed(batch):
        workflow.update(batch, 'producing', 'fixture production running')


def production_done(batch):
    """Legacy edits wait for production to finish (existing rule, unchanged)."""
    if not material_versions.managed(batch):
        workflow.update(batch, 'review', 'fixture production done')


def control(batch, action):
    if material_versions.managed(batch):
        state = material_versions.read(batch)
        return material_versions.control(batch, action, {'revision': state['revision'], 'mutation_id': uuid.uuid4().hex})
    return batch_control.change(batch, action)


def flags(batch):
    if material_versions.managed(batch):
        value = material_versions.read(batch)['control']
    else:
        value = batch_control.read(batch)
    return bool(value.get('paused')), bool(value.get('archived'))


def frozen(batch):
    with pytest.raises(cutting.CutError) as exc:
        cutting_access.require_unfrozen(batch)
    assert exc.value.status == 409


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('kind', ['cut', 'mosaic'])
@pytest.mark.parametrize('status', ['queued', 'cancelling'])
def test_pause_is_refused_while_cut_task_is_queued(ready, managed, kind, status):
    bid, rid, sha, source = ready; batch = workflow.folder(bid); enroll(batch, managed)
    app = serve()
    try:
        _, submitted = submit(app, bid, rid, sha, KINDS[kind])
        write_json_atomic(job_file(submitted['id']), {**job(submitted['id']), 'status': status})
        assert workflow.detail(bid)['runtime']['cutting_active']  # UI hides 暂停执行
        production_running(batch)
        with pytest.raises(ValueError, match=batch_control.PAUSE_CUTTING_ERROR):
            control(batch, 'pause')
        assert flags(batch) == (False, False)
        assert job(submitted['id'])['status'] == status and not job(submitted['id']).get('cancel_requested')
        assert file_sha256(source) == sha
    finally:
        app.shutdown(); app.server_close()


@pytest.mark.parametrize('managed', [False, True])
def test_pause_is_refused_while_mosaic_runs_and_archive_stops_it(ready, managed, monkeypatch):
    bid, rid, sha, source = ready; batch = workflow.folder(bid); enroll(batch, managed)
    accounts.initialize(workflow.BATCHES.parent / 'accounts.sqlite3', 'pause-cancel-password')
    entered, observed = threading.Event(), threading.Event()

    def running(*args, **kwargs):
        entered.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                processing_cancel.check()
            except processing_cancel.Cancelled:
                observed.set(); raise
            time.sleep(.01)
        raise AssertionError('archive never reached the running worker')
    monkeypatch.setattr(render, 'process_video', running)
    app = serve(); dispatcher = queue.Dispatcher(.01)
    try:
        sid, submitted = submit(app, bid, rid, sha, MOSAIC)
        dispatcher.start()
        assert entered.wait(10)
        assert job(submitted['id'])['status'] == 'running'
        with pytest.raises(ValueError, match=batch_control.PAUSE_CUTTING_ERROR):
            control(batch, 'pause')
        assert flags(batch) == (False, False) and not job(submitted['id']).get('cancel_requested')
        control(batch, 'archive')
        assert job(submitted['id'])['cancel_requested']
        assert wait_for(submitted['id'], {'cancelled', 'failed', 'completed'})['status'] == 'cancelled'
        assert observed.is_set()
        assert queue.blocked(bid) is None
        assert file_sha256(source) == sha
    finally:
        dispatcher.stop(); app.shutdown(); app.server_close()


@pytest.mark.parametrize('managed', [False, True])
def test_pause_keeps_completed_unconfirmed_result_and_resume_is_not_frozen(ready, managed):
    bid, rid, sha, source = ready; batch = workflow.folder(bid); enroll(batch, managed)
    accounts.initialize(workflow.BATCHES.parent / 'accounts.sqlite3', 'pause-cancel-password')
    app = serve(); dispatcher = queue.Dispatcher(.01)
    try:
        sid, submitted = submit(app, bid, rid, sha, MOSAIC)
        dispatcher.start()
        done = wait_for(submitted['id'], {'completed', 'failed', 'cancelled'}, timeout=60)
        assert done['status'] == 'completed', done
        dispatcher.stop()
        output = job_file(submitted['id']).parent / f"render-{submitted['id']}" / 'mosaic.mp4'
        assert output.is_file(); digest = file_sha256(output)
        frozen(batch)
        production_running(batch)
        control(batch, 'pause')
        assert workflow.detail(bid)['runtime']['events'][-1]['message'] == batch_control.PAUSE_MESSAGE
        kept = job(submitted['id'])
        assert kept == done and not kept.get('cancel_requested') and not kept.get('discarded_at')
        assert file_sha256(output) == digest
        frozen(batch)  # still awaiting confirm/discard
        control(batch, 'resume')  # not held back by the freeze
        assert flags(batch) == (False, False)
        assert job(submitted['id']) == done
        if managed:
            # Delete keeps its existing freeze check.
            with pytest.raises(cutting.CutError):
                control(batch, 'delete')
        production_done(batch)
        code, _ = call(app, 'POST', f'/api/batches/{bid}/mosaics/{sid}/discard-result',
                       {'job_id': submitted['id'], 'reset_at': ''})
        assert code == 200
        assert file_sha256(source) == sha
    finally:
        dispatcher.stop(); app.shutdown(); app.server_close()


@pytest.mark.parametrize('managed', [False, True])
def test_archive_cancels_tasks_keeps_completed_result_and_unarchive_is_not_frozen(ready, managed):
    bid, rid, sha, source = ready; rid2 = second_asset(bid, source, sha)
    batch = workflow.folder(bid); enroll(batch, managed)
    accounts.initialize(workflow.BATCHES.parent / 'accounts.sqlite3', 'pause-cancel-password')
    app = serve(); dispatcher = queue.Dispatcher(.01)
    try:
        _, completed = submit(app, bid, rid, sha, MOSAIC)
        dispatcher.start()
        completed = wait_for(completed['id'], {'completed', 'failed', 'cancelled'}, timeout=60)
        assert completed['status'] == 'completed', completed
        dispatcher.stop()
        _, queued = submit(app, bid, rid2, sha, CUT)
        assert job(queued['id'])['status'] == 'queued'
        production_running(batch)
        control(batch, 'archive')
        paused, archived = flags(batch)
        assert archived and paused
        assert workflow.detail(bid)['runtime']['events'][-1]['message'] == batch_control.ARCHIVE_MESSAGE
        assert job(queued['id'])['status'] == 'cancelled'
        assert job(completed['id']) == completed
        frozen(batch)
        control(batch, 'unarchive')
        assert flags(batch) == (True, False)
        control(batch, 'resume')
        assert flags(batch) == (False, False)
        assert job(queued['id'])['status'] == 'cancelled'
        assert queue.claim() is None
    finally:
        dispatcher.stop(); app.shutdown(); app.server_close()
