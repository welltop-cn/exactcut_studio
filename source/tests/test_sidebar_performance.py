import json
from unittest.mock import patch

from exactcut import accounts, workflow, webserver
from exactcut.util import read_json, write_json_atomic
from test_web_base_path import app, login, request


def test_sidebar_summary_excludes_history_and_keeps_authorization(app):
    headers = login(app)
    first = json.loads(request(app, 'POST', '/home/api/batches/blank', {}, headers)[2])['id']
    second = workflow.create_blank(display_name='other private project')['id']
    accounts.assign(workflow.folder(second), {'username': 'someone-else'})
    for bid in (first, second):
        path = workflow.folder(bid) / 'runtime.json'
        state = read_json(path)
        state['events'] = [{'at': '2026-10-10T00:00:00Z', 'message': 'history ' + 'x' * 1000}] * 100
        write_json_atomic(path, state)
    assert request(app, 'GET', '/home/api/batches/sidebar')[0] == 401
    status, _, body = request(app, 'GET', '/home/api/batches/sidebar', headers=headers)
    assert status == 200, body
    rows = json.loads(body)['batches']
    assert len(rows) == 2
    fields = {'id', 'filename', 'display_name', 'created_at', 'drama_id', 'drama_title', 'count', 'status', 'archived', 'owner'}
    assert all(set(row) == fields for row in rows)
    assert len(body) < 2000
    assert 'events' in json.loads(request(app, 'GET', '/home/api/batches', headers=headers)[2])['batches'][0]
    with patch.object(workflow, 'runtime', wraps=workflow.runtime) as runtime:
        only_first = workflow.summaries(lambda bid: bid == first, compact=True)
        assert [row['id'] for row in only_first] == [first]
        assert runtime.call_count == 1


def test_compact_summaries_read_each_job_once_and_preserve_derived_status(app):
    for _ in range(3):
        bid = workflow.create_blank()['id']
        path = workflow.folder(bid)
        state = read_json(path / 'runtime.json')
        state.update(request_parallel=True, status='review')
        write_json_atomic(path / 'runtime.json', state)
        write_json_atomic(path / 'confirmed.json', {'requests': [{'request_id': 'r1'}]})
        write_json_atomic(path / 'production/results.json', {'requests': {'r1': {'status': 'qc_passed'}}})
        write_json_atomic(workflow.ROOT / '.web-state/jobs' / (bid + '.json'),
                          {'batch_id': bid, 'phase': 'render', 'request_id': 'r1', 'status': 'queued'})
    with patch.object(workflow, 'read_json', wraps=workflow.read_json) as reads:
        rows = workflow.summaries(compact=True)
        job_reads = [call for call in reads.call_args_list if call.args[0].parent.name == 'jobs']
        assert len(job_reads) == 3
    assert {row['status'] for row in rows} == {'producing'}


def test_list_and_detail_do_not_hold_global_lock_while_sending(app, monkeypatch):
    headers = login(app)
    bid = json.loads(request(app, 'POST', '/home/api/batches/blank', {}, headers)[2])['id']
    original = webserver.Handler.respond
    held = []
    def observe(self, *args, **kwargs):
        held.append(webserver.LOCK.locked())
        return original(self, *args, **kwargs)
    monkeypatch.setattr(webserver.Handler, 'respond', observe)
    for route in ('/home/api/batches', '/home/api/batches/sidebar', '/home/api/batches/' + bid):
        assert request(app, 'GET', route, headers=headers)[0] == 200
    assert held == [False, False, False]
