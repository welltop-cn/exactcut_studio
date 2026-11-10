"""Run the actual upload click -> operation -> API -> refresh flow in Node.

Only network and DOM rendering are substituted; async requests are controlled by
these tests so the busy/response/navigation interleavings are deterministic.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


HARNESS = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const id='b'.repeat(32),other='a'.repeat(32),pending=[],messages=[],nodes={};
const clone=x=>JSON.parse(JSON.stringify(x));
const detail=(revision=7)=>({id,media_managed:true,media_delivery_enabled:true,revision,draft:{rows:[{request_id:'r',text:'saved'}]},
 runtime:{updated_at:`2026-11-10T10:00:${String(revision).padStart(2,'0')}Z`,status:'review'},
 assets:[{request_id:'r',version_id:'v',sha256:'sha',preview:'video',status:'qc_passed'},
 {request_id:'s',version_id:'w',sha256:'sha2',preview:'video2',status:'qc_passed'}],feedback:[]});
const $=key=>nodes[key]??={id:key,addEventListener(name,fn){this[name]=fn}};
const storage=new Map();let tokens=0;
const ctx=vm.createContext({console,AbortController,DOMException,TypeError,Date,Map,Set,Promise,
 current:detail(),selectedId:id,dirty:false,batchCache:[],busy:new Set(),savedDraftBaseline:null,
 openingBatchId:null,openingBatchRequest:1,hookPreviews:new Map(),validationDismissed:false,
 appUrl:x=>x,randomId:()=>String(tokens++).padStart(32,'c'),
 sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 fetch:(path,options)=>new Promise((resolve,reject)=>pending.push({path,options,resolve,reject})),
 setTimeout:()=>0,clearTimeout(){},location:{replace(){}},
 EditorBusy:{error:(r,b)=>Object.assign(Error(b.error),{status:r.status})},
 $,toast:(message,error)=>messages.push({message,error}),controls(){},renderAssets(){},renderRuntime(){},
 renderEditor(){},refreshTiming(){},focusValidationIssue(){},pollInterval:()=>0,
 });
// Load complete production functions, not replicas of the conflict logic.
vm.runInContext(source.slice(source.indexOf('function isRevisionConflict('),source.indexOf('function positionToast(')),ctx);
vm.runInContext(source.slice(source.indexOf('async function operation('),source.indexOf('function hasDraftIdentity(')),ctx);
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('async function refreshSelectedBatch('),source.indexOf('function renderSourcePrefetch(')),ctx);
const publishStart=source.indexOf('const publishSubmissions=');
if(publishStart>=0)vm.runInContext(source.slice(publishStart,source.indexOf("$('assets').addEventListener('click'",publishStart)),ctx);
vm.runInContext(source.split('\n').find(line=>line.startsWith("$('assets').addEventListener('click'")),ctx);
vm.runInContext('rememberSavedDraft(current)',ctx);
const flush=async()=>{for(let i=0;i<15;i++)await new Promise(setImmediate)};
const reply=(request,body,status=200)=>request.resolve({ok:status<400,status,json:async()=>clone(body)});
const click=(index=0)=>{const button={dataset:{publish:String(index)},textContent:'确认上传',disabled:false};
 nodes.assets.click({target:{closest:selector=>selector==='[data-publish]'?button:null}});return button};
'''


def run_flow(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = HARNESS + '\n(async()=>{\n' + body + r'''
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path = Path(__file__).parents[1] / 'exactcut/web/app.js'
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('change', [
    "ctx.dirty=true;ctx.current.draft.rows[0].text='typing'",
    "ctx.openingBatchRequest+=2",
    "ctx.current.assets[1].version_id='new'",
    "ctx.current.assets[1].status='unverified'",
    "ctx.current.runtime.paused=true",
    "ctx.current.media_delivery_enabled=false",
])
def test_queued_upload_rechecks_click_identity_and_state(change):
    run_flow(r'''
click(0);click(1);await flush();ctx.current.revision=9;
''' + change + r''';
reply(pending[0],detail(8));await flush();
assert.equal(pending.filter(p=>p.options.method==='POST').length,1);
assert.ok(messages.some(m=>m.error),'queued intent must explain why it was stopped');
''')


@pytest.mark.parametrize('failure', ['network', 'server', 'client'])
def test_receipt_storage_clear_and_retain_boundaries(failure):
    respond = {
        'network': "pending[0].reject(new TypeError('offline'))",
        'server': "reply(pending[0],{error:'failed'},503)",
        'client': "reply(pending[0],{error:'rejected'},400)",
    }[failure]
    expected_same = 'false' if failure == 'client' else 'true'
    run_flow(r'''
click();await flush();const original=JSON.parse(pending[0].options.body);
''' + respond + r''';await flush();
assert.equal(pending.length,1,'failure must never automatically POST again');
ctx.current=detail(8);click();await flush();const retry=JSON.parse(pending[1].options.body);
assert.equal(retry.submission_token===original.submission_token,''' + expected_same + r''');
assert.equal(retry.revision,''' + ('8' if failure == 'client' else '7') + r''');
reply(pending[1],detail(9));await flush();assert.equal(storage.size,0,'success clears only completed submission');
''')


def test_unknown_receipt_does_not_redirect_other_asset_or_new_version():
    run_flow(r'''
click();await flush();const first=JSON.parse(pending[0].options.body);
pending[0].reject(new TypeError('offline'));await flush();
click(1);await flush();const second=JSON.parse(pending[1].options.body);
assert.equal(second.request_id,'s');assert.notEqual(second.submission_token,first.submission_token);
reply(pending[1],detail(8));await flush();
assert.equal(storage.size,1,'another material success must retain unknown receipt');
ctx.current.assets[0].version_id='new';click();await flush();
const replacement=JSON.parse(pending[2].options.body);
assert.equal(replacement.version_id,'new');assert.equal(replacement.revision,8);
assert.notEqual(replacement.submission_token,first.submission_token);
reply(pending[2],detail(9));await flush();
assert.equal(storage.size,1,'new version success must retain old identity receipt');
''')


def test_unknown_receipt_replays_same_payload_after_project_revision_changes():
    run_flow(r'''
click();await flush();const original=JSON.parse(pending[0].options.body);
pending[0].reject(new TypeError('lost response'));await flush();
ctx.current=detail(8);click();await flush();
assert.deepEqual(JSON.parse(pending[1].options.body),original,'explicit retry must query the same idempotent transaction');
reply(pending[1],detail(9));await flush();
assert.equal(pending.length,2,'no automatic retries');
''')


def test_success_receipt_does_not_replace_new_selection_or_unsaved_external_draft():
    run_flow(r'''
click();await flush();ctx.selectedId=other;ctx.current={...detail(10),id:other};
reply(pending[0],detail(8));await flush();
assert.equal(ctx.current.id,other);assert.equal(ctx.current.revision,10);
''')
    run_flow(r'''
click();await flush();ctx.dirty=true;ctx.current.draft.rows[0].text='typing';
const changed=detail(8);changed.draft.rows[0].text='other tab';
reply(pending[0],changed);await flush();
assert.equal(ctx.current.draft.rows[0].text,'typing');assert.equal(ctx.current.revision,7);
assert.equal(ctx.dirty,true);
''')


def test_duplicate_click_is_rechecked_after_first_receipt():
    run_flow(r'''
click();click();await flush();assert.equal(pending.length,1);
const result=detail(8);result.assets[0].status='upload_queued';
reply(pending[0],result);await flush();
assert.equal(pending.length,1);assert.ok(messages.some(m=>m.error));
''')


def test_success_receipt_preserves_edits_made_while_waiting():
    run_flow(r'''
click();await flush();ctx.dirty=true;ctx.current.draft.rows[0].text='typing';
reply(pending[0],detail(8));await flush();
assert.equal(ctx.current.draft.rows[0].text,'typing');assert.equal(ctx.dirty,true);
assert.equal(ctx.current.revision,8);
''')


@pytest.mark.parametrize('scenario', ['network', 'navigation', 'external-draft', 'other-write', 'newer-state'])
def test_conflict_never_claims_sync_when_refresh_fails_or_is_not_applied(scenario):
    setup = {
        'network': "pending[1].reject(new TypeError('offline'))",
        'navigation': "ctx.openingBatchRequest+=2;reply(pending[1],detail(8))",
        'external-draft': "ctx.dirty=true;ctx.current.draft.rows[0].text='typing';const changed=detail(8);changed.draft.rows[0].text='external';reply(pending[1],changed)",
        'other-write': "vm.runInContext(\"api.pendingWrites.add({id:selectedId})\",ctx);reply(pending[1],detail(8))",
        'newer-state': "ctx.current=detail(10);reply(pending[1],detail(8))",
    }[scenario]
    run_flow(r'''
click();await flush();reply(pending[0],{error:'项目已在其他页面更新，请刷新后重试'},409);await flush();
''' + setup + r''';await flush();
assert.ok(messages.length);assert.doesNotMatch(messages[0].message,/已同步最新状态/);
assert.equal(pending.filter(p=>p.options.method==='POST').length,1);
''')


def test_uploads_wait_only_for_prior_submission_receipt():
    run_flow(r'''
click(0);click(1);await flush();
assert.equal(pending.length,1,'second upload must await the first receipt');
const result=detail(8);result.assets[0].status='upload_queued';
reply(pending[0],result);await flush();
assert.equal(pending.length,2,'must submit next without waiting for file transfer');
assert.equal(JSON.parse(pending[1].options.body).request_id,'s');
assert.equal(JSON.parse(pending[1].options.body).revision,8);
reply(pending[1],detail(9));await flush();
''')


def test_upload_conflict_waits_for_applied_refresh_while_operation_busy():
    run_flow(r'''
click();await flush();assert.equal(pending.length,1);
reply(pending[0],{error:'项目已在其他页面更新，请刷新后重试'},409);await flush();
assert.equal(pending.length,2);
assert.equal(messages.length,0,'must not claim synced before the GET finishes');
assert.equal(ctx.busy.size,1,'upload operation must still own its busy guard');
reply(pending[1],detail(8));await flush();
assert.equal(ctx.current.revision,8,'forced conflict refresh must actually apply');
assert.match(messages[0].message,/已同步最新状态/);
assert.equal(ctx.busy.size,0);
assert.equal(pending.filter(p=>p.options.method==='POST').length,1,'no automatic retry');
''')
