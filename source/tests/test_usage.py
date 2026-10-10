"""Read-only accounting uses real schema-1 batches and isolated directories."""
import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exactcut import accounts, storage, workflow
from exactcut.util import read_json, write_json_atomic
from test_web_base_path import app, login, request
from test_workflow import workspace


def put(root, name, size):
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b'x' * size)
    return file


def batch(root, owner, size, name='项目', drama='101460'):
    item = workflow.create_blank(display_name=name)
    path = workflow.folder(item['id'])
    draft = read_json(path / 'draft.json')
    draft['draft']['drama_id'] = drama
    write_json_atomic(path / 'draft.json', draft)
    if owner:
        accounts.assign(path, {'username': owner})
    put(path, 'production/videos/asset.mp4', size)
    return path


def size_of(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def populated(root):
    first = batch(root, 'alice', 5000, '大项目')
    second = batch(root, 'bob', 1000, '小项目')
    put(root, 'sources/101460/EP1.mp4', 100)
    put(root, 'projects/101460/source/videos/EP2.mp4', 200)
    put(root, 'projects/101460/cache/render-base/base.mp4', 300)
    put(root, 'projects/101460/data/episodes/EP1.json', 400)
    put(root, '.web-state/jobs/job.log', 50)
    return first, second


def report(root):
    assert callable(getattr(storage, 'usage_report', None)), 'usage_report is missing'
    return storage.usage_report(root)


def test_usage_aggregates_private_projects_and_shared_drama(workspace):
    first, second = populated(workspace)
    before = {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    result = report(workspace)
    assert result['projects'] == [
        {'batch_id': first.name, 'name': '大项目', 'owner': 'alice', 'drama_id': '101460',
         'bytes': size_of(first), 'assets': 1, 'status': 'draft'},
        {'batch_id': second.name, 'name': '小项目', 'owner': 'bob', 'drama_id': '101460',
         'bytes': size_of(second), 'assets': 1, 'status': 'draft'},
    ]
    assert result['users'] == [
        {'username': 'alice', 'projects': 1, 'bytes': size_of(first)},
        {'username': 'bob', 'projects': 1, 'bytes': size_of(second)},
    ]
    assert result['shared'] == [{'drama_id': '101460', 'sources_bytes': 300,
                                'cache_bytes': 300, 'episodes_bytes': 400,
                                'referenced_by': ['alice', 'bob']}]
    disk = shutil.disk_usage(workspace)
    assert result['totals']['bytes'] == sum(map(len, before.values()))
    assert result['totals']['total_bytes'] == disk.total
    assert result['totals']['free_bytes'] > 0
    assert result['generated_at']
    assert before == {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}


def test_legacy_owner_unreferenced_cache_empty_root_and_symlinks(workspace, tmp_path):
    old = batch(workspace, None, 10)
    # Also cover the batches/<id> layout named in the usage contract.
    (workspace / 'batches').mkdir()
    old.rename(workspace / 'batches' / old.name)
    put(workspace, 'sources/999999/EP1.mp4', 1000)
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    target = put(outside, 'secret', 99999)
    (workspace / 'linked-file').symlink_to(target)
    (workspace / 'sources/888888').symlink_to(outside, target_is_directory=True)
    result = report(workspace)
    assert result['projects'][0]['owner'] == 'admin'
    assert result['users'][0]['username'] == 'admin'
    assert result['shared'][0]['drama_id'] == '999999'
    assert result['shared'][0]['referenced_by'] == []
    assert result['shared'][1]['referenced_by'] == ['admin']
    assert result['totals']['bytes'] == size_of(workspace)
    empty = workspace / 'empty'
    empty.mkdir()
    assert report(empty)['totals']['bytes'] == 0
    assert report(empty)['projects'] == report(empty)['users'] == report(empty)['shared'] == []


def test_usage_cache_expires_without_repeated_walks(workspace, monkeypatch):
    populated(workspace)
    clock = [100.0]
    monkeypatch.setattr(storage.time, 'monotonic', lambda: clock[0])
    walks = []
    walk = storage.os.walk
    def observed(*args, **kwargs):
        walks.append(args[0])
        return walk(*args, **kwargs)
    monkeypatch.setattr(storage.os, 'walk', observed)
    first = report(workspace)
    put(workspace, 'extra', 50)
    assert report(workspace) == first
    clock[0] += 29
    assert report(workspace) == first
    assert len(walks) == 1
    clock[0] += 32
    assert report(workspace)['totals']['bytes'] == first['totals']['bytes'] + 50
    assert len(walks) == 2


def test_usage_concurrent_scan_is_not_reentered(workspace, monkeypatch):
    assert callable(getattr(storage, 'usage_report', None)), 'usage_report is missing'
    entered, release = threading.Event(), threading.Event()
    walk = storage.os.walk
    calls = []
    def slow(*args, **kwargs):
        calls.append(args[0])
        entered.set()
        assert release.wait(5)
        return walk(*args, **kwargs)
    monkeypatch.setattr(storage.os, 'walk', slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(report, workspace)
        try:
            assert entered.wait(5)
            with pytest.raises(storage.UsageUnavailable):
                pool.submit(report, workspace).result(timeout=1)
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5)['totals']['bytes'] == 0


def test_usage_timeout_has_cooldown_and_does_not_return_partial_total(workspace, monkeypatch):
    assert callable(getattr(storage, 'usage_report', None)), 'usage_report is missing'
    clock = [100.0]
    monkeypatch.setattr(storage.time, 'monotonic', lambda: clock[0])
    walks = []
    walk = storage.os.walk
    def slow(*args, **kwargs):
        walks.append(args[0])
        clock[0] += 31
        yield from walk(*args, **kwargs)
    monkeypatch.setattr(storage.os, 'walk', slow)
    with pytest.raises(storage.UsageUnavailable, match='超时'):
        report(workspace)
    with pytest.raises(storage.UsageUnavailable):
        report(workspace)
    assert len(walks) == 1
    clock[0] += 61
    monkeypatch.setattr(storage.os, 'walk', walk)
    assert report(workspace)['totals']['bytes'] == 0


def test_usage_http_admin_only_and_read_only(app, tmp_path):
    first, second = populated(tmp_path)
    assert request(app, 'GET', '/home/api/usage')[0] == 401
    admin = login(app)
    accounts.manage(app.auth_db, 'admin', {'action': 'create', 'username': 'alice',
                                         'name': 'Alice', 'password': 'test-password-alice'})
    status, headers, _ = request(app, 'POST', '/home/api/login',
        {'username': 'alice', 'password': 'test-password-alice'}, {'X-Exactcut-Request': '1'})
    assert status == 200
    assert request(app, 'GET', '/home/api/usage', headers={'Cookie': headers['Set-Cookie'].split(';')[0]})[0] == 403
    status, _, body = request(app, 'GET', '/home/api/usage', headers=admin)
    assert status == 200
    result = json.loads(body)
    assert [p['bytes'] for p in result['projects']] == [size_of(first), size_of(second)]
    assert [u['bytes'] for u in result['users']] == [size_of(first), size_of(second)]
    assert result['shared'][0]['sources_bytes'] == 300
    assert result['shared'][0]['referenced_by'] == ['alice', 'bob']
    assert result['totals']['bytes'] == size_of(tmp_path)
    assert request(app, 'POST', '/home/api/usage', {}, admin)[0] == 404


@pytest.mark.parametrize('filename,value', [('draft.json', []), ('runtime.json', None),
                                           ('access.json', {'unexpected': 'owner'})])
def test_usage_bad_metadata_returns_retryable_error(app, tmp_path, filename, value):
    path = batch(tmp_path, 'alice', 100)
    write_json_atomic(path / filename, value)
    status, _, body = request(app, 'GET', '/home/api/usage', headers=login(app))
    assert status == 503
    assert 'error' in json.loads(body) and 'totals' not in json.loads(body)


def test_usage_multiple_projects_same_owner_counted_once_per_batch(workspace):
    first, second = populated(workspace)
    third = batch(workspace, 'alice', 100, drama='999999')
    result = report(workspace)
    alice = next(u for u in result['users'] if u['username'] == 'alice')
    assert alice == {'username': 'alice', 'projects': 2, 'bytes': size_of(first) + size_of(third)}
    assert len(result['projects']) == 3
    assert result['shared'][0]['referenced_by'] == ['alice', 'bob']
    assert result['shared'][1]['referenced_by'] == ['alice']


def test_usage_busy_api_fails_promptly_while_other_reads_continue(app, tmp_path, monkeypatch):
    headers = login(app)
    entered, release = threading.Event(), threading.Event()
    walk = storage.os.walk
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return walk(*args, **kwargs)
    monkeypatch.setattr(storage.os, 'walk', slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(request, app, 'GET', '/home/api/usage', headers=headers)
        try:
            assert entered.wait(5)
            status, _, body = pool.submit(request, app, 'GET', '/home/api/usage', headers=headers).result(timeout=1)
            assert status == 503 and 'error' in json.loads(body)
            assert pool.submit(request, app, 'GET', '/home/api/me', headers=headers).result(timeout=1)[0] == 200
        finally:
            release.set()
        assert first.result(timeout=5)[0] == 200


def test_usage_frontend_visibility_rendering_sorting_and_read_only():
    base = Path(__file__).resolve().parents[1] / 'exactcut/web'
    html = (base / 'index.html').read_text()
    assert 'id="usageNav"' in html, 'admin usage navigation is missing'
    dialog = html.split('<dialog id="usageDialog">')[1].split('</dialog>')[0]
    assert '清理' not in dialog and '删除' not in dialog
    assert 'id="usageNav" class="header-btn" hidden' in html
    node = shutil.which('node')
    assert node, 'Node is required for frontend acceptance'
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
(async()=>{
for(const role of ['admin','producer']){
 const elements=new Map();
 const get=id=>{if(!elements.has(id))elements.set(id,{hidden:true,textContent:'',innerHTML:'',showModal(){this.open=true},close(){this.open=false}});return elements.get(id)};
 const calls=[];
 const context=vm.createContext({URL,Date,document:{getElementById:get,baseURI:'http://fixture/home/'},
  api:async path=>{calls.push(path);return {role,name:'Fixture'}},restoreWorkspace:async()=>{},poll:async()=>{},toast:()=>{},showEmptyWorkspace:()=>{}});
 vm.runInContext(source.slice(0,source.indexOf('// Show hundredths')),context);
 const startup=source.slice(source.indexOf('(async()=>{try{currentUser='),source.indexOf(';setInterval(poll,3000)'));
 await vm.runInContext('let currentUser;'+startup,context);
 assert.equal(get('usageNav').hidden,role!=='admin');
 vm.runInContext(source.slice(source.indexOf('function renderUsage(')),context);
 context.data={generated_at:'2026-10-10T00:00:00+00:00',totals:{bytes:5000,free_bytes:6000,total_bytes:10000},
  projects:[{name:'Small',owner:'alice',drama_id:'101460',assets:1,bytes:1},{name:'<Big>',owner:'bob',drama_id:'101460',assets:2,bytes:3000}],
  users:[{username:'alice',projects:1,bytes:1},{username:'bob',projects:1,bytes:3000}],
  shared:[{drama_id:'small',sources_bytes:1,cache_bytes:0,episodes_bytes:0,referenced_by:[]},{drama_id:'large',sources_bytes:100,cache_bytes:200,episodes_bytes:300,referenced_by:['<bob>']}]};
 vm.runInContext('renderUsage(data)',context);
 assert(get('usageProjects').innerHTML.indexOf('&lt;Big&gt;')<get('usageProjects').innerHTML.indexOf('Small'));
 assert(get('usageUsers').innerHTML.indexOf('bob')<get('usageUsers').innerHTML.indexOf('alice'));
 assert(get('usageShared').innerHTML.indexOf('large')<get('usageShared').innerHTML.indexOf('small'));
 assert(get('usageShared').innerHTML.includes('&lt;bob&gt;'));
 for(const id of ['usageProjects','usageUsers','usageShared'])assert(!/<button|<input/.test(get(id).innerHTML));
 context.api=async(...args)=>{assert.deepEqual(args,['/api/usage']);return context.data};
 context.operation=async(button,fn)=>fn();
 await get('usageNav').onclick();assert.equal(get('usageDialog').open,true);
 get('closeUsage').onclick();assert.equal(get('usageDialog').open,false);
 context.data.projects=[];context.data.users=[];context.data.shared=[];
 vm.runInContext('renderUsage(data)',context);
 for(const id of ['usageProjects','usageUsers','usageShared'])assert(get(id).innerHTML.includes('暂无'));
}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps((base / 'app.js').read_text()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
