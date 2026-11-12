import copy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest
from exactcut import workflow as w
from exactcut.util import read_json, write_json_atomic
from test_workflow import workspace, imported, complete_asset


def store_module():
    from exactcut import result_store
    return result_store


def test_shards_merge_legacy_and_parallel_writes_without_lost_updates(tmp_path):
    store = store_module()
    write_json_atomic(tmp_path / 'results.json', {'schema_version': 1, 'requests': {'old': {'status': 'failed'}, 'a': {'status': 'failed'}}})
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda rid: store.put(tmp_path, rid, {'status': 'qc_passed'}), ['a', 'b']))
    assert store.read(tmp_path)['requests'] == {'old': {'status': 'failed'}, 'a': {'status': 'qc_passed'}, 'b': {'status': 'qc_passed'}}
    assert read_json(tmp_path / 'results.json')['requests']['a']['status'] == 'failed'


@pytest.mark.parametrize('rid', ['../escape', '/tmp/a', '.', '..', 'a/b', 'a\\b'])
def test_result_paths_reject_traversal(tmp_path, rid):
    with pytest.raises(ValueError): store_module().put(tmp_path, rid, {})


def test_render_writes_preserve_concurrent_executor_heartbeat(tmp_path):
    store = store_module()
    store.patch(tmp_path, 'a', executor_running=True, heartbeat_at='new')
    store.put(tmp_path, 'a', {'status': 'qc_passed', 'heartbeat_at': 'stale'})
    assert store.read(tmp_path)['requests']['a']['heartbeat_at'] == 'new'
    assert store.read(tmp_path)['requests']['a']['executor_running']


def test_execution_is_counted_per_request_and_progress_is_isolated(workspace):
    batch = imported(); path = w.folder(batch['id'])
    with w.execution(path, 'a', interval=60):
        with w.execution(path, 'b', interval=60):
            w.checkpoint(path, 'qc', request_id='b')
            assert w.runtime(path)['executor_running']
        assert w.runtime(path)['executor_running']
        records = w.result_records(path)
        assert records['a']['executor_running'] and not records['b']['executor_running']
        assert records['b']['stage'] == 'qc' and records['a'].get('stage') != 'qc'
    assert not w.runtime(path)['executor_running']


@pytest.mark.parametrize('states,publication,expected', [
    (['failed', 'processing'], {}, 'producing'),
    (['qc_passed', 'processing'], {}, 'producing'),
    (['qc_passed', 'qc_passed'], {'a': {'status': 'fbbm_queued'}}, 'publishing'),
    (['qc_passed', 'qc_passed'], {'a': {'status': 'fbbm_ready'}, 'b': {'status': 'fbbm_ready'}}, 'complete'),
])
def test_batch_status_is_derived_from_all_materials(workspace, states, publication, expected):
    batch = imported(); path = w.folder(batch['id'])
    w.update(path, request_parallel=True)
    for rid, status in zip(['a','b'], states): store_module().put(path / 'production', rid, {'status': status})
    write_json_atomic(path / 'confirmed.json', {'requests': [{'request_id': 'a'}, {'request_id': 'b'}]})
    write_json_atomic(path / 'publications.json', publication)
    assert w.runtime(path)['status'] == expected


@pytest.mark.integration
def test_ready_material_can_be_approved_while_sibling_renders(workspace):
    batch = imported(); path, request, video, record = complete_asset(batch)
    w.update(path, 'producing', request_parallel=True)
    store_module().put(path / 'production', 'sibling', {'status': 'processing'})
    assert 'publish' in w.queue_publish(batch['id'], request['request_id'], record['output_sha256'])


def test_legacy_render_requeues_only_validation_failure_once(workspace, monkeypatch):
    from exactcut import webserver as web
    batch = imported()
    w.start_batch(batch['id'], batch['revision'])
    path = w.folder(batch['id'])
    rid = read_json(path / 'confirmed.json')['requests'][0]['request_id']
    monkeypatch.setattr(web, 'JOBS', workspace / '.web-state/jobs')
    monkeypatch.setattr(web.stage_results, 'verified', lambda *a: {'status': 'failed', 'exit_code': 2})
    job = {'id': 'test-hook-retry', 'batch_id': path.name, 'request_id': rid,
           'phase': 'render', 'attempt': 1, 'status': 'running'}
    store_module().put(path / 'production', rid, {
        'status': 'failed', 'error_type': 'HookEmphasisValidationError', 'error': 'AI 未生成合规重点'})
    store_module().put(path / 'production', 'successful', {'status': 'qc_passed', 'output_sha256': 'keep'})
    web.finish_job(job, 2)
    assert job['status'] == 'queued'
    assert job['hook_validation_retries'] == 1
    assert job['request_id'] == rid
    job.update(status='running', attempt=2)
    web.finish_job(job, 2)
    assert job['status'] == 'failed'
    assert job['hook_validation_retries'] == 1
    assert w.result_records(path)['successful'] == {'status': 'qc_passed', 'output_sha256': 'keep'}


def test_single_request_render_calls_selected_executor(workspace, monkeypatch):
    batch = imported(); w.start_batch(batch['id'], batch['revision']); path = w.folder(batch['id'])
    rid = read_json(path / 'confirmed.json')['requests'][0]['request_id']
    monkeypatch.setattr(w, 'validate_downloaded_video', lambda p: None)
    monkeypatch.setattr('exactcut.media.resolve_episode_video', lambda *a: path / 'fixture.mp4')
    monkeypatch.setattr('exactcut.media.validate_manifest_sources', lambda *a: None)
    execute = Mock(return_value={'requests': {rid: {'status': 'qc_passed'}}})
    monkeypatch.setattr('exactcut.orchestrator.execute_batch', execute)
    assert w.render(batch['id'], rid) == 0
    assert execute.call_args.kwargs['request_ids'] == [rid]


def two_requests(batch):
    w.start_batch(batch['id'], batch['revision']); path = w.folder(batch['id'])
    manifest = read_json(path / 'confirmed.json'); first = manifest['requests'][0]
    second = copy.deepcopy(first); second.update(request_id='R2', output_filename='second.mp4')
    for item in second['timeline']:
        if item['type'] == 'episode': item['episode'] = 'EP2'
    manifest['requests'].append(second); write_json_atomic(path / 'confirmed.json', manifest)
    return path, manifest, first['request_id']


def test_strict_emphasis_failure_remains_typed_in_production_results(workspace, monkeypatch):
    from exactcut import orchestrator as o, hook_emphasis as semantic
    from exactcut.models import BatchManifest
    from test_hook_emphasis import TEXT, RESPONSE, permit_mock
    permit_mock(monkeypatch)
    path, raw, rid = two_requests(imported())
    manifest = BatchManifest.model_validate(raw)
    monkeypatch.setattr(o, 'resolve_full_episodes', lambda m, p: m)
    monkeypatch.setattr(o, 'validate_manifest_sources', lambda *a: None)
    monkeypatch.setattr(o, '_request_fingerprint', lambda *a: 'fixture')
    monkeypatch.setattr(semantic, '_request_analysis', lambda *a: {
        **RESPONSE, 'highlights': [{'phrase': 'rewritten queen', 'occurrence': 1, 'role': 'identity'}]})
    def render(project, request, timeline, staged, config, **options):
        options['progress']('ai_emphasis')
        return semantic.resolve_emphasis(TEXT, workspace / 'cache')
    monkeypatch.setattr(o, 'render_request', render)
    results = o.execute_batch(workspace, manifest, {}, request_ids=[rid], output_dir=path / 'production')
    record = results['requests'][rid]
    assert record['status'] == 'failed'
    assert record['error_type'] == 'HookEmphasisValidationError'
    assert w.result_records(path)[rid]['error_type'] == 'HookEmphasisValidationError'
    assert len(list((workspace / 'cache/rejections').glob('*.json'))) == 2
    assert 'R2' not in results['requests']


@pytest.mark.integration
def test_selected_orchestrator_isolates_sources_results_and_resolved_manifest(workspace, monkeypatch):
    from exactcut import orchestrator as o
    from exactcut.models import BatchManifest
    path, raw, rid = two_requests(imported())
    manifest = BatchManifest.model_validate(raw)
    checked = []
    monkeypatch.setattr(o, 'resolve_full_episodes', lambda m, p: m)
    monkeypatch.setattr(o, 'validate_manifest_sources', lambda m, p: checked.append([r.request_id for r in m.requests]))
    monkeypatch.setattr(o, '_request_fingerprint', lambda *a: 'fixture')
    def render(project, request, timeline, staged, config, **kw):
        staged.write_bytes(b'video'); return {'expected_duration_ms': 1000, 'stage_seconds': {}}
    monkeypatch.setattr(o, 'render_request', render)
    monkeypatch.setattr(o, 'inspect_output', lambda *a: {'allowed': True, 'actual_duration_ms': 1000})
    monkeypatch.setattr(o, 'make_preview', lambda *a: {'fixture': 'generated'})
    uploaded = []
    def upload(batch_dir, request, record):
        assert record['preview']['fixture'] == 'generated'
        assert (batch_dir / 'videos' / request.output_filename).is_file()
        uploaded.append(request.request_id)
        record['preview']['oss'] = {'fixture': 'uploaded'}
    monkeypatch.setattr(o, 'upload_generated_preview', upload)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda r: o.execute_batch(workspace, manifest, {}, request_ids=[r], output_dir=path / 'production'), [rid, 'R2']))
    assert checked == [[rid], ['R2']] or checked == [['R2'], [rid]]
    assert set(uploaded) == {rid, 'R2'}
    assert all(r['preview']['oss'] == {'fixture': 'uploaded'} for r in w.result_records(path).values())
    assert set(w.result_records(path)) == {rid, 'R2'}
    assert all(r['status'] == 'qc_passed' for r in w.result_records(path).values())
    assert not (path / 'production/results.json').exists()
    assert len(list((path / 'production').glob('resolved-*.manifest.json'))) == 2


@pytest.mark.integration
def test_render_evidence_and_disk_budget_include_only_selected_request(workspace, monkeypatch):
    from exactcut import stage_results as e, storage
    path, manifest, rid = two_requests(imported())
    source = workspace / 'episode.mp4'; source.write_bytes(b'episode')
    monkeypatch.setattr('exactcut.media.resolve_episode_video', lambda p, ep: source if ep == 'EP1' else (_ for _ in ()).throw(ValueError('sibling source unavailable')))
    production = path / 'production/videos'; production.mkdir(parents=True)
    (production / manifest['requests'][0]['output_filename']).write_bytes(b'a')
    sibling = production / 'second.mp4'; sibling.write_bytes(b'b')
    job = {'id': 'a' * 32, 'batch_id': path.name, 'phase': 'render', 'request_id': rid}
    assert set(e.source_paths(job, w.BATCHES, workspace)) == {'EP1'}
    assert [row['path'] for row in e.artifacts(job, w.BATCHES, workspace)] == [str(production / manifest['requests'][0]['output_filename'])]
    before = e.fingerprint(job, w.BATCHES, workspace)
    sibling.write_bytes(b'changed')
    assert e.fingerprint(job, w.BATCHES, workspace) == before
    monkeypatch.setattr('exactcut.media.resolve_full_episodes', lambda m, p: m)
    monkeypatch.setattr(storage, 'render_budget', lambda *a: 100)
    assert sum(storage.job_budget(job, w.BATCHES, workspace).values()) == 200


def test_single_request_render_does_not_inherit_sibling_failure(workspace, monkeypatch):
    from exactcut import orchestrator
    path, manifest, rid = two_requests(imported())
    monkeypatch.setattr(w, 'validate_downloaded_video', lambda p: None)
    monkeypatch.setattr('exactcut.media.resolve_episode_video', lambda *a: path / 'fixture.mp4')
    monkeypatch.setattr('exactcut.media.validate_manifest_sources', lambda *a: None)
    monkeypatch.setattr(orchestrator, 'execute_batch', lambda *a, **kw: {'requests': {rid: {'status': 'qc_passed'}, 'R2': {'status': 'failed'}}})
    assert w.render(path.name, rid) == 0


def test_batch_executor_observation_does_not_use_finished_fetch_timestamp(workspace):
    from exactcut.progress import observation
    batch = imported(); path = w.folder(batch['id'])
    w.update(path, execution_ended_at='2020-01-01T00:00:00+00:00')
    with w.execution(path, 'a', interval=60):
        state = w.runtime(path)
        assert state['execution_ended_at'] is None
        assert observation(state)['heartbeat_seconds'] is not None


@pytest.mark.integration
def test_upload_progress_ignores_previous_render_completion(workspace):
    from exactcut.progress import observation
    batch = imported(); path, request, video, record = complete_asset(batch)
    store_module().patch(path / 'production', request['request_id'], completed_at='2020-01-01T00:00:00+00:00')
    with w.execution(path, request['request_id'], interval=60):
        w.checkpoint(path, 'upload', request_id=request['request_id'])
        running = w.result_records(path)[request['request_id']]
        assert observation(running)['heartbeat_seconds'] is not None
        assert running['completed_at'] == '2020-01-01T00:00:00+00:00'


def test_production_complete_is_aggregated_not_last_material_wins(workspace):
    path, manifest, rid = two_requests(imported())
    w.update(path, request_parallel=True, production_complete=True)
    store_module().put(path / 'production', rid, {'status': 'qc_passed'})
    store_module().put(path / 'production', 'R2', {'status': 'processing'})
    assert not w.runtime(path)['production_complete']
    store_module().put(path / 'production', 'R2', {'status': 'qc_passed'})
    assert w.runtime(path)['production_complete']


def test_empty_request_selection_retains_legacy_all_requests(workspace, monkeypatch):
    from exactcut import orchestrator as o
    from exactcut.models import BatchManifest
    path, raw, rid = two_requests(imported())
    monkeypatch.setattr(o, 'resolve_full_episodes', lambda m, p: m)
    monkeypatch.setattr(o, 'validate_manifest_sources', lambda *a: None)
    monkeypatch.setattr(o, '_request_fingerprint', lambda *a: 'fixture')
    def fail(*a, **kw): raise RuntimeError('fixture render unavailable')
    monkeypatch.setattr(o, 'render_request', fail)
    result = o.execute_batch(workspace, BatchManifest.model_validate(raw), {}, request_ids=[], output_dir=path / 'production')
    assert set(result['requests']) == {rid, 'R2'}
    assert (path / 'production/results.json').exists()


def test_partial_batch_cannot_restart_while_sibling_executor_is_live(workspace):
    path, manifest, rid = two_requests(imported())
    w.update(path, request_parallel=True)
    store_module().put(path / 'production', rid, {'status': 'failed'})
    with w.execution(path, 'R2', interval=60):
        with pytest.raises(ValueError, match='正在执行'): w.start_batch(path.name, 1)


def test_partial_batch_cannot_restart_while_sibling_render_queued(workspace):
    path, manifest, rid = two_requests(imported())
    w.update(path, request_parallel=True)
    store_module().put(path / 'production', rid, {'status': 'failed'})
    write_json_atomic(workspace / '.web-state/jobs/pending.json', {'batch_id': path.name, 'phase': 'render', 'request_id': 'R2', 'status': 'queued', 'not_before': 9999999999})
    with pytest.raises(ValueError, match='正在执行'): w.start_batch(path.name, 1)


def test_finished_material_runtime_uses_matching_execution_end(workspace):
    batch = imported(); path = w.folder(batch['id'])
    w.update(path, execution_started_at='2020-01-01T00:00:00+00:00', execution_ended_at='2020-01-01T00:00:01+00:00')
    with w.execution(path, 'a', interval=60): w.checkpoint(path, 'fbbm', request_id='a')
    latest = w.result_records(path)['a']; state = w.runtime(path)
    assert state['execution_started_at'] == latest['execution_started_at']
    assert state['execution_ended_at'] == latest['execution_ended_at']
    assert state['execution_ended_at'] >= state['execution_started_at']


def test_newer_finished_batch_execution_is_not_replaced_by_old_shard(workspace):
    batch = imported(); path = w.folder(batch['id'])
    store_module().patch(path / 'production', 'a', executor_running=False, stage='base_cut',
                         execution_started_at='2026-10-09T00:00:00+00:00', execution_ended_at='2026-10-09T00:00:10+00:00')
    w.update(path, stage='download', executor_running=False,
             execution_started_at='2026-10-10T00:00:00+00:00', execution_ended_at='2026-10-10T00:00:10+00:00')
    state = w.runtime(path)
    assert state['stage'] == 'download'
    assert state['execution_started_at'] == '2026-10-10T00:00:00+00:00'
    assert state['execution_ended_at'] == '2026-10-10T00:00:10+00:00'


def test_inactive_shard_cannot_freeze_legacy_rerender_timestamps(tmp_path):
    store = store_module()
    store.patch(tmp_path, 'a', executor_running=False, execution_started_at='old', execution_ended_at='old-end')
    store.put(tmp_path, 'a', {'status': 'qc_passed', 'started_at': 'new', 'execution_ended_at': 'new-end'})
    result = store.read(tmp_path)['requests']['a']
    assert result['execution_ended_at'] == 'new-end'
    assert 'execution_started_at' not in result
