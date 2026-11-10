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
const detail=(revision=7)=>({id,media_managed:true,revision,draft:{rows:[{request_id:'r',text:'saved'}]},
 runtime:{updated_at:`2026-11-10T10:00:${String(revision).padStart(2,'0')}Z`,status:'review'},
 assets:[{request_id:'r',version_id:'v',sha256:'sha',preview:'video',status:'qc_passed'},
 {request_id:'s',version_id:'w',sha256:'sha2',preview:'video2',status:'qc_passed'}],feedback:[]});
const $=key=>nodes[key]??={id:key,addEventListener(name,fn){this[name]=fn}};
const storage=new Map();
const ctx=vm.createContext({console,AbortController,DOMException,TypeError,Date,Map,Set,Promise,
 current:detail(),selectedId:id,dirty:false,batchCache:[],busy:new Set(),savedDraftBaseline:null,
 openingBatchId:null,openingBatchRequest:1,hookPreviews:new Map(),validationDismissed:false,
 appUrl:x=>x,randomId:()=>String(storage.size).padStart(32,'c'),
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
