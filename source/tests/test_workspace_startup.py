from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess

from exactcut import accounts, workflow
from test_web_base_path import app, login, request


def enter(app, headers, data=None):
    status, _, body = request(app, 'POST', '/exactcut/api/workspace', data or {}, headers)
    assert status == 200, body
    return json.loads(body)


def test_first_visit_is_owned_and_concurrent_visits_create_only_one_draft(app):
    assert request(app, 'POST', '/exactcut/api/workspace', {})[0] == 401
    headers = login(app)
    assert request(app, 'POST', '/exactcut/api/workspace', {}, {'Cookie': headers['Cookie']})[0] == 403
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: enter(app, headers), range(6)))
    batch_id = results[0]['batch_id']
    assert all(result['batch_id'] == batch_id for result in results)
    assert len(workflow.summaries()) == 1
    item = workflow.detail(batch_id)
    assert item['display_name'] == '默认项目'
    assert item['runtime']['status'] == 'draft'
    assert len(item['draft']['rows']) == 1 and len(item['draft']['rows'][0]['clips']) == 1
    assert accounts.owner(workflow.folder(batch_id)) == 'admin'
    assert not list((workflow.ROOT / '.web-state/jobs').glob('*.json'))
    assert request(app, 'POST', f'/exactcut/api/batches/{batch_id}/archive', {}, headers)[0] == 200
    assert request(app, 'POST', f'/exactcut/api/batches/{batch_id}/delete', {'confirmation': batch_id}, headers)[0] == 200
    assert enter(app, headers)['batch_id'] is None
    assert workflow.summaries() == []


def test_accounts_receive_independent_defaults_including_admin(app):
    ids = []
    for username in ('alice', 'bob'):
        password = 'fixture-producer-password'
        accounts.manage(app.auth_db, 'admin', {'username': username, 'name': username, 'password': password})
        status, headers, _ = request(app, 'POST', '/exactcut/api/login',
                                    {'username': username, 'password': password}, {'X-Exactcut-Request': '1'})
        assert status == 200
        auth = {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}
        batch_id = enter(app, auth)['batch_id']
        assert accounts.owner(workflow.folder(batch_id)) == username
        ids.append(batch_id)
        assert enter(app, auth)['batch_id'] == batch_id
        if username == 'bob':
            assert request(app, 'POST', '/exactcut/api/workspace', {'batch_id': ids[0]}, auth)[0] == 400
    admin_id = enter(app, login(app))['batch_id']
    assert accounts.owner(workflow.folder(admin_id)) == 'admin'
    assert len({*ids, admin_id}) == 3


def test_existing_projects_last_selection_and_deleted_all_do_not_create_defaults(app):
    headers = login(app)
    first = json.loads(request(app, 'POST', '/exactcut/api/batches/blank', {}, headers)[2])['id']
    assert enter(app, headers)['batch_id'] == first
    second = json.loads(request(app, 'POST', '/exactcut/api/batches/blank', {}, headers)[2])['id']
    assert enter(app, headers, {'batch_id': second})['batch_id'] == second
    assert enter(app, headers)['batch_id'] == second
    enter(app, headers, {'batch_id': first})
    assert enter(app, headers)['batch_id'] == first
    assert len(workflow.summaries()) == 2
    for batch_id in (first, second):
        assert request(app, 'POST', f'/exactcut/api/batches/{batch_id}/archive', {}, headers)[0] == 200
        assert request(app, 'POST', f'/exactcut/api/batches/{batch_id}/delete', {'confirmation': batch_id}, headers)[0] == 200
    assert enter(app, headers)['batch_id'] is None
    assert enter(app, headers)['batch_id'] is None
    assert workflow.summaries() == []


def test_frontend_has_no_import_entry_and_restores_server_selection():
    web = Path(__file__).resolve().parents[1] / 'exactcut/web'
    html = (web / 'index.html').read_text()
    assert 'id="excel"' not in html and 'template.xlsx' not in html and 'id="intake"' not in html
    assert '制作在服务器后台继续' in html
    node = shutil.which('node')
    if not node:
        import pytest
        pytest.skip('Node unavailable; covered on host')
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const start=source.indexOf('async function restoreWorkspace('),end=source.indexOf('\n(async()=>',start);
assert(start>=0&&end>start);
(async()=>{for(const [saved,hash,expected] of [['a','','a'],['a','#b','b'],['a','#missing','a'],[null,'',null]]){
const opened=[],calls=[];let empty=0;
const ctx=vm.createContext({location:{hash},api:async(path,data)=>{calls.push(path);return {batch_id:saved}},refreshBatches:async()=>[{id:'a'},{id:'b'}],openBatch:async id=>opened.push(id),showEmptyWorkspace:()=>empty++});
vm.runInContext(source.slice(start,end),ctx);await vm.runInContext('restoreWorkspace()',ctx);
assert.deepEqual(calls,['/api/workspace']);assert.deepEqual(opened,expected?[expected]:[]);assert.equal(empty,expected?0:1);
}})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps((web / 'app.js').read_text()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
