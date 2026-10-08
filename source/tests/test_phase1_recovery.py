import base64
import json
import queue
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest
from exactcut import workflow as w, webserver as web, batch_control, material_download as dl
from exactcut.intake import read_excel
from exactcut.util import write_json_atomic

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates/精确剪辑_FBBM_填写模板.xlsx'

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(w, 'read_excel', read_excel)
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    monkeypatch.setattr(web, 'JOBS', tmp_path / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setattr(batch_control, 'roots', lambda: {})
    monkeypatch.setattr(batch_control, 'reconcile', lambda root: False)
    item = w.import_excel({'filename': 'test.xlsx', 'content': base64.b64encode(TEMPLATE.read_bytes()).decode()})
    return item


def job_for(item, status='queued'):
    return dict(id='a'*32, batch_id=item['id'], action='start', actor='admin', status=status, created=1,
                command=[sys.executable, '-c', 'pass'])


def test_launch_failure_releases_slot_and_next_job_runs(workspace, monkeypatch):
    item = workspace
    w.start_batch(item['id'], 1)
    first = job_for(item)
    second = dict(id='b'*32, status='queued', command=[sys.executable, '-c', 'pass'])
    web.save(first); web.save(second)
    web.WORK.put((first, first['command'])); web.WORK.put((second, second['command']))
    pending, owned = [], {}
    monkeypatch.setattr(web.subprocess, 'Popen', Mock(side_effect=[OSError('fail'), Mock(poll=lambda: None)]))
    web.worker_tick(pending, owned)
    assert json.loads((web.JOBS/(first['id']+'.json')).read_text())['status'] == 'failed'
    assert w.runtime(w.folder(item['id']))['status'] == 'interrupted'
    web.worker_tick(pending, owned)
    assert second['id'] in owned
    assert web.WORK.unfinished_tasks == 1
    for _, _, log in owned.values(): log.close()


def test_restart_and_orphaned_upload_recover_without_duplicate_enqueue(workspace):
    item = workspace; path = w.folder(item['id'])
    w.update(path, 'publishing', 'upload')
    write_json_atomic(path/'publications.json', {'R1': {'status': 'uploading', 'publication': {'feishu_record_id': 'existing'}}})
    job = {**job_for(item, 'running'), 'action': 'publish', 'request_id': 'R1'}
    web.save(job)
    web.recover_jobs(enqueue=True)
    assert web.WORK.empty()
    pub = json.loads((path/'publications.json').read_text())['R1']
    assert pub['status'] == 'publish_failed'
    assert pub['publication']['feishu_record_id'] == 'existing'
    assert w.runtime(path)['status'] == 'review'


def test_archived_queue_does_not_consume_admission_capacity(workspace):
    item = workspace; path = w.folder(item['id'])
    job = job_for(item); web.save(job)
    assert web.queue_size() == 1
    batch_control.write(path, {'archived': True, 'paused': True})
    assert web.queue_size() == 0
    assert (path/'draft.json').exists()


def test_idempotent_http_submission_survives_lost_response(workspace):
    import threading
    import requests
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.password = 'test-password-more-than-24-characters'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = requests.Session(); client.trust_env = False
    client.auth = ('admin', server.password); client.headers['X-Exactcut-Request'] = '1'
    url = f'http://127.0.0.1:{server.server_port}/api/batches/{workspace["id"]}/start'
    data = {'revision': 1, 'submission_token': 'd'*32}
    try:
        one = client.post(url, json=data); assert one.status_code == 202
        two = client.post(url, json=data); assert two.status_code == 200
        assert one.json()['submission']['id'] == two.json()['submission']['id']
        assert web.WORK.unfinished_tasks == 1
        assert len(list(web.JOBS.glob('*.json'))) == 1
        assert client.post(url, json={**data, 'revision': 2}).status_code == 400
        web.recover_jobs(enqueue=False)
        assert web.WORK.unfinished_tasks == 1
    finally: server.shutdown(); server.server_close(); client.close()



def test_restart_adopts_spawned_but_unrecorded_process_without_replay(workspace, monkeypatch):
    item=workspace;job=job_for(item)
    job['command']=[sys.executable,'-u','-m','exactcut.workflow','produce',item['id'],'--instance=test']
    w.update(w.folder(item['id']),'producing','rendering');web.save(job)
    monkeypatch.setattr(batch_control,'roots',lambda:{item['id']:{'command':' '.join(job['command'])}})
    web.recover_jobs(enqueue=True)
    assert web.WORK.empty()
    saved=json.loads((web.JOBS/(job['id']+'.json')).read_text())
    assert saved['status']=='running' and saved['adopted_after_restart']
    w.update(w.folder(item['id']),'review','finished')
    monkeypatch.setattr(batch_control,'roots',lambda:{})
    web.recover_jobs()
    assert json.loads((web.JOBS/(job['id']+'.json')).read_text())['status']=='completed'
    assert w.runtime(w.folder(item['id']))['status']=='review'
