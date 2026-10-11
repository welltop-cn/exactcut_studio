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
const video={dataset:{},pause(){paused++},removeAttribute(){},querySelectorAll(){return []},load(){released++}};
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
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('const previewRequests='),source.indexOf('function renderAssets(')),ctx);
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
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


def test_visited_project_displays_before_network_and_keeps_edits_during_refresh():
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes={},pending=[],rendered=[];
const ctx=vm.createContext({dirty:false,selectedId:null,current:null,batchCache:[],archivedView:false,
 assetKeys:'',hookPreviews:new Map(),validationDismissed:false,prefetchTimer:0,previewObserver:null,
 clearTimeout(){},$:id=>nodes[id]||(nodes[id]={querySelectorAll:()=>[]}),window:{confirm:()=>true},
 api:(path,data)=>data?Promise.resolve({}):new Promise((resolve,reject)=>pending.push({path,resolve,reject})),
 renderBatches(){},showPage(name){nodes.page=name},renderEditor(){rendered.push(ctx.current.id)},
 renderRuntime(){},setTab(){},scheduleSourcePrefetch(){},refreshBatches:async()=>{},
 localStorage:{setItem(){}},history:{replaceState(){}},showEmptyWorkspace(){nodes.page='error'}
});
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('const previewRequests='),source.indexOf('function renderAssets(')),ctx);
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('let openingBatchId='),source.indexOf('function isoDate(')),ctx);
const item=(id,revision=1)=>({id,revision,draft:{text:'saved'},runtime:{status:'draft'},assets:[]});
(async()=>{
 let opening=vm.runInContext("openBatch('a')",ctx);pending[0].resolve(item('a'));await opening;
 opening=vm.runInContext("openBatch('b')",ctx);pending[1].resolve(item('b'));await opening;
 opening=vm.runInContext("openBatch('a')",ctx);
 assert.equal(nodes.page,'workspace','visited project must display without waiting for network');
 assert.equal(ctx.current.id,'a');assert.equal(rendered.at(-1),'a');
 ctx.current.draft.text='unsaved edit';ctx.dirty=true;
 pending[2].resolve(item('a',2));await opening;
 assert.equal(ctx.current.draft.text,'unsaved edit');assert.equal(ctx.current.revision,1);
 assert.equal(ctx.dirty,true,'revalidation must not discard edits or bypass revision conflicts');
 ctx.dirty=false;
 await vm.runInContext("warmBatch('c')",ctx); // Starts in background; does not await network.
 assert.equal(pending.length,4);
 opening=vm.runInContext("openBatch('c')",ctx);
 assert.equal(pending.length,4,'click must reuse the hover request');
 pending[3].resolve(item('c'));await opening;
 assert.equal(ctx.current.id,'c');
 opening=vm.runInContext("openBatch('a')",ctx);
 ctx.current=item('a',3);ctx.current.draft.text='saved while refreshing';
 pending[4].resolve(item('a',2));await opening;
 assert.equal(ctx.current.revision,3);assert.equal(ctx.current.draft.text,'saved while refreshing');
 opening=vm.runInContext("openBatch('c')",ctx);pending[5].reject(Error('deleted'));await assert.rejects(opening);
 assert.equal(nodes.page,'error','failed revalidation must hide inaccessible cached data');
 vm.runInContext("for(let i=0;i<12;i++)rememberBatch({id:'cache-'+i})",ctx);
 assert.equal(vm.runInContext('batchDetails.size',ctx),8,'cache memory must be bounded');
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
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
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


def test_slow_admin_sidebar_does_not_delay_selected_project_refresh():
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let finishList,renders=0;
const old={id:'a',revision:1,runtime:{updated_at:'2026-10-10T00:00:00Z'}};
const ctx=vm.createContext({polling:false,selectedId:'a',current:old,
 openingBatchId:null,openingBatchRequest:1,busy:new Set(),dirty:false,savedDraftBaseline:null,
 refreshBatches:()=>new Promise(r=>finishList=r),refreshQueue:async()=>{},prefetchSources:async()=>{},
 api:async()=>({...old,runtime:{updated_at:'2026-10-10T00:00:01Z'}}),$:()=>({}),
 renderRuntime(){renders++},renderEditor(){},refreshTiming(){},hookPreviews:new Map()});
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('async function poll('),source.indexOf('function renderSourcePrefetch(')),ctx);
(async()=>{
 const pending=vm.runInContext('poll()',ctx);await new Promise(setImmediate);
 assert.equal(renders,1,'selected project must update while admin sidebar is still pending');
 finishList();await pending;
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_rework_opens_editor_without_starting_or_copying_managed_project():
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    handler = next(line for line in source.splitlines() if line.startswith("for(const id of ['cloneButton','reworkButton'])"))
    script = r'''
const assert=require('node:assert/strict'),nodes={},calls=[];
const $=id=>nodes[id]??={id};let dirty=false,selectedId='original',current={id:'original',media_managed:true},tab='progress';
const operation=async(button,fn)=>fn(),toast=()=>{};
const api=async(path,data)=>{calls.push({path,data});return {id:data.purpose==='repair'?'original':'copy',media_managed:true}};
const openBatch=async id=>{selectedId=id;current={id,media_managed:true};tab=id==='original'?'progress':'editor'};
const setTab=value=>tab=value;
''' + handler + r'''
(async()=>{
 await nodes.reworkButton.onclick();assert.equal(tab,'editor');assert.equal(current.id,'original');
 assert.equal(calls.length,1);assert.equal(calls[0].data.purpose,'repair');
 await nodes.cloneButton.onclick();assert.equal(current.id,'copy');assert.equal(tab,'editor');
 assert.equal(calls[1].data.purpose,'copy');assert.ok(!calls.some(c=>c.path.endsWith('/start')));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_runtime_revision_does_not_rebuild_unchanged_editor():
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    code = next(line for line in source.splitlines() if line.startswith('async function refreshSelectedBatch('))
    script = r'''
const assert=require('node:assert/strict');let renders=0;
let current={id:'a',media_managed:true,revision:1,draft:{text:'saved'},runtime:{updated_at:'2026-10-11T00:00:00Z'}};
let savedDraftBaseline={id:'a',draft:JSON.stringify(current.draft)},selectedId='a',openingBatchId=null,openingBatchRequest=1,dirty=false;
const busy=new Set(),hookPreviews=new Map();let validationDismissed=false;
let response=structuredClone(current);response.revision=2;
const api=async()=>structuredClone(response),renderRuntime=()=>{},refreshTiming=()=>{},renderEditor=()=>renders++;
const rememberSavedDraft=item=>savedDraftBaseline={id:item.id,draft:JSON.stringify(item.draft)};
''' + code + r'''
(async()=>{
 await refreshSelectedBatch();assert.equal(renders,0);assert.equal(current.revision,2);
 response.revision=3;response.draft.text='external change';await refreshSelectedBatch();assert.equal(renders,1);
 dirty=true;current.draft.text='unsaved';response.revision=4;response.draft.text='another edit';await refreshSelectedBatch();
 assert.equal(current.draft.text,'unsaved');assert.equal(current.revision,3);assert.equal(renders,1);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
