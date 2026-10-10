"""Batch summaries use durable shards, independent of render completion order."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from exactcut import result_store, workflow as w
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace, imported
from test_intra_project_results import two_requests


@pytest.mark.parametrize('stale_first', [0, 1])
def test_parallel_render_completion_ignores_stale_local_result(workspace, monkeypatch, stale_first):
    path, manifest, _ = two_requests(imported())
    request_ids = [r['request_id'] for r in manifest['requests']]
    stale, other = request_ids[stale_first], request_ids[1 - stale_first]
    captured = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(w, 'validate_downloaded_video', lambda p: None)
    monkeypatch.setattr('exactcut.media.resolve_episode_video', lambda *a: path / 'fixture.mp4')
    monkeypatch.setattr('exactcut.media.validate_manifest_sources', lambda *a: None)

    def execute(project, selected, config, *, request_ids, **kw):
        rid = request_ids[0]
        record = {'status': 'qc_passed'}
        result_store.put(path / 'production', rid, record)
        # Reproduce an executor retaining a snapshot that contains only itself.
        local = {'requests': {rid: copy.deepcopy(record)}}
        if rid == stale:
            captured.set()
            assert release.wait(5), 'test did not release the stale executor'
        return local

    monkeypatch.setattr('exactcut.orchestrator.execute_batch', execute)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(w.render, path.name, stale)
        try:
            assert captured.wait(5), 'first render did not reach its snapshot'
            assert pool.submit(w.render, path.name, other).result(timeout=5) == 0
            # Both shards are QC-passed, but one render has not exited yet.
            intermediate = read_json(path / 'runtime.json')
            observed = w.runtime(path)
        finally:
            release.set()
        assert first.result(timeout=5) == 0

    persisted = read_json(path / 'runtime.json')
    assert persisted['status'] == 'review'
    assert persisted['production_complete'] is True
    assert persisted['message'] == '制作结束，2 条可预览，0 条失败'
    assert w.runtime(path)['message'] == persisted['message']
    assert intermediate['status'] == observed['status'] == 'producing'
    assert intermediate['production_complete'] is False


@pytest.mark.parametrize('states,expected,complete,message', [
    (['qc_passed', 'processing'], 'producing', False, None),
    (['failed', 'processing'], 'producing', False, None),
    (['qc_passed', None], 'producing', False, None),
    (['qc_passed', 'waiting_resource'], 'producing', False, None),
    (['qc_passed', 'qc_passed'], 'review', True, '制作结束，2 条可预览，0 条失败'),
    (['qc_passed', 'failed'], 'partial', True, '制作结束，1 条可预览，1 条失败'),
    (['failed', 'failed'], 'partial', True, '制作结束，0 条可预览，2 条失败'),
])
def test_persisted_summary_requires_all_confirmed_requests_terminal(workspace, states, expected, complete, message):
    path, manifest, _ = two_requests(imported())
    for request, status in zip(manifest['requests'], states):
        if status: result_store.put(path / 'production', request['request_id'], {'status': status})
    w.update(path, 'review', 'local stale message', request_parallel=True, production_complete=True)
    persisted = read_json(path / 'runtime.json')
    assert persisted['status'] == expected
    assert persisted['production_complete'] is complete
    assert w.runtime(path)['status'] == expected
    if message: assert persisted['message'] == message
    # An unrelated leftover shard must never inflate counts.
    result_store.put(path / 'production', 'not-in-confirmed', {'status': 'qc_passed'})
    w.update(path, 'review', 'another stale message')
    assert read_json(path / 'runtime.json')['message'] == persisted['message']


def test_completed_shards_correct_old_summary_on_read_and_next_update(workspace):
    path, manifest, _ = two_requests(imported())
    for request in manifest['requests']:
        result_store.put(path / 'production', request['request_id'], {'status': 'qc_passed'})
    raw = read_json(path / 'runtime.json')
    raw.update(request_parallel=True, status='review', message='制作结束，1 条可预览，0 条失败', production_complete=True)
    write_json_atomic(path / 'runtime.json', raw)
    assert w.runtime(path)['message'] == '制作结束，2 条可预览，0 条失败'
    w.update(path, 'producing', 'late sibling preparation message')
    assert read_json(path / 'runtime.json')['message'] == '制作结束，2 条可预览，0 条失败'
    assert read_json(path / 'runtime.json')['status'] == 'review'


def test_upload_executor_does_not_make_finished_production_incomplete(workspace):
    path, manifest, rid = two_requests(imported())
    for request in manifest['requests']:
        result_store.put(path / 'production', request['request_id'], {'status': 'qc_passed'})
    write_json_atomic(path / 'publications.json', {rid: {'status': 'uploading'}})
    w.update(path, request_parallel=True)
    with w.execution(path, rid, interval=60):
        w.checkpoint(path, 'upload', request_id=rid)
        w.update(path)
        state = read_json(path / 'runtime.json')
        assert state['production_complete'] is True
        assert state['status'] == 'publishing'


def test_durable_render_jobs_keep_terminal_shards_producing_until_reaped(workspace):
    path, manifest, rid = two_requests(imported())
    for request in manifest['requests']:
        result_store.put(path / 'production', request['request_id'], {'status': 'qc_passed'})
    job_file = workspace / '.web-state/jobs/render.json'
    job = {'id': 'a' * 32, 'batch_id': path.name, 'request_id': rid, 'phase': 'render', 'status': 'running'}
    write_json_atomic(job_file, job)
    w.update(path, request_parallel=True)
    assert read_json(path / 'runtime.json')['status'] == 'producing'
    assert not w.runtime(path)['production_complete']
    job['status'] = 'queued'; job['not_before'] = 9999999999
    write_json_atomic(job_file, job); w.update(path)
    assert not read_json(path / 'runtime.json')['production_complete']
    job['status'] = 'completed'
    write_json_atomic(job_file, job); w.update(path)
    assert read_json(path / 'runtime.json')['status'] == 'review'


def test_six_confirmed_qc_shards_always_count_six(workspace):
    path, manifest, _ = two_requests(imported())
    for index in range(4):
        request = copy.deepcopy(manifest['requests'][0])
        request.update(request_id=f'additional-{index}', output_filename=f'additional-{index}.mp4')
        manifest['requests'].append(request)
    write_json_atomic(path / 'confirmed.json', manifest)
    for request in reversed(manifest['requests']):
        result_store.put(path / 'production', request['request_id'], {'status': 'qc_passed'})
    w.update(path, 'review', '制作结束，5 条可预览，0 条失败', request_parallel=True, production_complete=True)
    assert read_json(path / 'runtime.json')['message'] == '制作结束，6 条可预览，0 条失败'


def test_old_batch_render_phase_does_not_misclassify_live_upload(workspace):
    path, manifest, rid = two_requests(imported())
    for request in manifest['requests']:
        result_store.put(path / 'production', request['request_id'], {'status': 'qc_passed'})
    write_json_atomic(path / 'publications.json', {rid: {'status': 'uploading'}})
    w.update(path, request_parallel=True, executor_running=False, execution_phase='render')
    with w.execution(path, rid, interval=60, phase='upload'):
        w.update(path)
        assert read_json(path / 'runtime.json')['production_complete'] is True
        assert w.runtime(path)['production_complete'] is True
        assert w.runtime(path)['status'] == 'publishing'
        assert w.result_records(path)[rid]['execution_phase'] == 'upload'
