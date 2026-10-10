"""Real scheduler/workflow stages with only process/media/remote boundaries replaced."""
import queue
import signal
import uuid
from types import SimpleNamespace

import pytest
from exactcut import accounts, batch_control as control, concurrency, publish
from exactcut import stage_results, storage, workflow as w, webserver as web
from exactcut.runtime import instance_marker
from exactcut.util import file_sha256, json_sha256, read_json, write_json_atomic
from test_workflow import workspace, imported
from test_linux_release import ops


@pytest.fixture
def chain(workspace, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(workspace / 'sources'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setattr(web, 'JOBS', workspace / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setattr(web, 'INLINE_RENDERS', {})
    table = control.FakeProcessTable({})
    monkeypatch.setattr(control, 'PROCESSES', table)
    limits = {'enabled': True, 'fetch': 2, 'render': 2, 'upload': 2, 'poll': 2, 'max_active_children': 4}
    monkeypatch.setattr(concurrency, 'load_config', lambda: {'concurrency': limits})
    monkeypatch.setattr(control.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10**12))
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda path: None)
    monkeypatch.setattr(w, 'validate_downloaded_video', lambda path: None)
    monkeypatch.setattr('exactcut.media.validate_manifest_sources', lambda *args: None)
    monkeypatch.setattr('exactcut.timing_review._duration', lambda *args: 130000)
    fetch_calls, render_calls, upload_calls, poll_calls = [], [], [], []
    def fetch(drama_id, episodes, **kwargs):
        fetch_calls.append((drama_id, tuple(episodes)))
        files = {}
        for episode in episodes:
            file = workspace / 'sources' / drama_id / (episode + '.mp4')
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(('source:' + drama_id + ':' + episode).encode())
            files[episode] = file
        return files
    monkeypatch.setattr('exactcut.source_cache.ensure', fetch)
    monkeypatch.setattr('exactcut.source_cache.reused_episodes', lambda: [])
    def render(project, manifest, config, *, output_dir, progress, request_ids=None):
        render_calls.append((manifest.drama_id, output_dir))
        results = {'requests': {}}
        for request in manifest.requests:
            if request_ids is not None and request.request_id not in request_ids: continue
            file = output_dir / 'videos' / request.output_filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(('render:' + str(output_dir)).encode())
            results['requests'][request.request_id] = {'status': 'qc_passed', 'output': str(file), 'output_sha256': file_sha256(file)}
        from exactcut import result_store
        for rid, record in results['requests'].items(): result_store.patch(output_dir, rid, **record)
        return results
    monkeypatch.setattr('exactcut.orchestrator.execute_batch', render)
    def upload(video, manifest, request):
        upload_calls.append((video, manifest.drama_id, request.request_id))
        return {'feishu_record_id': 'fixture-' + video.parents[2].name, 'sha256': file_sha256(video),
                'oss_key': publish.OssPublisher().key_for(video)[0]}
    monkeypatch.setattr(publish, 'publish_to_queue', upload)
    answers = ['syncing', 'ready']
    def poll(record_id):
        poll_calls.append(record_id)
        action = answers.pop(0) if answers else 'ready'
        return {'record_id': record_id, 'action': action, **({'video_id': 'fixture-video'} if action == 'ready' else {})}
    monkeypatch.setattr(publish, 'sync_record_once', poll)
    processes = []
    def spawn(command, **kwargs):
        process = SimpleNamespace(pid=10000 + len(processes), code=None)
        process.poll = lambda: process.code
        table.rows[process.pid] = {'pgid': process.pid, 'state': 'S', 'arguments': command}
        processes.append(process)
        return process
    monkeypatch.setattr(web.subprocess, 'Popen', spawn)
    signals = []
    def signal_group(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGSTOP: table.rows[pid]['state'] = 'T'
        elif sig == signal.SIGCONT: table.rows[pid]['state'] = 'S'
        elif sig == signal.SIGKILL: table.rows.pop(pid, None)
    monkeypatch.setattr(control.os, 'killpg', signal_group)

    class Chain:
        def __init__(self):
            self.pending, self.owned = [], {}
            self.limits, self.table, self.processes = limits, table, processes
            self.fetch_calls, self.render_calls = fetch_calls, render_calls
            self.upload_calls, self.poll_calls, self.signals = upload_calls, poll_calls, signals
        def batch(self, drama='101460', owner='alice'):
            batch = imported()
            path = w.folder(batch['id'])
            write_json_atomic(path / 'access.json', {'owner': owner})
            draft = read_json(path / 'draft.json')
            draft['draft']['drama_id'] = drama
            write_json_atomic(path / 'draft.json', draft)
            return w.detail(batch['id'])
        def enqueue(self, batch, action='start', rid=None):
            if action == 'start':
                command = w.start_batch(batch['id'], batch['revision'])
            else:
                record = w.result_records(w.folder(batch['id']))[rid]
                command = w.queue_publish(batch['id'], rid, record['output_sha256'])
            job = {'id': uuid.uuid4().hex, 'batch_id': batch['id'], 'owner': accounts.owner(w.folder(batch['id'])),
                   'action': action, 'status': 'queued', 'created': len(list(web.stored_jobs())),
                   'command': command, 'instance': instance_marker()}
            if rid: job['request_id'] = rid
            web.prepare_staged_job(job); web.save(job); web.WORK.put((job, job['command']))
            return job
        def tick(self):
            web.worker_tick(self.pending, self.owned)
            return list(self.owned.values())
        def one(self, phase, batch=None, rid=None):
            matches = [(job, process, log) for job, process, log in self.owned.values()
                       if job['phase'] == phase and (batch is None or job['batch_id'] == batch['id'])
                       and (rid is None or job.get('request_id') == rid)]
            assert len(matches) == 1, [(j['phase'], j['batch_id']) for j, _, _ in self.owned.values()]
            return matches[0]
        def finish(self, phase, batch=None, rid=None):
            job, process, _ = self.one(phase, batch, rid)
            args = [job['batch_id']] + ([job['request_id']] if job.get('request_id') and phase in {'render', 'upload', 'poll'} else [])
            code = getattr(w, phase)(*args)
            assert code == 0
            stage_results.complete(job, web.JOBS, w.BATCHES, w.ROOT, code)
            evidence = stage_results.verified(job, web.JOBS, w.BATCHES, w.ROOT)
            assert evidence and evidence['status'] == 'done'
            process.code = code; table.rows.pop(process.pid)
            self.tick()
            return job
        def approve(self, batch):
            assert w.runtime(w.folder(batch['id']))['status'] == 'review'
            rid = read_json(w.folder(batch['id']) / 'confirmed.json')['requests'][0]['request_id']
            return self.enqueue(batch, 'publish', rid)
        def due(self, batch):
            job = next(j for j in web.stored_jobs() if j.get('batch_id') == batch['id'] and j.get('phase') == 'poll' and j['status'] == 'queued')
            job['not_before'] = 0; web.save(job); self.tick()
            return job
        def reservations(self):
            return read_json(web.JOBS.parent / 'reservations.json')['jobs']
        def review(self, batch):
            self.enqueue(batch); self.tick(); self.finish('fetch', batch); self.finish('render', batch)
    harness = Chain()
    try:
        yield harness
    finally:
        for _, _, log in harness.owned.values(): log.close()


@pytest.mark.parametrize('serial', [False, True])
def test_batch_full_chain_requires_approval_and_delayed_poll_releases_slots(chain, serial):
    if serial:
        chain.limits.update(fetch=1, render=1, upload=1, poll=1, max_active_children=1)
    batch = chain.batch(); chain.review(batch)
    assert not chain.upload_calls and not chain.owned and not chain.reservations()
    chain.approve(batch); chain.tick(); chain.finish('upload', batch)
    assert not chain.owned and not chain.reservations() and not chain.poll_calls
    deferred = next(j for j in web.stored_jobs() if j.get('phase') == 'poll')
    assert deferred['not_before'] > web.time.time()
    chain.tick(); assert not chain.owned
    chain.due(batch); chain.finish('poll', batch)
    assert w.detail(batch['id'])['assets'][0]['status'] == 'fbbm_queued'
    assert not chain.owned and not chain.reservations()
    chain.due(batch); chain.finish('poll', batch)
    assert w.detail(batch['id'])['assets'][0]['status'] == 'fbbm_ready'
    assert len(chain.upload_calls) == 1 and len(chain.poll_calls) == 2
    assert len(chain.fetch_calls) == 1 and len(chain.render_calls) == 1
    assert not chain.reservations()


def test_two_actual_owners_and_projects_render_in_parallel_with_isolated_outputs(chain):
    a, b = chain.batch('101460', 'alice'), chain.batch('101461', 'bob')
    chain.enqueue(a); chain.enqueue(b); chain.tick()
    chain.finish('fetch', a); chain.finish('fetch', b)
    assert {j['owner'] for j, _, _ in chain.owned.values()} == {'alice', 'bob'}
    assert {j['phase'] for j, _, _ in chain.owned.values()} == {'render'}
    assert len(chain.reservations()) == 2
    chain.finish('render', a); chain.finish('render', b)
    av = w.preview_file(a['id'], a['assets'][0]['request_id'])[0]
    bv = w.preview_file(b['id'], b['assets'][0]['request_id'])[0]
    assert av != bv and av.read_bytes() != bv.read_bytes()
    assert {drama for drama, _ in chain.render_calls} == {'101460', '101461'}
    assert not chain.upload_calls and not chain.reservations()


def test_same_batch_stages_serialize_even_with_spare_capacity(chain):
    batch = chain.batch(); job = chain.enqueue(batch); chain.tick()
    sibling = dict(job, id=uuid.uuid4().hex, phase='render', lane='render', command=web.stage_command(job, 'render'))
    web.save(sibling); web.WORK.put((sibling, sibling['command']))
    chain.tick()
    assert len(chain.owned) == 1 and chain.one('fetch')[0]['id'] == job['id']
    chain.finish('fetch', batch)
    assert len(chain.owned) == 1 and chain.one('render')[0]['batch_id'] == batch['id']
    assert len(chain.reservations()) == 1


def test_waiting_fbbm_allows_other_batch_to_render_at_global_capacity_one(chain):
    chain.limits.update(fetch=1, render=1, upload=1, poll=1, max_active_children=1)
    a = chain.batch(); chain.review(a); chain.approve(a); chain.tick(); chain.finish('upload', a)
    chain.due(a); chain.finish('poll', a)
    assert not chain.owned and not chain.reservations()
    b = chain.batch('101461', 'bob'); chain.enqueue(b); chain.tick()
    assert len(chain.owned) == 1
    chain.finish('fetch', b)
    assert len(chain.owned) == 1 and chain.one('render', b)
    chain.tick(); assert len(chain.owned) == 1 and chain.one('render', b)[0].get('request_id')
    assert len(chain.poll_calls) == 1
    chain.finish('render', b)
    assert w.runtime(w.folder(b['id']))['status'] == 'review'
    assert w.detail(a['id'])['assets'][0]['status'] == 'fbbm_queued'
    chain.approve(b); chain.tick(); assert len(chain.owned) == 1
    chain.finish('upload', b); assert not chain.owned and not chain.reservations()
    chain.due(b); assert len(chain.owned) == 1
    chain.finish('poll', b); assert not chain.owned and not chain.reservations()
    chain.due(a); assert len(chain.owned) == 1
    chain.finish('poll', a)
    assert w.detail(a['id'])['assets'][0]['status'] == w.detail(b['id'])['assets'][0]['status'] == 'fbbm_ready'


def test_crashed_render_restarts_only_render_after_verified_fetch(chain):
    batch = chain.batch(); fetch = chain.enqueue(batch); chain.tick(); chain.finish('fetch', batch)
    render, process, log = chain.one('render', batch)
    manifest = read_json(w.folder(batch['id']) / 'confirmed.json')
    partial = w.folder(batch['id']) / 'production/videos' / manifest['requests'][0]['output_filename']
    partial.parent.mkdir(parents=True); partial.write_bytes(b'crash-partial-output')
    assert not stage_results.result_path(render, web.JOBS).exists()
    chain.table.rows.pop(process.pid); log.close()
    chain.owned.clear(); chain.pending.clear(); web.WORK = queue.Queue()
    web.recover_jobs(enqueue=True)
    saved = read_json(web.JOBS / (render['id'] + '.json'))
    assert saved['phase'] == 'render' and saved['status'] == 'queued'
    assert not chain.reservations()
    assert read_json(web.JOBS / (fetch['id'] + '.json'))['status'] == 'completed'
    chain.tick(); chain.finish('render', batch)
    assert len(chain.fetch_calls) == 1 and len(chain.render_calls) == 1
    assert w.runtime(w.folder(batch['id']))['status'] == 'review'
    assert partial.read_bytes() != b'crash-partial-output'


@pytest.mark.parametrize('action', ['pause', 'archive'])
@pytest.mark.parametrize('capacity', [1, 2])
def test_stopped_batch_keeps_capacity_and_other_batch_uses_only_spare_slot(chain, action, capacity):
    chain.limits.update(render=capacity, max_active_children=capacity)
    a = chain.batch(); chain.enqueue(a); chain.tick(); chain.finish('fetch', a)
    ajob, process, _ = chain.one('render', a)
    control.change(w.folder(a['id']), action)
    assert chain.table.rows[process.pid]['state'] == 'T'
    b = chain.batch('101461', 'bob'); chain.enqueue(b); chain.tick()
    if capacity == 1:
        assert set(chain.owned) == {ajob['id']}
        assert not any(drama == '101461' for drama, _ in chain.fetch_calls)
    else:
        chain.finish('fetch', b)
        assert len(chain.owned) == 2 and chain.one('render', b)
        chain.finish('render', b)
        assert w.runtime(w.folder(b['id']))['status'] == 'review'
    assert chain.table.rows[process.pid]['state'] == 'T'
    assert ajob['id'] in chain.reservations()


def test_enabled_false_preserves_persisted_delayed_poll_protocol(chain):
    batch = chain.batch(); chain.review(batch); chain.approve(batch); chain.tick(); chain.finish('upload', batch)
    before = next(j for j in web.stored_jobs() if j.get('phase') == 'poll')
    chain.limits['enabled'] = False
    web.recover_jobs(enqueue=True); chain.tick()
    after = read_json(web.JOBS / (before['id'] + '.json'))
    assert after['phase'] == 'poll' and after['command'] == before['command'] and after['not_before'] == before['not_before']
    assert not chain.owned and not chain.reservations()
    chain.due(batch); chain.finish('poll', batch)
    assert len(chain.poll_calls) == 1


@pytest.mark.parametrize('original_proof', ['valid', 'missing', 'crash_before_result', 'readonly_local_failure', 'no_admission', 'tampered', 'other_binding'])
def test_controlled_verification_resolves_only_proven_original_job_for_old_rollback(chain, monkeypatch, ops, tmp_path, original_proof):
    from unittest.mock import Mock
    from test_linux_release import Engine, initialize, release
    batch = chain.batch(); chain.review(batch); chain.approve(batch); chain.tick(); chain.finish('upload', batch)
    path = w.folder(batch['id'])
    rid = read_json(path / 'confirmed.json')['requests'][0]['request_id']
    entry = read_json(path / 'publications.json')[rid]
    entry['publication']['oss_url'] = 'https://fixture.invalid/video'
    w.store_publication(path, rid, entry)
    def lost_reply(record_id):
        publish.delivery_receipt({'record_id': record_id, 'material_id': '123'})
        raise TimeoutError('sync reply lost')
    monkeypatch.setattr(publish, 'sync_record_once', lost_reply)
    chain.due(batch)
    original, process, _ = chain.one('poll', batch)
    assert w.poll(batch['id'], rid) == 2
    evidence_path = stage_results.result_path(original, web.JOBS)
    if original_proof != 'crash_before_result':
        stage_results.complete(original, web.JOBS, w.BATCHES, w.ROOT, 2)
    evidence_before = evidence_path.read_bytes() if evidence_path.exists() else None
    process.code = -9 if original_proof == 'crash_before_result' else 2
    chain.table.rows.pop(process.pid); chain.tick()
    assert read_json(web.JOBS / (original['id'] + '.json'))['status'] == 'unverified'
    if original_proof in {'missing', 'no_admission'}:
        evidence_path.unlink()
        if original_proof == 'no_admission':
            original.pop('input_fingerprint', None); web.save(original)
    elif original_proof in {'tampered', 'other_binding'}:
        evidence = read_json(evidence_path)
        evidence['publication']['intent']['receipt']['material_id'] = 'other-material'
        if original_proof == 'other_binding':
            evidence['evidence_sha256'] = json_sha256({key: value for key, value in evidence.items() if key != 'evidence_sha256'})
        write_json_atomic(evidence_path, evidence)
        evidence_before = evidence_path.read_bytes()
    deployment = tmp_path / 'deployment'
    initialize(ops, deployment); release(ops, deployment, 'old')
    (deployment / 'data').rmdir(); (deployment / 'data').symlink_to(w.ROOT, target_is_directory=True)
    engine = Engine()
    with pytest.raises(RuntimeError, match='阶段'):
        ops.transition(deployment, 'old', engine=engine, timeout=0)
    command = w.queue_verify_delivery(batch['id'], rid, entry['approved_sha256'])
    persisted = read_json(path / 'publications.json')[rid]
    local_failure = None
    if original_proof == 'readonly_local_failure':
        local_failure = {'id': uuid.uuid4().hex, 'batch_id': batch['id'], 'request_id': rid,
                         'action': 'verify-delivery', 'phase': 'poll', 'status': 'running',
                         'verification_only': persisted['verification_only']}
        web.finish_job(local_failure, 2, local_failure=True)
        assert local_failure['status'] == 'unverified'
        assert not stage_results.result_path(local_failure, web.JOBS).exists()
        command = w.queue_verify_delivery(batch['id'], rid, entry['approved_sha256'])
        persisted = read_json(path / 'publications.json')[rid]
    job = {'id': uuid.uuid4().hex, 'batch_id': batch['id'], 'request_id': rid,
           'action': 'verify-delivery', 'owner': 'alice', 'status': 'queued', 'created': web.time.time(),
           'command': command, 'instance': instance_marker(), 'verification_only': persisted['verification_only']}
    web.prepare_staged_job(job); web.save(job); web.WORK.put((job, job['command']))
    feishu = Mock(); feishu.get_record.return_value = {'record_id': entry['publication']['feishu_record_id'],
        'fields': {'OSS_URL': {'link': 'https://fixture.invalid/video'}, 'ADS素材ID': '123'}}
    cf = Mock(); cf.sync_status.return_value = {'facebook_bm_sync_status': 'ready', 'facebook_bm_video_id': 'saved-video'}
    monkeypatch.setattr(publish, 'FeishuClient', lambda: feishu)
    monkeypatch.setattr(publish, 'CfAdsClient', lambda: cf)
    chain.tick(); completed = chain.finish('poll', batch)
    assert not chain.owned and not chain.reservations()
    assert read_json(path / 'publications.json')[rid]['status'] == 'fbbm_ready'
    resolved = read_json(web.JOBS / (original['id'] + '.json'))
    if original_proof in {'no_admission', 'tampered', 'other_binding'}:
        assert resolved['status'] == 'unverified' and 'resolved_by' not in resolved
        with pytest.raises(RuntimeError, match='阶段'):
            ops.transition(deployment, 'old', engine=engine, timeout=0)
        assert engine.calls == []
        return
    assert resolved['status'] == 'interrupted' and resolved['resolved_by'] == completed['id']
    assert resolved['resolved_at'] and resolved['resolution'] == 'verified_delivery'
    if evidence_path.exists(): assert evidence_path.read_bytes() == evidence_before
    if local_failure:
        recovered = read_json(web.JOBS / (local_failure['id'] + '.json'))
        assert recovered['status'] == 'interrupted' and recovered['resolved_by'] == completed['id']
    assert persisted['verification_history'] == read_json(path / 'publications.json')[rid]['verification_history']
    cf.ingest.assert_not_called(); cf.resync.assert_not_called(); feishu.update.assert_not_called()
    assert ops.transition(deployment, 'old', engine=engine, timeout=0)['status'] == 'ok'


@pytest.mark.parametrize('state', ['S', 'T', 'Z', None])
def test_resolution_keeps_any_living_original_executor_blocked(chain, state):
    batch = chain.batch(); chain.review(batch)
    path = w.folder(batch['id']); rid = batch['assets'][0]['request_id']
    sha = w.result_records(path)[rid]['output_sha256']
    binding = {'record_id': 'original-record', 'material_id': '123', 'approved_sha256': sha,
               'oss_url': 'https://fixture.invalid/original', 'oss_key': 'original-key'}
    entry = {'status': 'fbbm_ready', 'approved_sha256': sha, 'verification_only': binding,
             'publication': {'feishu_record_id': binding['record_id'], 'oss_url': binding['oss_url'],
                             'oss_key': binding['oss_key'], 'sha256': sha},
             'fbbm': {'record_id': binding['record_id'], 'material_id': binding['material_id']},
             'intent': {'status': 'verified'}}
    w.store_publication(path, rid, entry)
    original = {'id': uuid.uuid4().hex, 'batch_id': batch['id'], 'request_id': rid, 'status': 'unverified',
                'action': 'verify-delivery', 'phase': 'poll', 'verification_only': binding,
                'pid': 20000, 'pgid': 20000, 'process_token': 'c' * 32, 'instance': instance_marker()}
    web.save(original)
    if state:
        chain.table.rows[20000] = {'pgid': 20000, 'state': state,
                                  'arguments': ['python', '-X', 'exactcut_process_token=' + 'c' * 32]}
    job = {'id': uuid.uuid4().hex, 'batch_id': batch['id'], 'request_id': rid,
           'action': 'verify-delivery', 'phase': 'poll', 'verification_only': binding}
    web.resolve_verified_delivery(job, {'status': 'done', 'exit_code': 0, 'publication': entry})
    saved = read_json(web.JOBS / (original['id'] + '.json'))
    assert saved['status'] == ('unverified' if state in {'S', 'T'} else 'interrupted')
