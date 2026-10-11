"""Managed lifecycle invariants use isolated local fixtures; no external writes."""
import copy
import importlib.util
import uuid
from pathlib import Path

import pytest

from exactcut import workflow as w, result_store
from exactcut.util import read_json, write_json_atomic, file_sha256
from test_workflow import workspace, imported, complete_asset


def module():
    assert importlib.util.find_spec('exactcut.material_versions'), 'Managed lifecycle module is missing'
    from exactcut import material_versions
    return material_versions


@pytest.fixture(autouse=True)
def enable_fixture_delivery(monkeypatch):
    from exactcut import material_versions
    monkeypatch.setattr(material_versions, 'delivery_enabled', lambda: True)


def token():
    return uuid.uuid4().hex


def enrolled(*, media=False):
    m = module()
    item = imported()
    if media:
        path, request, video, record = complete_asset(item)
    else:
        path = w.folder(item['id'])
    m.enroll(path)
    return m, path, m.read(path)


def test_full_draft_cas_stable_ids_delete_restore_idempotency(workspace):
    m, path, state = enrolled()
    first = state['draft']['rows'][0]['request_id']
    draft = copy.deepcopy(state['draft'])
    draft['rows'].append({**copy.deepcopy(draft['rows'][0]), 'request_id': None, 'seq': '2'})
    mutation = {'revision': state['revision'], 'mutation_id': token(), 'draft': draft}
    saved = m.save_draft(path, mutation)
    assert saved == m.save_draft(path, mutation)
    second = saved['draft']['rows'][1]['request_id']
    assert second and second != first
    with pytest.raises(ValueError, match='幂等'):
        m.save_draft(path, {**mutation, 'draft': state['draft']})
    with pytest.raises(ValueError, match='更新'):
        m.save_draft(path, {**mutation, 'mutation_id': token()})
    reordered = copy.deepcopy(saved['draft']); reordered['rows'].reverse()
    saved = m.save_draft(path, {'revision': saved['revision'], 'mutation_id': token(), 'draft': reordered})
    assert [r['request_id'] for r in saved['draft']['rows']] == [second, first]
    saved = m.save_draft(path, {'revision': saved['revision'], 'mutation_id': token(), 'draft': {**saved['draft'], 'rows': [saved['draft']['rows'][0]]}})
    assert saved['assets'][first]['deleted_at']
    restored = m.restore(path, first, {'revision': saved['revision'], 'mutation_id': token()})
    assert restored['assets'][first]['deleted_at'] is None
    assert set(r['request_id'] for r in restored['draft']['rows']) == {first, second}


def test_registry_corruption_never_falls_back_and_old_writers_blocked(workspace):
    m, path, state = enrolled()
    with pytest.raises(ValueError, match='纳管'):
        result_store.put(path / 'production', state['draft']['rows'][0]['request_id'], {})
    (path / 'media-state.json').write_text('{}')
    with pytest.raises(ValueError, match='纳管'):
        m.read(path)
    with pytest.raises(ValueError, match='纳管'):
        w.delete_batch(path.name, path.name)


def test_b2_internal_name_alias_does_not_renumber_parent_or_allow_third_party(workspace):
    m, path, state = enrolled()
    confirmed = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    m.recover_outbox()
    candidate = next(iter(confirmed['outbox'].values()))
    child = w.BATCHES / candidate['child_id']
    assert read_json(child / 'draft.json')['media_internal']['parent_batch_id'] == path.name
    assert read_json(child / 'confirmed.json')['requests'][0]['output_filename'] == candidate['manifest']['requests'][0]['output_filename']
    draft = copy.deepcopy(state['draft'])
    assert not w.allocate_sequences(draft, path.name)
    other = imported()
    assert other['draft']['rows'][0]['seq'] != state['draft']['rows'][0]['seq']
    assert child.name not in {item['id'] for item in w.summaries()}


def test_b1_unknown_internal_delivery_missing_job_never_reenqueues(workspace, monkeypatch):
    m, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    version = state['assets'][rid]['current_version']
    state = m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid,
                                   'version_id': version, 'sha256': state['versions'][version]['record']['output_sha256']})
    m.recover_outbox()
    event = next(e for e in state['outbox'].values() if e['kind'] == 'delivery')
    child = w.BATCHES / event['child_id']
    write_json_atomic(child / 'publications.json', {rid: {'status': 'unverified', 'approved_sha256': event['sha256'], 'intent': {'status': 'pending'}}})
    for file in (w.ROOT / '.web-state/jobs').glob('*.json'): file.unlink()
    m.recover_outbox()
    w.recover_verification_queue([])
    m.recover_outbox()
    assert not list((w.ROOT / '.web-state/jobs').glob('*.json'))
    latest = m.read(path)
    assert latest['deliveries'][event['delivery_id']]['status'] == 'unverified'
    with pytest.raises(ValueError, match='交付'):
        m.queue_delivery(path, {'revision': latest['revision'], 'mutation_id': token(), 'request_id': rid,
                               'version_id': version, 'sha256': event['sha256']})


def candidate_job(path, state):
    from exactcut import material_workers as worker
    event = next(e for e in state['outbox'].values() if e['kind'] == 'candidate' and e['version_id'] == state['assets'][e['asset_id']]['candidate_version'])
    child = worker.materialize(path, event)
    job = {'id': token(), 'batch_id': child.name, 'request_id': event['asset_id'], 'phase': 'render',
           'attempt': 1, 'process_token': token(), 'status': 'running'}
    write_json_atomic(w.ROOT / '.web-state/jobs' / (job['id'] + '.json'), job)
    return worker, child, event, job


def rendered(child, event, *, master=b'new-master', preview=b'new-preview'):
    name = event['manifest']['requests'][0]['output_filename']
    video = child / 'production/videos' / name
    derivative = child / 'production/previews' / name
    video.parent.mkdir(parents=True, exist_ok=True); derivative.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(master); derivative.write_bytes(preview)
    record = {'status': 'qc_passed', 'output_sha256': file_sha256(video), 'output': str(video),
              'preview': {'master_sha256': file_sha256(video), 'sha256': file_sha256(derivative), 'bytes': len(preview)}}
    result_store.put(child / 'production', event['asset_id'], record)
    return video, derivative


def changed_candidate():
    m, path, state = enrolled(media=True)
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] += ' edited'
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    old = state['assets'][draft['rows'][0]['request_id']]['current_version']
    return m, path, m.confirm(path, {'revision': state['revision'], 'mutation_id': token()}), old


def test_candidate_pair_freezes_by_copy_and_commit_is_idempotent(workspace):
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    assert hasattr(worker, 'stage_enter'), 'Actual stage fencing is missing'
    worker.stage_enter(job)
    video, preview = rendered(child, event)
    worker.stage_finish(job, 0)
    latest = m.read(path); current = latest['assets'][event['asset_id']]['current_version']
    assert current != old
    version = latest['versions'][current]
    frozen = Path(version['master_path'])
    assert frozen.read_bytes() == video.read_bytes() and frozen.stat().st_ino != video.stat().st_ino
    assert Path(version['preview_path']).read_bytes() == preview.read_bytes()
    video.write_bytes(b'late-worker-overwrite')
    worker.stage_finish(job, 0)
    assert frozen.read_bytes() == b'new-master'
    assert m.read(path)['assets'][event['asset_id']]['history'] == [old]


@pytest.mark.parametrize('failure', ['preview', 'master', 'oss', 'generation', 'deleted', 'paused', 'attempt'])
def test_failed_or_late_candidate_never_replaces_current(workspace, monkeypatch, failure):
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    assert hasattr(worker, 'stage_enter'), 'Actual stage fencing is missing'
    worker.stage_enter(job)
    video, preview = rendered(child, event)
    if failure == 'preview': preview.write_bytes(b'corrupted')
    if failure == 'master': video.write_bytes(b'corrupted')
    if failure == 'oss': monkeypatch.setattr(worker, 'require_oss', lambda: True)
    if failure == 'attempt':
        write_json_atomic(w.ROOT / '.web-state/jobs' / (job['id'] + '.json'), {**job, 'attempt': 2, 'process_token': token()})
    if failure in {'generation', 'deleted', 'paused'}:
        state = m.read(path)
        if failure == 'paused': m.control(path, 'pause', {'revision': state['revision'], 'mutation_id': token()})
        else:
            draft = copy.deepcopy(state['draft'])
            if failure == 'generation': draft['rows'][0]['hook_text'] += ' newer'
            else: draft['rows'] = []
            m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    worker.stage_finish(job, 0)
    latest = m.read(path)
    assert latest['assets'][event['asset_id']]['current_version'] == old
    assert Path(latest['versions'][old]['master_path']).read_bytes() == b'pretend-video-for-upload-integrity-test'


def test_old_stage_cannot_claim_new_durable_attempt(workspace):
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    assert hasattr(worker, 'stage_enter'), 'Actual stage fencing is missing'
    write_json_atomic(w.ROOT / '.web-state/jobs' / (job['id'] + '.json'), {**job, 'attempt': 2, 'process_token': token()})
    with pytest.raises(ValueError): worker.stage_enter(job)


def test_workflow_detail_preview_repair_copy_and_legacy_guards(workspace):
    m, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    item = w.detail(path.name)
    assert item.get('media_managed') is True
    assert item['assets'][0]['version_id'] == state['assets'][rid]['current_version']
    master, record = w.preview_file(path.name, rid)
    assert record['media_version_id'] == item['assets'][0]['version_id']
    repaired = w.clone_batch(path.name, purpose='repair')
    assert repaired['id'] == path.name
    copied = w.clone_batch(path.name, purpose='copy')
    assert copied['id'] != path.name and copied['draft']['rows'][0]['request_id'] != rid
    for operation, args in [(w.produce, (path.name,)), (w.fetch, (path.name,)), (w.render, (path.name, rid)),
                            (w.make_batch_previews, (path.name,)), (w.queue_publish, (path.name, rid, record['output_sha256']))]:
        with pytest.raises(ValueError, match='纳管'): operation(*args)
    assert master.read_bytes() == b'pretend-video-for-upload-integrity-test'


def test_current_preview_and_receipt_cannot_cross_version_or_tombstone(workspace):
    m, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    assert hasattr(m, 'current_file'), 'Managed current resolver missing'
    master, record = m.current_file(path, rid)
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': {**state['draft'], 'rows': []}})
    with pytest.raises(ValueError): m.current_file(path, rid)
    with pytest.raises(ValueError): m.save_receipt(path, rid, record, master, {}, original=True)
    state = m.restore(path, rid, {'revision': state['revision'], 'mutation_id': token()})
    assert m.current_file(path, rid)[0] == master


@pytest.fixture
def http(workspace):
    import threading
    from http.server import ThreadingHTTPServer
    from exactcut import webserver
    server = ThreadingHTTPServer(('127.0.0.1', 0), webserver.Handler)
    server.password = 'material-test-password-12345'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown(); server.server_close()


def test_http_managed_save_delete_restore_and_internal_denial(http):
    import json
    from test_blank_batch_api import call
    m, path, state = enrolled()
    route = '/api/batches/' + path.name
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] += ' HTTP edit'
    code, _, body = call(http, 'POST', route + '/save', {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert code == 200, body
    saved = json.loads(body)
    assert saved['mutation']['action'] == 'save' and saved['mutation']['revision'] == saved['revision']
    assert saved['draft']['rows'][0]['hook_text'].endswith('HTTP edit')
    state = m.confirm(path, {'revision': saved['revision'], 'mutation_id': token()})
    m.recover_outbox(); child = next(iter(state['outbox']))
    assert call(http, 'GET', '/api/batches/' + child)[0] == 404
    assert call(http, 'POST', '/api/batches/' + child + '/start', {})[0] == 404
    code, _, body = call(http, 'POST', route + '/delete', {'revision': state['revision'], 'mutation_id': token()})
    assert code == 200 and path.exists(), body
    deleted = json.loads(body)
    code, _, body = call(http, 'POST', route + '/restore', {'revision': deleted['revision'], 'mutation_id': token()})
    assert code == 200 and not json.loads(body)['deleted_at'], body


def test_actual_run_child_commits_pair_before_stage_evidence(workspace, monkeypatch):
    import sys
    from exactcut import stage_results
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', job['process_token'])
    seen = []
    def evidence(*args, **kwargs):
        seen.append(m.read(path)['assets'][event['asset_id']]['current_version'])
        raise OSError('simulated crash after parent commit')
    monkeypatch.setattr(stage_results, 'complete', evidence)
    def render(): rendered(child, event); return 0
    with pytest.raises(OSError): stage_results.run_child(render)
    assert seen == [event['version_id']]
    worker.stage_finish(job, 0)
    assert m.read(path)['assets'][event['asset_id']]['history'] == [old]


def test_render_retry_uses_fresh_child_and_old_attempt_cannot_commit(workspace):
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job); rendered(child, event)
    write_json_atomic(w.ROOT / '.web-state/jobs' / (job['id'] + '.json'), {**job, 'status': 'queued'})
    m.recover_outbox()
    state = m.read(path)
    assert state['versions'][event['version_id']]['child_id'] != child.name
    worker.stage_finish(job, 0)
    assert m.read(path)['assets'][event['asset_id']]['current_version'] == old


def test_pause_is_inherited_before_child_scheduler_admission(workspace):
    from exactcut import batch_control
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    state = m.control(path, 'pause', {'revision': state['revision'], 'mutation_id': token()})
    assert batch_control.read(child)['paused']
    m.control(path, 'resume', {'revision': state['revision'], 'mutation_id': token()})
    assert not batch_control.read(child)['paused']


def test_internal_upload_uses_parent_namespace_and_managed_receipt_route(workspace, monkeypatch):
    from exactcut import preview_oss
    m, path, state, old = changed_candidate()
    worker, child, event, job = candidate_job(path, state)
    video, derivative = rendered(child, event)
    _, request = w.request_for(child, event['asset_id'])
    record = w.result_records(child)[event['asset_id']]
    seen = []
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    monkeypatch.setattr(preview_oss, '_connect', lambda: object())
    def uploaded(video, batch_id, request_id, **kw):
        seen.append(batch_id)
        return {'sha256': file_sha256(video)}
    monkeypatch.setattr(preview_oss, '_ensure_uploaded', uploaded)
    preview_oss.upload_generated_preview(child / 'production', request, record)
    preview_oss.upload_generated_original(child / 'production', request, record)
    assert seen == [path.name, path.name]


def test_managed_ending_images_append_only_and_legacy_upload_rejected(workspace):
    import base64
    m, path, state = enrolled()
    rid = state['draft']['rows'][0]['request_id']
    payload = base64.b64encode(b'\x89PNG\r\n\x1a\nimage1').decode()
    assert hasattr(m, 'save_ending_image'), 'Append-only managed input writer missing'
    image1 = m.save_ending_image(path, rid, payload)
    image2 = m.save_ending_image(path, rid, base64.b64encode(b'\xff\xd8\xffimage2').decode())
    assert image1['image'] != image2['image']
    assert (path / 'endings' / image1['image']).exists()
    with pytest.raises(ValueError, match='纳管'): w.save_ending_image(path.name, rid, payload)


def test_cross_process_cas_has_one_winner(workspace):
    import multiprocessing
    m, path, state = enrolled()
    context = multiprocessing.get_context('fork'); results = context.Queue(); ready = context.Event()
    def writer(label):
        draft = copy.deepcopy(state['draft']); draft['rows'][0]['hook_text'] = label
        ready.wait()
        try:
            m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
            results.put('saved')
        except ValueError: results.put('conflict')
    processes = [context.Process(target=writer, args=(str(i),)) for i in range(2)]
    for process in processes: process.start()
    ready.set()
    for process in processes: process.join(10); assert process.exitcode == 0
    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == ['conflict', 'saved']


def test_process_crash_after_registry_before_state_recovers_explicitly(workspace):
    import multiprocessing
    import os
    m = module(); item = imported(); path = w.folder(item['id'])
    def crash():
        original = m.durable_write
        def write(file, value):
            if file.name == 'media-state.json': os._exit(97)
            original(file, value)
        m.durable_write = write
        m.enroll(path)
    process = multiprocessing.get_context('fork').Process(target=crash)
    process.start(); process.join(10); assert process.exitcode == 97
    assert m.managed(path)
    with pytest.raises(ValueError): m.read(path)
    state = m.enroll(path)
    assert state['batch_id'] == path.name and m.read(path) == state


def test_managed_http_rechecks_maintenance_inside_release_barrier(http, monkeypatch):
    from contextlib import contextmanager
    from test_blank_batch_api import call
    from exactcut import webserver
    m, path, state = enrolled()
    maintenance = path / 'test-maintenance'
    monkeypatch.setattr(webserver, 'maintenance_file', lambda: maintenance)
    entered = []
    @contextmanager
    def barrier():
        entered.append(True); maintenance.write_text('release')
        yield
    monkeypatch.setattr(webserver, 'mutation_lock', barrier)
    code, _, _ = call(http, 'POST', '/api/batches/' + path.name + '/pause', {'revision': state['revision'], 'mutation_id': token()})
    assert entered and code == 503
    assert not m.read(path)['control']['paused']


def test_unknown_delivery_explicit_verification_keeps_same_snapshot(workspace):
    m, path, state = enrolled(media=True); rid = state['draft']['rows'][0]['request_id']
    vid = state['assets'][rid]['current_version']; sha = state['versions'][vid]['record']['output_sha256']
    state = m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid, 'version_id': vid, 'sha256': sha})
    m.recover_outbox(); event = next(iter(state['outbox'].values())); child = w.BATCHES / event['child_id']
    entry = {'status': 'unverified', 'approved_sha256': sha,
             'publication': {'sha256': sha, 'feishu_record_id': 'record-1', 'oss_url': 'https://fixture.invalid/video', 'oss_key': 'fixture/video.mp4'},
             'intent': {'status': 'pending', 'receipt': {'material_id': 'material-1'}}}
    write_json_atomic(child / 'publications.json', {rid: entry}); m.recover_outbox()
    state = m.read(path)
    assert hasattr(m, 'queue_verification'), 'Explicit historical delivery verification missing'
    state = m.queue_verification(path, {'revision': state['revision'], 'mutation_id': token(), 'delivery_id': event['delivery_id']})
    m.recover_outbox(); w.recover_verification_queue([]); m.recover_outbox()
    jobs = [read_json(f) for f in (w.ROOT / '.web-state/jobs').glob('*.json')]
    verifiers = [j for j in jobs if j.get('action') == 'verify-delivery']
    assert len(verifiers) == 1 and verifiers[0]['batch_id'] == child.name
    assert read_json(child / 'publications.json')[rid]['verification_only']['approved_sha256'] == sha
    assert m.read(path)['deliveries'][event['delivery_id']]['status'] == 'unverified'


def test_enrollment_rejects_foreign_instance_identity(workspace):
    m = module(); item = imported(); path = w.folder(item['id'])
    write_json_atomic(path / 'control.json', {'children': [{'pid': 999999, 'pgid': 999999, 'process_token': token(), 'instance': '--instance=another-host'}]})
    with pytest.raises(ValueError, match='身份'): m.enroll(path)


def test_enrollment_rejects_hash_anomaly_and_unknown_delivery(workspace):
    m = module(); item = imported(); path, request, video, record = complete_asset(item)
    video.write_bytes(b'changed')
    with pytest.raises(ValueError, match='校验'): m.enroll(path)
    video.write_bytes(b'pretend-video-for-upload-integrity-test')
    write_json_atomic(path / 'publications.json', {request['request_id']: {'status': 'unverified'}})
    with pytest.raises(ValueError, match='交付'): m.enroll(path)


def test_approval_is_version_bound_even_identical_sha_and_delivery_is_fixed(workspace):
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    old_sha = state['versions'][old]['record']['output_sha256']; rid = event['asset_id']
    state = m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid, 'version_id': old, 'sha256': old_sha})
    delivery = next(iter(state['deliveries'].values()))
    worker.stage_enter(job); rendered(child, event, master=b'pretend-video-for-upload-integrity-test'); worker.stage_finish(job, 0)
    state = m.read(path)
    assert state['deliveries'][delivery['delivery_id']]['version_id'] == old
    with pytest.raises(ValueError, match='版本'):
        m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid, 'version_id': old, 'sha256': old_sha})


def test_current_paths_reject_traversal_after_corruption(workspace):
    m, path, state = enrolled(media=True); rid = state['draft']['rows'][0]['request_id']; vid = state['assets'][rid]['current_version']
    outside = path.parent / 'outside.mp4'; outside.write_bytes(b'outside')
    state['versions'][vid]['master_path'] = str(path / '..' / outside.name)
    write_json_atomic(path / 'media-state.json', state)
    with pytest.raises(ValueError): m.current_file(path, rid)


def test_internal_batch_physical_delete_and_manual_parent_preview_are_rejected(workspace, monkeypatch):
    from exactcut import batch_control
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    m.control(path, 'archive', {'revision': state['revision'], 'mutation_id': token()})
    write_json_atomic(child / 'control.json', {'archived': True})
    with pytest.raises(ValueError): w.delete_batch(child.name, child.name)
    assert child.exists()
    monkeypatch.setattr(w, 'make_preview', lambda *a, **k: pytest.fail('legacy writer was reached'))
    with pytest.raises(ValueError, match='纳管'): w._make_batch_previews(path.name)


def test_parent_results_and_feedback_follow_current_version_for_added_asset(workspace):
    m, path, state = enrolled()
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['seq'] = '2'
    draft['rows'].append({**copy.deepcopy(draft['rows'][0]), 'request_id': None, 'seq': '3'})
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    added = state['draft']['rows'][-1]['request_id']
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    from exactcut import material_workers as worker
    event = next(e for e in state['outbox'].values() if e['asset_id'] == added)
    child = worker.materialize(path, event)
    job = {'id': token(), 'batch_id': child.name, 'request_id': added, 'phase': 'render', 'attempt': 1, 'process_token': token(), 'status': 'running'}
    write_json_atomic(w.ROOT / '.web-state/jobs' / (job['id'] + '.json'), job)
    worker.stage_enter(job); rendered(child, event); worker.stage_finish(job, 0)
    assert added in w.result_records(path)
    feedback = w.add_feedback(path.name, {'request_id': added, 'category': '剧情衔接', 'message': 'new material feedback'})
    assert feedback['request_id'] == added and feedback['version_id'] == event['version_id']


def test_managed_backfill_never_visits_internal_outputs(workspace, monkeypatch):
    from exactcut import media_backfill, preview_oss
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    rendered(child, event)
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    visited = []
    monkeypatch.setattr(preview_oss, '_connect', lambda: pytest.fail('managed/internal legacy backfill attempted network'))
    report = media_backfill.backfill(apply=True)
    assert all(row['batch_id'] != child.name for row in report)
    assert any(row['batch_id'] == path.name and row['status'] == 'managed' for row in report)

from test_round1_integration import chain


def test_managed_fetch_render_scheduler_chain_commits_and_delivery_is_separate(chain, monkeypatch):
    import sys
    from exactcut import stage_results, webserver as web, orchestrator
    m = module(); item = chain.batch(); path = w.folder(item['id']); state = m.enroll(path)
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    event = next(iter(state['outbox'].values()))
    original = orchestrator.execute_batch
    def render_pair(project, manifest, config, **kw):
        result = original(project, manifest, config, **kw)
        for request in manifest.requests:
            record = result['requests'][request.request_id]
            preview = kw['output_dir'] / 'previews' / request.output_filename
            preview.parent.mkdir(exist_ok=True); preview.write_bytes(b'scheduler-preview')
            record['preview'] = {'master_sha256': record['output_sha256'], 'sha256': file_sha256(preview)}
            result_store.put(kw['output_dir'], request.request_id, record)
        return result
    monkeypatch.setattr(orchestrator, 'execute_batch', render_pair)
    def finish(phase):
        job, process, _ = chain.one(phase)
        monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', job['process_token'])
        args = [job['batch_id']] + ([job['request_id']] if phase == 'render' else [])
        assert stage_results.run_child(getattr(w, phase), *args) == 0
        process.code = 0; chain.table.rows.pop(process.pid)
        chain.tick()
    chain.tick(); finish('fetch'); finish('render')
    state = m.read(path)
    assert state['assets'][event['asset_id']]['current_version'] == event['version_id']
    assert chain.fetch_calls and chain.render_calls and not chain.upload_calls
    assert not chain.owned and not chain.reservations()
    assert w.detail(path.name)['assets'][0]['preview']


def test_idempotent_receipt_is_compact_and_replay_reads_latest_draft(workspace):
    m, path, state = enrolled()
    first = {'revision': state['revision'], 'mutation_id': token(), 'draft': copy.deepcopy(state['draft'])}
    first['draft']['rows'].append({**copy.deepcopy(state['draft']['rows'][0]), 'request_id': None, 'seq': '2'})
    saved = m.save_draft(path, first)
    assert 'mutation' in saved, 'Compact immutable mutation receipt missing'
    receipt = saved['mutation']; count = len(saved['assets'])
    later = copy.deepcopy(saved['draft']); later['rows'][0]['hook_text'] = 'later-edit'
    current = m.save_draft(path, {'revision': saved['revision'], 'mutation_id': token(), 'draft': later})
    replay = m.save_draft(path, first)
    assert replay['mutation'] == receipt and replay['draft'] == current['draft']
    assert len(replay['assets']) == count
    assert 'versions' not in m.read(path)['mutations'][first['mutation_id']]['result']


def test_upload_only_retry_reuses_version_and_candidate_without_render(workspace, monkeypatch):
    from exactcut import preview_oss
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job); rendered(child, event)
    monkeypatch.setattr(worker, 'require_oss', lambda: True)
    worker.stage_finish(job, 0)
    state = m.read(path); assert state['versions'][event['version_id']]['status'] == 'failed'
    retry = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    assert retry['assets'][event['asset_id']]['candidate_version'] == event['version_id']
    assert any(e['kind'] == 'media-upload' for e in retry['outbox'].values())
    from test_preview_oss import Bucket
    bucket = Bucket()
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    m.recover_outbox()
    queued = next(read_json(f) for f in (w.ROOT / '.web-state/jobs').glob('*.json') if read_json(f).get('phase') == 'media-upload')
    queued.update(attempt=1, process_token=token(), status='running')
    write_json_atomic(w.ROOT / '.web-state/jobs' / (queued['id'] + '.json'), queued)
    worker.stage_enter(queued)
    assert worker.retry_media_upload(child.name, event['asset_id']) == 0
    worker.stage_finish(queued, 0)
    latest = m.read(path)
    assert latest['assets'][event['asset_id']]['current_version'] == event['version_id']
    assert len(bucket.puts) == 2
    assert Path(latest['versions'][old]['master_path']).exists()


def test_delivery_target_drift_fails_before_any_external_operation(workspace, monkeypatch):
    m, path, state = enrolled(media=True); rid = state['draft']['rows'][0]['request_id']; vid = state['assets'][rid]['current_version']
    state = m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid, 'version_id': vid, 'sha256': state['versions'][vid]['record']['output_sha256']})
    from exactcut import material_workers as worker
    event = next(iter(state['outbox'].values())); child = worker.materialize(path, event)
    job = {'id': event['job_id'], 'batch_id': child.name, 'request_id': rid, 'phase': 'upload', 'attempt': 1, 'process_token': token()}
    write_json_atomic(w.ROOT / '.web-state/jobs' / (job['id'] + '.json'), job)
    monkeypatch.setenv('ALIYUN_OSS_PREFIX', 'changed-target-after-approval')
    with pytest.raises(ValueError, match='目标'): worker.stage_enter(job)


def test_freeze_fsyncs_rename_parent_before_current_commit(workspace, monkeypatch):
    import os
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job); rendered(child, event)
    assert hasattr(m, 'sync_directory'), 'Directory durability helper missing'
    seen = []; original = m.sync_directory
    def sync(directory): seen.append(Path(directory)); return original(directory)
    monkeypatch.setattr(m, 'sync_directory', sync)
    worker.stage_finish(job, 0)
    current = m.read(path)['versions'][event['version_id']]
    assert Path(current['master_path']).parents[2] in seen


def test_managed_delivery_gate_does_not_create_outbox(workspace, monkeypatch):
    m, path, state = enrolled(media=True); rid = state['draft']['rows'][0]['request_id']; vid = state['assets'][rid]['current_version']
    assert hasattr(m, 'delivery_enabled'), 'Managed delivery gate missing'
    monkeypatch.setattr(m, 'delivery_enabled', lambda: False)
    with pytest.raises(ValueError, match='交付'):
        m.queue_delivery(path, {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid, 'version_id': vid, 'sha256': state['versions'][vid]['record']['output_sha256']})
    assert m.read(path)['outbox'] == {}


def test_old_render_cannot_fail_new_upload_retry(workspace, monkeypatch):
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job); rendered(child, event); monkeypatch.setattr(worker, 'require_oss', lambda: True)
    worker.stage_finish(job, 0); state = m.read(path)
    m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    worker.stage_finish(job, 0)
    assert m.read(path)['versions'][event['version_id']]['status'] == 'queued'


def test_deleted_candidate_cancels_own_running_process_without_deleting_files(chain):
    import signal
    m = module(); item = chain.batch(); path = w.folder(item['id']); state = m.enroll(path)
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    event = next(iter(state['outbox'].values())); chain.tick()
    job, process, _ = chain.one('fetch'); child = w.BATCHES / event['child_id']
    state = m.read(path)
    m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': {**state['draft'], 'rows': []}})
    chain.tick()
    assert (process.pid, signal.SIGKILL) in chain.signals
    assert child.exists() and (child / 'confirmed.json').exists()


def test_killed_media_upload_scheduler_requires_fresh_explicit_retry(chain, monkeypatch):
    from exactcut import webserver as web
    m, path, state, old = changed_candidate(); worker, child, event, render_job = candidate_job(path, state)
    worker.stage_enter(render_job); rendered(child, event)
    monkeypatch.setattr(worker, 'require_oss', lambda: True)
    worker.stage_finish(render_job, 0)
    render_job.update(status='completed'); web.save(render_job)
    state = m.read(path); m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    chain.tick(); job, process, _ = chain.one('media-upload')
    worker.stage_enter(job)
    process.code = -9; chain.table.rows.pop(process.pid)
    chain.tick(); chain.tick()
    state = m.read(path)
    assert state['versions'][event['version_id']]['status'] == 'failed'
    assert not chain.owned
    assert read_json(web.JOBS / (job['id'] + '.json'))['status'] == 'interrupted'
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    chain.tick(); replacement, _, _ = chain.one('media-upload')
    assert replacement['id'] != job['id'] and replacement['attempt'] == 1
    assert replacement['batch_id'] == child.name
    worker.stage_enter(replacement)
    assert not chain.render_calls


def test_pause_at_final_commit_can_retry_completed_pair_after_resume(chain, monkeypatch):
    import sys
    from exactcut import stage_results, webserver as web
    m = module(); item = chain.batch(); path = w.folder(item['id']); state = m.enroll(path)
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    event = next(iter(state['outbox'].values()))
    chain.tick(); job, process, _ = chain.one('fetch')
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', job['process_token'])
    assert stage_results.run_child(w.fetch, job['batch_id']) == 0
    process.code = 0; chain.table.rows.pop(process.pid); chain.tick()
    job, process, _ = chain.one('render')
    monkeypatch.setitem(sys._xoptions, 'exactcut_process_token', job['process_token'])
    def render_then_pause(batch_id, rid):
        rendered(w.folder(batch_id), event)
        state = m.read(path)
        m.control(path, 'pause', {'revision': state['revision'], 'mutation_id': token()})
        return 0
    assert stage_results.run_child(render_then_pause, job['batch_id'], job['request_id']) == 0
    process.code = 0; chain.table.rows.pop(process.pid); chain.tick()
    state = m.read(path)
    m.control(path, 'resume', {'revision': state['revision'], 'mutation_id': token()})
    chain.tick(); state = m.read(path)
    assert state['versions'][event['version_id']]['status'] == 'failed'
    retried = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    assert retried['versions'][event['version_id']]['upload_job_id']
    chain.tick(); retry, _, _ = chain.one('media-upload')
    assert retry['batch_id'] == event['child_id']


def test_managed_empty_draft_can_save_last_asset_deletion():
    import json, shutil, subprocess
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    code = source[source.index('function hasDraftData('):source.index('\nfunction ', source.index('function controls(') + 1)]
    script = "const assert=require('node:assert/strict');const elements={};const $=id=>elements[id]??={};const busy=new Set();const activeStatuses=[];const document={querySelectorAll:()=>[]};let dirty=true;let current={media_managed:true,draft:{rows:[]},runtime:{status:'review'},assets:[],errors:[]};" + code + "\ncontrols();assert.equal($('saveButton').disabled,false);assert.equal($('confirmButton').disabled,true);"
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_requeued_old_render_cannot_replace_upload_retry(workspace, monkeypatch):
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job); rendered(child, event)
    monkeypatch.setattr(worker, 'require_oss', lambda: True)
    worker.stage_finish(job, 0)
    state = m.read(path)
    retry = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    upload_id = retry['versions'][event['version_id']]['upload_job_id']
    job.update(status='queued')
    jobs = w.ROOT / '.web-state/jobs'
    write_json_atomic(jobs / (job['id'] + '.json'), job)
    worker.reconcile_candidates(path, jobs)
    version = m.read(path)['versions'][event['version_id']]
    assert version['child_id'] == child.name
    assert version['upload_job_id'] == upload_id and version['status'] == 'queued'
    assert read_json(jobs / (job['id'] + '.json'))['status'] == 'interrupted'


def test_detail_projects_candidate_execution_separately_from_current(workspace):
    m, path, state, old = changed_candidate(); worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job)
    result_store.put(child / 'production', event['asset_id'], {'status': 'processing', 'stage': 'base_cut', 'progress_details': {'completed': 1, 'total': 3}})
    w.update(child, download_complete=True)
    detail = m.detail(path); asset = detail['assets'][0]
    assert asset['preview'] and asset['version_id'] == old
    assert asset['candidate']['observation']['details'] == {'completed': 1, 'total': 3}
    assert detail['runtime']['download_complete'] is True
    worker.stage_finish(job, 1)
    assert m.detail(path)['assets'][0]['candidate']['status'] == 'failed'


def test_completed_managed_pair_reports_downloads_ready(workspace):
    m, path, state = enrolled(media=True)
    assert m.detail(path)['runtime']['download_complete'] is True


def test_candidate_progress_ui_keeps_current_preview_separate():
    import shutil, subprocess
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helper = source[source.index('function productionProgress('):source.index('\nfunction renderQueue(')]
    prefix = source[source.index('function executionAsset('):source.index('function productionProgress(')] if 'function executionAsset(' in source else ''
    script = "const assert=require('node:assert/strict');const esc=s=>String(s??'');const durationLabel=s=>s+'秒';const stageNames={base_cut:'剪辑片段'};const materialLabel=s=>s;const materialEventDetails=()=>'';" + prefix + helper + """
const asset={request_id:'R1',preview:'/old',status:'qc_passed',candidate:{status:'processing',stage:'base_cut',observation:{details:{completed:1,total:3}}}};
let html=productionProgress(asset,{});assert.ok(html.includes('剪辑片段'));assert.ok(html.includes('1/3'));assert.ok(!html.includes('制作完成'));assert.equal(asset.preview,'/old');
asset.candidate={status:'failed',error:'upload failed'};html=productionProgress(asset,{});assert.ok(html.includes('制作失败'));assert.ok(html.includes('upload failed'));assert.ok(!html.includes('制作完成'));
"""
    summary = next(line for line in source.splitlines() if line.startswith("$('progressCount').textContent="))
    script += "const current={assets:[asset]},r={},assets=current.assets.map(executionAsset),done=assets.filter(a=>a.preview).length;const box={};const $=()=>box;" + summary + "assert.ok(box.textContent.includes('1 条失败'));assert.ok(box.textContent.startsWith('0/1'));"
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_managed_copy_retains_only_referenced_ending_image_as_independent_bytes(workspace):
    import base64
    m, path, state = enrolled(); rid = state['draft']['rows'][0]['request_id']
    image = m.save_ending_image(path, rid, base64.b64encode(b'\x89PNG\r\n\x1a\nreferenced').decode())['image']
    m.save_ending_image(path, rid, base64.b64encode(b'\x89PNG\r\n\x1a\nunreferenced').decode())
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['composition'] = {'plots': [{'episode': '1', 'start': '00:00', 'end': '00:10'}], 'ending': {'image': image}}
    m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    copied = w.clone_batch(path.name)
    newrid = copied['draft']['rows'][0]['request_id']
    target = w.ending_image_file(copied['id'], newrid)
    original = path / 'endings' / image
    assert target.read_bytes() == original.read_bytes()
    assert target.stat().st_ino != original.stat().st_ino
    assert len(list(target.parent.iterdir())) == 1
    assert copied['draft']['rows'][0]['composition']['ending']['image'] == target.name


def test_enrollment_media_hashing_releases_global_naming_lock(workspace, monkeypatch):
    import fcntl
    m = module(); path, _, _, _ = complete_asset(imported())
    original = m.file_sha256
    probes = []
    def probe(file):
        with (w.ROOT / '.web-state/naming.lock').open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: pytest.fail('Media hashing holds global naming lock')
            else: fcntl.flock(lock, fcntl.LOCK_UN)
        probes.append(file)
        return original(file)
    monkeypatch.setattr(m, 'file_sha256', probe)
    m.enroll(path)
    assert probes


def test_enrollment_rechecks_media_identity_before_commit(workspace, monkeypatch):
    m = module(); path, _, video, _ = complete_asset(imported())
    original = m.file_sha256
    def changing_hash(file):
        digest = original(file)
        if file == video: video.write_bytes(b'changed after verification')
        return digest
    monkeypatch.setattr(m, 'file_sha256', changing_hash)
    with pytest.raises(ValueError, match='更新'): m.enroll(path)
    assert not m.managed(path)


def test_managed_copy_rejects_changed_content_addressed_ending(workspace):
    import base64
    m, path, state = enrolled(); rid = state['draft']['rows'][0]['request_id']
    image = m.save_ending_image(path, rid, base64.b64encode(b'\x89PNG\r\n\x1a\noriginal').decode())['image']
    draft = copy.deepcopy(state['draft']); draft['rows'][0]['composition'] = {'plots': [], 'ending': {'image': image}}
    m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    (path / 'endings' / image).write_bytes(b'changed')
    with pytest.raises(ValueError, match='校验'): w.clone_batch(path.name)


def test_managed_detail_preserves_project_and_candidate_execution_history(workspace):
    m, path, state = enrolled()
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    worker, child, event, job = candidate_job(path, state)
    rid = event['asset_id']
    w.update(child, 'producing', rid + '：开始制作')
    detail = m.detail(path)
    messages = [e['message'] for e in detail['runtime']['events']]
    assert any('项目已创建' in message for message in messages)
    assert rid + '：开始制作' in messages
    assert any('内容已确认' in message for message in messages)


def test_managed_save_allocates_sequences_around_existing_completed_project(workspace):
    original = imported()
    complete_asset(original)
    item = w.create_blank()
    path = w.folder(item['id']); m = module(); state = m.enroll(path)
    draft = copy.deepcopy(original['draft'])
    draft['rows'][0]['request_id'] = state['draft']['rows'][0]['request_id']
    saved = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert saved['draft']['rows'][0]['seq'] != original['draft']['rows'][0]['seq']
    confirmed = m.confirm(path, {'revision': saved['revision'], 'mutation_id': token()})
    assert confirmed['assets'][draft['rows'][0]['request_id']]['candidate_version']


@pytest.mark.parametrize('candidate_status', ['queued', 'processing', 'failed', 'current', None])
def test_production_events_follow_selected_version_without_old_history(workspace, candidate_status):
    m, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    write_json_atomic(path / 'runtime.json', {'events': [{'at': '2026-10-11T01:28:48+00:00', 'message': rid + '：旧成片记录'}]})
    if candidate_status is not None:
        draft = copy.deepcopy(state['draft']); draft['drama_title'] += '新版'
        state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
        state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
        vid = state['assets'][rid]['candidate_version']
        version = state['versions'][vid]
        child = w.BATCHES / version['child_id']; child.mkdir(exist_ok=True)
        if candidate_status != 'queued':
            write_json_atomic(child / 'runtime.json', {'events': [{'at': '2026-10-11T01:50:42+00:00', 'message': rid + '：本轮制作'}]})
        version['status'] = candidate_status
        if candidate_status == 'current':
            state['assets'][rid].update(current_version=vid, candidate_version=None)
        m._write(path, state)
    detail = m.detail(path)
    messages = [e['message'] for e in detail['assets'][0]['production_events']]
    assert messages == ([rid + '：旧成片记录'] if candidate_status is None else [] if candidate_status == 'queued' else [rid + '：本轮制作'])
    assert rid + '：旧成片记录' in [e['message'] for e in detail['runtime']['events']]


def test_legacy_text_timeline_has_lossless_clip_editor_without_changing_draft(workspace):
    from exactcut.intake import compile_clips, validate_draft
    m, path, state = enrolled()
    draft = copy.deepcopy(state['draft']); rid = draft['rows'][0]['request_id']
    draft['rows'][0].pop('clips', None)
    draft['rows'][0]['timeline_text'] = 'EP1 7s-37s\nEP2 00:01.234-00:03.456'
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    before = (path / 'media-state.json').read_bytes()
    detail = m.detail(path)
    assert detail['draft'] == state['draft']
    edited = copy.deepcopy(detail['draft'])
    edited['rows'][0]['clips'] = detail['clip_editor'][rid]
    compile_clips(edited['rows'][0])
    original, errors = validate_draft(state['draft'], 'fixture')
    converted, changed_errors = validate_draft(edited, 'fixture')
    assert not errors and not changed_errors
    assert converted.requests[0].timeline == original.requests[0].timeline
    assert (path / 'media-state.json').read_bytes() == before


@pytest.mark.parametrize('timeline', ['EP1 全集', 'EP1 00:07-结尾', '字卡|测试|1.5s'])
def test_clip_editor_does_not_reinterpret_open_ended_or_title_cards(workspace, timeline):
    m, path, state = enrolled()
    draft = copy.deepcopy(state['draft']); draft['rows'][0].pop('clips', None)
    draft['rows'][0]['timeline_text'] = timeline
    m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert not m.detail(path)['clip_editor']


def test_managed_copy_starts_with_configuration_only(workspace):
    m, path, state = enrolled(media=True)
    (path / 'logs').mkdir(); (path / 'logs/old.log').write_text('old execution')
    (path / 'cache').mkdir(); (path / 'cache/old.cache').write_text('old intermediate')
    before = (path / 'media-state.json').read_bytes()
    copied = w.clone_batch(path.name)
    target = w.folder(copied['id'])
    assert not (target / 'production').exists()
    assert not (target / 'logs').exists() and not (target / 'cache').exists()
    assert not (target / 'publications.json').exists()
    assert copied['runtime']['status'] == 'draft'
    assert all(not a.get('preview') and not a.get('version_id') and not a.get('candidate') for a in copied['assets'])
    m.enroll(target)  # The HTTP new-batch handler enrolls a copied project.
    copied = m.detail(target)
    assert not m.read(target)['versions'] and not m.read(target)['outbox'] and not m.read(target)['deliveries']
    assert copied['filename'] == '网页填写'
    assert '当前成片保留' not in copied['runtime']['message']
    assert all('开始制作' not in e['message'] for e in copied['runtime']['events'])
    assert (path / 'media-state.json').read_bytes() == before
    assert copied['draft']['rows'][0]['timeline_text'] == state['draft']['rows'][0]['timeline_text']
    assert copied['draft']['rows'][0].get('hook_options') == state['draft']['rows'][0].get('hook_options')
    assert copied['draft']['rows'][0]['request_id'] != state['draft']['rows'][0]['request_id']
