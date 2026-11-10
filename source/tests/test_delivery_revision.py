"""Public managed delivery transactions against isolated synthetic media."""
import copy

import pytest

from test_material_versions import enrolled, token, enable_fixture_delivery
from test_workflow import workspace


pytestmark = pytest.mark.integration


def approval(state, rid=None):
    rid = rid or state['draft']['rows'][0]['request_id']
    vid = state['assets'][rid]['current_version']
    return {'revision': state['revision'], 'mutation_id': token(), 'request_id': rid,
            'version_id': vid, 'sha256': state['versions'][vid]['record']['output_sha256']}


def test_delivery_accepts_project_progress_but_save_keeps_strict_cas(workspace):
    m, path, state = enrolled(media=True)
    data = approval(state)
    draft = copy.deepcopy(state['draft'])
    saved = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    delivered = m.queue_delivery(path, data)
    assert len(delivered['deliveries']) == 1
    assert delivered['revision'] == saved['revision'] + 1
    assert m.queue_delivery(path, data)['mutation'] == delivered['mutation']
    with pytest.raises(ValueError, match='更新'):
        m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})


def test_hash_then_file_replacement_is_rejected(workspace, monkeypatch):
    m, path, state = enrolled(media=True)
    data = approval(state)
    real_hash = m.file_sha256
    def replace_after_hash(file):
        digest = real_hash(file)
        replacement = file.with_suffix('.replacement')
        replacement.write_bytes(b'replaced after hash')
        replacement.replace(file)
        return digest
    monkeypatch.setattr(m, 'file_sha256', replace_after_hash)
    with pytest.raises(ValueError, match='成片'):
        m.queue_delivery(path, data)
    assert not m.read(path)['deliveries']


@pytest.mark.parametrize('revision', [None, True, 1.0, '1', -1, 0, 999999])
def test_delivery_rejects_invalid_or_future_revision(workspace, revision):
    m, path, state = enrolled(media=True)
    data = {**approval(state), 'revision': revision}
    with pytest.raises(ValueError, match='更新'):
        m.queue_delivery(path, data)
    assert not m.read(path)['deliveries']


@pytest.mark.parametrize('control', ['pause', 'archive', 'delete'])
def test_stale_approval_cannot_bypass_project_control(workspace, control):
    m, path, state = enrolled(media=True)
    data = approval(state)
    m.control(path, control, {'revision': state['revision'], 'mutation_id': token()})
    with pytest.raises(ValueError, match='暂停|删除|归档'):
        m.queue_delivery(path, data)
    assert not m.read(path)['deliveries']


def test_two_concurrent_approvals_create_only_one_delivery(workspace):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from exactcut.cutting import CutError
    m, path, state = enrolled(media=True)
    barrier = Barrier(2)
    def submit():
        data = approval(state)
        barrier.wait(timeout=5)
        try:
            return m.queue_delivery(path, data)
        except (ValueError, CutError) as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    final = m.read(path)
    assert len(final['deliveries']) == len(final['outbox']) == 1


def test_hash_then_current_version_switch_rejects_original_intent(workspace, monkeypatch):
    m, path, state = enrolled(media=True)
    data = approval(state)
    real_hash = m.file_sha256
    def switch_after_hash(file):
        digest = real_hash(file)
        with m.locked(path):
            latest = m.read(path)
            old = latest['versions'][data['version_id']]
            latest['versions']['new-version'] = {**copy.deepcopy(old), 'version_id': 'new-version'}
            latest['assets'][data['request_id']]['current_version'] = 'new-version'
            latest['revision'] += 1
            m.durable_write(path / 'media-state.json', latest)
        return digest
    monkeypatch.setattr(m, 'file_sha256', switch_after_hash)
    with pytest.raises(ValueError, match='版本'):
        m.queue_delivery(path, data)
    assert not m.read(path)['deliveries']


def test_outbox_uses_version_rechecked_inside_transaction(workspace, monkeypatch):
    m, path, state = enrolled(media=True)
    data = approval(state)
    real_hash = m.file_sha256
    def advance_metadata_after_hash(file):
        digest = real_hash(file)
        with m.locked(path):
            latest = m.read(path)
            latest['versions'][data['version_id']]['generation'] = 42
            latest['revision'] += 1
            m.durable_write(path / 'media-state.json', latest)
        return digest
    monkeypatch.setattr(m, 'file_sha256', advance_metadata_after_hash)
    delivered = m.queue_delivery(path, data)
    assert next(iter(delivered['outbox'].values()))['generation'] == 42


@pytest.mark.parametrize('status', ['pending', 'upload_queued', 'uploading', 'fbbm_queued', 'unverified', 'fbbm_ready'])
def test_existing_delivery_blocks_new_token_but_original_receipt_replays(workspace, status):
    m, path, state = enrolled(media=True)
    data = approval(state)
    delivered = m.queue_delivery(path, data)
    with m.locked(path):
        latest = m.read(path)
        next(iter(latest['deliveries'].values()))['status'] = status
        latest['revision'] += 1
        m.durable_write(path / 'media-state.json', latest)
    replay = m.queue_delivery(path, data)
    assert replay['mutation'] == delivered['mutation']
    assert len(replay['deliveries']) == len(replay['outbox']) == 1
    with pytest.raises(ValueError, match='交付'):
        m.queue_delivery(path, {**data, 'mutation_id': token()})
    with pytest.raises(ValueError, match='幂等'):
        m.queue_delivery(path, {**data, 'revision': latest['revision']})


def test_edited_draft_keeps_existing_current_media_delivery_semantics(workspace):
    m, path, state = enrolled(media=True)
    data = approval(state)
    draft = copy.deepcopy(state['draft'])
    draft['rows'][0]['hook_text'] = 'saved next draft'
    saved = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    assert saved['assets'][data['request_id']]['current_version'] == data['version_id']
    delivered = m.queue_delivery(path, data)
    assert next(iter(delivered['deliveries'].values()))['version_id'] == data['version_id']


def test_other_asset_submission_and_worker_progress_do_not_invalidate_approval(workspace):
    from test_material_versions import candidate_job, rendered
    m, path, state = enrolled(media=True)
    first = approval(state)
    draft = copy.deepcopy(state['draft'])
    draft['rows'].append({**copy.deepcopy(draft['rows'][0]), 'request_id': None, 'seq': '2'})
    state = m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
    state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
    worker, child, event, job = candidate_job(path, state)
    worker.stage_enter(job)
    rendered(child, event)
    worker.stage_finish(job, 0)
    state = m.read(path)
    second = approval(state, event['asset_id'])
    m.queue_delivery(path, first)
    final = m.queue_delivery(path, second)
    assert len(final['deliveries']) == 2
    assert {d['asset_id'] for d in final['deliveries'].values()} == {first['request_id'], second['request_id']}


def test_filename_binding_still_rejects_different_content(workspace):
    m, path, state = enrolled(media=True)
    data = approval(state)
    delivered = m.queue_delivery(path, data)
    with m.locked(path):
        latest = m.read(path)
        previous = next(iter(latest['deliveries'].values()))
        previous['status'] = 'publish_failed'
        old = latest['versions'][data['version_id']]
        new = {**copy.deepcopy(old), 'version_id': token()}
        master = path / 'production/videos/replacement.mp4'
        master.write_bytes(b'different content under same output name')
        new['master_path'] = str(master)
        new['record']['output_sha256'] = m.file_sha256(master)
        latest['versions'][new['version_id']] = new
        latest['assets'][data['request_id']]['current_version'] = new['version_id']
        latest['revision'] += 1
        m.durable_write(path / 'media-state.json', latest)
    with pytest.raises(ValueError, match='文件名冲突'):
        m.queue_delivery(path, approval(latest))
    assert len(m.read(path)['deliveries']) == len(delivered['deliveries']) == 1


def test_http_publish_keeps_auth_origin_ownership_and_environment_guards(workspace, monkeypatch):
    import threading
    import requests
    from http.server import ThreadingHTTPServer
    from exactcut import accounts, webserver as web
    m, path, state = enrolled(media=True)
    data = approval(state)
    db = workspace / 'accounts.sqlite3'
    password = 'isolated-admin-password-for-test'
    accounts.initialize(db, password)
    accounts.manage(db, 'admin', {'username': 'other', 'name': 'Other', 'password': 'isolated-other-password'})
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.password, server.auth_db = password, db
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    url = base + f'/api/batches/{path.name}/publish'
    try:
        with requests.Session() as admin, requests.Session() as other:
            headers = {'X-Exactcut-Request': '1'}
            assert admin.post(base+'/api/login', headers=headers, json={'username': 'admin', 'password': password}).status_code == 200
            assert other.post(base+'/api/login', headers=headers, json={'username': 'other', 'password': 'isolated-other-password'}).status_code == 200
            assert requests.post(url, json=data).status_code == 401
            assert admin.post(url, json=data).status_code == 403
            assert other.post(url, json=data, headers=headers).status_code == 404
            monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '0')
            assert admin.post(url, json=data, headers=headers).status_code == 503
            monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
            monkeypatch.setenv('EXACTCUT_ALLOW_PUBLISH', '0')
            assert admin.post(url, json=data, headers=headers).status_code == 400
            assert not m.read(path)['deliveries']
            # Isolated acceptance: permit enqueue only; no worker is started.
            checked = []
            monkeypatch.setattr(web, 'require_publish', lambda: checked.append(True))
            draft = copy.deepcopy(state['draft'])
            m.save_draft(path, {'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
            response = admin.post(url, json=data, headers=headers)
            assert response.status_code == 202, response.text
            assert len(response.json()['mutation']['delivery_ids']) == 1
            assert checked == [True]
            replay = admin.post(url, json=data, headers=headers)
            assert replay.status_code == 202
            assert replay.json()['mutation'] == response.json()['mutation']
            stale_save = admin.post(base+f'/api/batches/{path.name}/save', headers=headers,
                                    json={'revision': state['revision'], 'mutation_id': token(), 'draft': draft})
            assert stale_save.status_code == 409
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert not thread.is_alive()
