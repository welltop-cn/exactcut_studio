import json
from pathlib import Path
import subprocess


def test_project_switch_hides_old_content_and_ignores_out_of_order_responses():
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes={},pending=[],rendered=[];
let paused=0,released=0;
const video={pause(){paused++},removeAttribute(){},querySelectorAll(){return []},load(){released++}};
const ctx=vm.createContext({
 dirty:false,selectedId:'old',current:{id:'old'},batchCache:[],archivedView:false,
 assetKeys:'old',hookPreviews:new Map(),validationDismissed:false,prefetchTimer:0,
 previewObserver:{disconnect(){}},clearTimeout(){},
 $:id=>nodes[id]||(nodes[id]={hidden:false,textContent:'',querySelectorAll:()=>[video]}),
 document:{querySelectorAll:()=>[video]},window:{confirm:()=>true},
 api:(path,data)=>data?Promise.resolve({}):new Promise((resolve,reject)=>pending.push({resolve,reject})),
 renderBatches(){},showPage(name){nodes.page=name},renderEditor(){rendered.push(ctx.current.marker)},
 renderRuntime(){},setTab(){},scheduleSourcePrefetch(){},refreshBatches:async()=>{},
 localStorage:{setItem(){}},history:{replaceState(){}},showEmptyWorkspace(){nodes.page='error'}
});
vm.runInContext(source.slice(source.indexOf('let openingBatchId='),source.indexOf('function isoDate(')),ctx);
const item=marker=>({id:'a',marker,runtime:{status:'draft'},assets:[]});
(async()=>{
 ctx.dirty=true;ctx.window.confirm=()=>false;
 await vm.runInContext("openBatch('a')",ctx);
 assert.equal(pending.length,0);assert.equal(paused,0);assert.equal(ctx.selectedId,'old');
 ctx.window.confirm=()=>true;
 const first=vm.runInContext("openBatch('a')",ctx);
 assert.notEqual(nodes.page,'workspace');
 assert.equal(nodes.page,'empty','old project must be hidden before network completes');
 assert.match(nodes.emptyWorkspaceTitle.textContent,/加载/);
 assert.equal(paused,1);assert.equal(released,1);
 const second=vm.runInContext("openBatch('b')",ctx);
 const third=vm.runInContext("openBatch('a')",ctx);
 pending[2].resolve(item('latest'));await third;
 pending[0].resolve(item('stale'));await first;
 pending[1].reject(Error('stale failure'));await second;
 assert.deepEqual(rendered,['latest']);assert.equal(ctx.current.marker,'latest');
 const failed=vm.runInContext("openBatch('b')",ctx);
 pending[3].reject(Error('offline'));await assert.rejects(failed,/offline/);
 assert.equal(nodes.page,'error');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_poll_does_not_update_a_project_during_or_after_a_new_switch():
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let resolveDetail,calls=0,renders=0;
const old={id:'a',revision:1,runtime:{updated_at:'2026-10-10T00:00:00Z'}};
const ctx=vm.createContext({polling:false,selectedId:'a',current:old,
 openingBatchId:'b',openingBatchRequest:1,busy:new Set(),dirty:false,
 refreshBatches:async()=>{},refreshQueue:async()=>{},prefetchSources:async()=>{},
 api:()=>{calls++;return new Promise(r=>resolveDetail=r)},$:()=>({}),
 renderRuntime(){renders++},renderEditor(){},refreshTiming(){},hookPreviews:new Map()});
vm.runInContext(source.slice(source.indexOf('async function poll('),source.indexOf('function renderSourcePrefetch(')),ctx);
(async()=>{
 await vm.runInContext('poll()',ctx);assert.equal(calls,0);
 ctx.openingBatchId=null;
 const pending=vm.runInContext('poll()',ctx);await new Promise(setImmediate);
 assert.equal(calls,1);
 ctx.openingBatchRequest+=2; // A -> B -> A while the old A poll is pending.
 resolveDetail({...old,revision:2});await pending;
 assert.equal(ctx.current,old);assert.equal(renders,0);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
