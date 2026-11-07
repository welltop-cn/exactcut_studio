from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess

from exactcut import accounts, workflow
from test_web_base_path import app, login, request


def enter(app, headers, data=None):
    status, _, body = request(app, 'POST', '/home/api/workspace', data or {}, headers)
    assert status == 200, body
    return json.loads(body)


def test_first_visit_is_owned_and_concurrent_visits_create_only_one_draft(app):
    assert request(app, 'POST', '/home/api/workspace', {})[0] == 401
    headers = login(app)
    assert request(app, 'POST', '/home/api/workspace', {}, {'Cookie': headers['Cookie']})[0] == 403
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
    assert request(app, 'POST', f'/home/api/batches/{batch_id}/archive', {}, headers)[0] == 200
    assert request(app, 'POST', f'/home/api/batches/{batch_id}/delete', {'confirmation': batch_id}, headers)[0] == 200
    assert enter(app, headers)['batch_id'] is None
    assert workflow.summaries() == []


def test_accounts_receive_independent_defaults_including_admin(app):
    ids = []
    for username in ('alice', 'bob'):
        password = 'fixture-producer-password'
        accounts.manage(app.auth_db, 'admin', {'username': username, 'name': username, 'password': password})
        status, headers, _ = request(app, 'POST', '/home/api/login',
                                    {'username': username, 'password': password}, {'X-Exactcut-Request': '1'})
        assert status == 200
        auth = {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'}
        batch_id = enter(app, auth)['batch_id']
        assert accounts.owner(workflow.folder(batch_id)) == username
        ids.append(batch_id)
        assert enter(app, auth)['batch_id'] == batch_id
        if username == 'bob':
            assert request(app, 'POST', '/home/api/workspace', {'batch_id': ids[0]}, auth)[0] == 400
    admin_id = enter(app, login(app))['batch_id']
    assert accounts.owner(workflow.folder(admin_id)) == 'admin'
    assert len({*ids, admin_id}) == 3


def test_existing_projects_last_selection_and_deleted_all_do_not_create_defaults(app):
    headers = login(app)
    first = json.loads(request(app, 'POST', '/home/api/batches/blank', {}, headers)[2])['id']
    assert enter(app, headers)['batch_id'] == first
    second = json.loads(request(app, 'POST', '/home/api/batches/blank', {}, headers)[2])['id']
    assert enter(app, headers, {'batch_id': second})['batch_id'] == second
    assert enter(app, headers)['batch_id'] == second
    enter(app, headers, {'batch_id': first})
    assert enter(app, headers)['batch_id'] == first
    assert len(workflow.summaries()) == 2
    for batch_id in (first, second):
        assert request(app, 'POST', f'/home/api/batches/{batch_id}/archive', {}, headers)[0] == 200
        assert request(app, 'POST', f'/home/api/batches/{batch_id}/delete', {'confirmation': batch_id}, headers)[0] == 200
    assert enter(app, headers)['batch_id'] is None
    assert enter(app, headers)['batch_id'] is None
    assert workflow.summaries() == []


def test_linked_project_opens_before_workspace_and_sidebar_finish():
    web = Path(__file__).resolve().parents[1] / 'exactcut/web'
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const start=source.indexOf('async function restoreWorkspace('),end=source.indexOf('\n(async()=>',start);
const id='a'.repeat(32),opened=[],calls=[],nodes={};let finishList;
const ctx=vm.createContext({window:{addEventListener(){}},location:{hash:'#'+id},
 api:async path=>{calls.push(path);if(path==='/api/me')return {name:'Admin',role:'admin'};return new Promise(()=>{})},
 refreshBatches:()=>new Promise(r=>finishList=r),openBatch:async id=>opened.push(id),
 showEmptyWorkspace(){throw Error('project must stay visible')},toast(){},
 $:id=>nodes[id]??={},refreshQueue(){},scheduledPoll(){},setInterval(){},document:{addEventListener(){}}});
vm.runInContext(source.slice(start,end),ctx);
vm.runInContext(source.split('\n').find(line=>line.startsWith('(async()=>{try{const initial=')),ctx);
(async()=>{await new Promise(setImmediate);
 assert.deepEqual(opened,[id],'deep link must open while sidebar/workspace are unresolved');
 assert.deepEqual(calls,['/api/me'],'deep link must not restore an unrelated saved workspace');
 finishList([{id}]);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps((web / 'app.js').read_text()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_workspace_sidebar_failure_does_not_hide_project_and_detail_errors_propagate():
    web = Path(__file__).resolve().parents[1] / 'exactcut/web'
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const start=source.indexOf('async function restoreWorkspace('),end=source.indexOf('\n(async()=>',start);
(async()=>{for(const hash of ['', '#'+ 'a'.repeat(32)]){
 const opened=[],warnings=[],calls=[];let finishList;
 const ctx=vm.createContext({window:{addEventListener(){}},location:{hash},
  api:async path=>{calls.push(path);return {batch_id:'a'.repeat(32)}},
  refreshBatches:()=>new Promise((resolve,reject)=>finishList=reject),
  openBatch:async id=>opened.push(id),showEmptyWorkspace(){throw Error('must keep project visible')},
  toast:message=>warnings.push(message)});
 vm.runInContext(source.slice(start,end),ctx);
 await vm.runInContext('restoreWorkspace()',ctx);
 assert.deepEqual(opened,['a'.repeat(32)],'saved and linked projects must open before sidebar');
 finishList(Error('offline'));await new Promise(setImmediate);assert.equal(warnings.length,1);
 ctx.location.hash='#'+ 'b'.repeat(32);calls.length=0;
 ctx.openBatch=async()=>{throw Object.assign(Error('server unavailable'),{status:503})};
 await assert.rejects(vm.runInContext('restoreWorkspace()',ctx),/server unavailable/);
 assert.deepEqual(calls,[],'server errors must not silently switch to saved project');
 finishList(Error('offline'));await new Promise(setImmediate);
}})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps((web / 'app.js').read_text()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


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
const a='a'.repeat(32),b='b'.repeat(32),missing='c'.repeat(32);
(async()=>{for(const [saved,hash,expected] of [[a,'',a],[a,'#'+b,b],[a,'#missing',a],[a,'#'+missing,a],[null,'',null],[null,'#'+missing,null]]){
const opened=[],calls=[],listeners=new Map();let empty=0;
const ctx=vm.createContext({window:{addEventListener:(event,handler)=>listeners.set(event,handler)},location:{hash},api:async(path,data)=>{calls.push(path);return {batch_id:saved}},refreshBatches:async()=>[{id:a},{id:b}],openBatch:async id=>{if(id===missing)throw Object.assign(Error('missing'),{status:404});opened.push(id)},showEmptyWorkspace:()=>empty++});
vm.runInContext(source.slice(start,end),ctx);await vm.runInContext('restoreWorkspace()',ctx);
assert.equal(typeof listeners.get('hashchange'),'function');
assert.deepEqual(calls,hash==='#'+b?[]:['/api/workspace']);assert.deepEqual(opened,expected?[expected]:[]);assert.equal(empty,expected?0:1);
}})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps((web / 'app.js').read_text()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
