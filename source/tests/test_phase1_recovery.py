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
    monkeypatch.setattr(batch_control, 'reconcile', lambda root, **kwargs: False)
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
    # Popen below is a launcher stub, so process discovery must use the same fake surface.
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({}))
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
    item = workspace
    job = job_for(item)
    job['process_token'] = 'a'*32
    path = w.folder(item['id'])
    w.update(path, 'producing', 'rendering')
    web.save(job)
    batch_control.remember_child(path, {'pid': 4321, 'pgid': 4321, 'process_token': 'a'*32, 'lane': 'start', 'request_id': None})
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({4321: {
        'pgid': 4321, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'a'*32]}}))
    web.recover_jobs(enqueue=True)
    assert web.WORK.empty()
    saved = json.loads((web.JOBS / (job['id'] + '.json')).read_text())
    assert saved['status'] == 'running' and saved['adopted_after_restart'] and saved['pid'] == 4321
    w.update(path, 'review', 'finished')
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({}))
    web.recover_jobs()
    assert json.loads((web.JOBS / (job['id'] + '.json')).read_text())['status'] == 'completed'
    assert w.runtime(path)['status'] == 'review'


def test_restart_adopts_recorded_process_without_replay(workspace, monkeypatch):
    item=workspace;job=job_for(item, 'running')
    job.update(pid=4321, pgid=4321, process_token='a'*32, instance=batch_control.instance_marker())
    w.update(w.folder(item['id']),'producing','rendering');web.save(job)
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({4321: {
        'pgid': 4321, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + 'a'*32]}}))
    web.recover_jobs(enqueue=True)
    assert web.WORK.empty()
    saved=json.loads((web.JOBS/(job['id']+'.json')).read_text())
    assert saved['status']=='running' and saved['adopted_after_restart']
    w.update(w.folder(item['id']),'review','finished')
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({}))
    web.recover_jobs()
    assert json.loads((web.JOBS/(job['id']+'.json')).read_text())['status']=='completed'
    assert w.runtime(w.folder(item['id']))['status']=='review'


def test_restart_adopts_launch_before_pid_or_child_was_saved(workspace, monkeypatch):
    item = workspace
    token = 'b'*32
    job = {**job_for(item), 'process_token': token, 'instance': batch_control.instance_marker()}
    web.save(job)
    path = w.folder(item['id'])
    w.update(path, 'producing', 'rendering')
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({4321: {
        'pgid': 4321, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + token]}}))
    web.recover_jobs(enqueue=True)
    assert web.WORK.empty()
    saved = json.loads((web.JOBS/(job['id']+'.json')).read_text())
    assert saved['status'] == 'running' and saved['pid'] == 4321
    assert batch_control.recorded_children(path)[0]['process_token'] == token


def test_restart_keeps_other_publish_request_queued(workspace, monkeypatch):
    item = workspace
    path = w.folder(item['id'])
    token = 'b'*32
    running = {**job_for(item, 'running'), 'action': 'publish', 'request_id': 'R1',
               'pid': 4321, 'pgid': 4321, 'process_token': token, 'instance': batch_control.instance_marker()}
    queued = {**job_for(item), 'id': 'b'*32, 'action': 'publish', 'request_id': 'R2', 'created': 2}
    web.save(running); web.save(queued)
    batch_control.remember_child(path, {**running, 'lane': 'publish'})
    w.update(path, 'publishing', 'uploading')
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({4321: {
        'pgid': 4321, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + token]}}))
    web.recover_jobs(enqueue=True)
    assert web.WORK.get_nowait()[0]['id'] == queued['id']
    assert json.loads((web.JOBS/(queued['id']+'.json')).read_text())['status'] == 'queued'


def test_restart_rebuilds_control_for_saved_running_job(workspace, monkeypatch):
    item = workspace
    path = w.folder(item['id'])
    token = 'c'*32
    job = {**job_for(item, 'running'), 'pid': 4321, 'pgid': 4321,
           'process_token': token, 'instance': batch_control.instance_marker()}
    web.save(job)
    w.update(path, 'producing', 'rendering')
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({4321: {
        'pgid': 4321, 'state': 'S', 'arguments': ['python', '-X', 'exactcut_process_token=' + token]}}))
    web.recover_jobs(enqueue=True)
    assert web.WORK.empty()
    assert batch_control.own_live_children(path)[0]['process_token'] == token


def test_real_launch_can_be_recovered_without_saved_pid(workspace, monkeypatch):
    token = 'd'*32
    child = subprocess.Popen([sys.executable, '-X', 'exactcut_process_token=' + token,
                              '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        job = {**job_for(workspace), 'process_token': token, 'instance': batch_control.instance_marker()}
        web.save(job)
        path = w.folder(workspace['id'])
        w.update(path, 'producing', 'rendering')
        web.recover_jobs(enqueue=True)
        assert web.WORK.empty()
        saved = json.loads((web.JOBS/(job['id']+'.json')).read_text())
        assert saved['pid'] == child.pid and saved['status'] == 'running'
        assert batch_control.own_live_children(path)[0]['pid'] == child.pid
    finally:
        child.terminate()
        child.wait(timeout=5)
