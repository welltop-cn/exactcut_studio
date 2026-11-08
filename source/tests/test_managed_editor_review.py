"""Browser regressions for editing while managed workers advance revisions."""
import pytest
from pathlib import Path
import subprocess

SOURCE = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()


def run(script):
    subprocess.run(['node', '-e', "const assert=require('node:assert/strict');\n" + script], check=True, capture_output=True, text=True)


def test_worker_revision_preserves_dirty_draft_and_concurrent_editor_fence():
    refresh = next(line for line in SOURCE.splitlines() if line.startswith('async function refreshSelectedBatch('))
    run("""
let current={id:'a',media_managed:true,revision:4,draft:{text:'my unsaved edit'},runtime:{updated_at:'2026-10-11T01:00:00Z'}};
let savedDraftBaseline={id:'a',draft:JSON.stringify({text:'saved original'})};
let selectedId='a',openingBatchId=null,openingBatchRequest=0,dirty=true;
const busy=new Set(),renderRuntime=()=>{};
let result={id:'a',media_managed:true,revision:5,draft:{text:'saved original'},runtime:{updated_at:'2026-10-11T01:01:00Z'},assets:[]};
const api=async()=>result;
""" + refresh + """
(async()=>{
await refreshSelectedBatch();
assert.equal(current.revision,5);
assert.equal(current.draft.text,'my unsaved edit');
result={...result,revision:6,draft:{text:'another editor saved this'}};
await refreshSelectedBatch();
assert.equal(current.revision,5);
assert.equal(current.draft.text,'my unsaved edit');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_restore_asset_does_not_discard_dirty_edits():
    handler = next(line for line in SOURCE.splitlines() if line.startswith("$('requests').addEventListener('click'") and "data-restore-asset" in line)
    run("""
let dirty=true,current={id:'a',draft:{text:'unsaved edit'}},handler,called=false,notice='';
const $=()=>({addEventListener:(kind,callback)=>{handler=callback}});
const operation=()=>{called=true},toast=message=>{notice=message};
""" + handler + """
handler({target:{closest:selector=>selector==='[data-restore-asset]'?{dataset:{restoreAsset:'deleted'}}:null}});
assert.equal(called,false);
assert.equal(current.draft.text,'unsaved edit');
assert.ok(notice.includes('保存'));
""")


from test_workflow import workspace
from test_material_versions import enrolled


@pytest.mark.integration
def test_managed_review_preserves_duration_and_public_hook_details(workspace):
    m, path, state = enrolled(media=True)
    record = next(iter(state['versions'].values()))['record']
    record['qc'] = {'expected_duration_ms': 12345}
    record.pop('planned_duration_ms', None)
    record['hook_emphasis'] = {
        'source': 'ai_semantic', 'reason': '强调转折', 'private_debug': 'private',
        'highlights': [{'phrase': '转折', 'role': 'turn', 'internal_trace': 'private'}],
    }
    m._write(path, state)
    asset = m.detail(path)['assets'][0]
    assert asset['planned_duration_ms'] == 12345
    assert asset['hook_emphasis'] == {
        'source': 'ai_semantic', 'reason': '强调转折',
        'highlights': [{'phrase': '转折', 'role': 'turn'}],
    }
    record['planned_duration_ms'] = 14000
    m._write(path, state)
    assert m.detail(path)['assets'][0]['planned_duration_ms'] == 14000


def test_dirty_publish_retry_and_clone_require_save():
    lines = SOURCE.splitlines()
    retry = next(line for line in lines if line.startswith("$('retryButton').onclick"))
    clone = next(line for line in lines if line.startswith("for(const id of ['cloneButton','reworkButton'])"))
    publish = next(line for line in lines if line.startswith("$('assets').addEventListener('click'"))
    run("""
const elements={},$=id=>elements[id]??={addEventListener:(event,handler)=>{elements[id].handler=handler}};
let dirty=true,current={id:'a',media_managed:true,draft:{text:'unsaved'},assets:[{request_id:'r'}]},calls=0,notices=[];
const api=async()=>{calls++;throw Error('must not submit')},toast=msg=>notices.push(msg),operation=(button,fn)=>fn();
""" + retry + '\n' + clone + '\n' + publish + """
(async()=>{
await $('retryButton').onclick();
await $('cloneButton').onclick();
await $('reworkButton').onclick();
$('assets').handler({target:{closest:selector=>selector==='[data-publish]'?{dataset:{publish:'0'}}:null}});
await Promise.resolve();
assert.equal(calls,0);
assert.equal(notices.length,4);
assert.equal(current.draft.text,'unsaved');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_managed_editor_is_locked_during_mutation_request():
    controls = next(line for line in SOURCE.splitlines() if line.startswith('function controls()'))
    operation = next(line for line in SOURCE.splitlines() if line.startswith('async function operation('))
    run("""
const elements={},$=id=>elements[id]??={id,textContent:'save'},input={dataset:{meta:'author'}};
const document={querySelectorAll:selector=>selector.startsWith('#editorTab')?[input]:[]};
const busy=new Set(),activeStatuses=[],hasDraftData=()=>true,renderAssets=()=>{},focusValidationIssue=()=>{};
let dirty=true,current={media_managed:true,draft:{text:'unsaved'},runtime:{status:'review'},assets:[],errors:[]};
""" + controls + '\n' + operation + """
(async()=>{
await operation($('saveButton'),async()=>{
assert.equal(input.disabled,true);
assert.equal($('addRow').disabled,true);
});
assert.equal(input.disabled,false);
assert.equal(dirty,true);
assert.equal(current.draft.text,'unsaved');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_restore_response_does_not_replace_newly_selected_project():
    handler = next(line for line in SOURCE.splitlines() if line.startswith("$('requests').addEventListener('click'") and 'data-restore-asset' in line)
    run("""
let dirty=false,current={id:'a',draft:{text:'original'}},selectedId='a',handler,finish,pending;
const $=()=>({addEventListener:(kind,callback)=>{handler=callback}});
const operation=(button,fn)=>{pending=fn()},toast=()=>{},api=()=>new Promise(resolve=>{finish=resolve});
const renderEditor=()=>{},renderRuntime=()=>{},rememberSavedDraft=()=>{};
""" + handler + """
(async()=>{
handler({target:{closest:selector=>selector==='[data-restore-asset]'?{dataset:{restoreAsset:'deleted'}}:null}});
selectedId='b';current={id:'b',draft:{text:'new project'}};
finish({id:'a',draft:{text:'restored'}});await pending;
assert.equal(current.id,'b');assert.equal(current.draft.text,'new project');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


@pytest.mark.integration
def test_managed_projection_redacts_events_and_current_delivery_errors(workspace):
    from exactcut.util import read_json, write_json_atomic
    m, path, state = enrolled(media=True)
    vid, version = next(iter(state['versions'].items()))
    rid = version['manifest']['requests'][0]['request_id']
    version['record']['base_cache_reused'] = True
    version['record']['stage_seconds'] = {'base_cut': 2}
    secret_error = 'timeout https://host/path?signature=private_signed_value access_token=private_token_value'
    state['events'] = [{'at': '2026-01-01', 'message': secret_error}]
    runtime = read_json(path / 'runtime.json')
    runtime['events'] = [{'at': '2026-01-02', 'message': 'Bearer private_bearer_value'}]
    write_json_atomic(path / 'runtime.json', runtime)
    state['deliveries']['delivery'] = {'delivery_id': 'delivery', 'asset_id': rid, 'version_id': vid,
        'status': 'publish_failed', 'publication': {'error': secret_error, 'stage_seconds': {'upload': 3}}}
    m._write(path, state)
    detail = m.detail(path)
    asset = detail['assets'][0]
    assert asset['base_cache_reused'] is True
    assert asset['stage_seconds'] == {'base_cut': 2, 'upload': 3}
    assert asset['error'] and asset['error_info']['code'] == 'network'
    assert 'private_' not in str(asset['error_info']) + asset['error'] + str(detail['runtime']['events'])
    state['deliveries']['delivery']['version_id'] = 'older-version'
    m._write(path, state)
    asset = m.detail(path)['assets'][0]
    assert asset['error'] == ''
    assert asset['error_info'] is None
    assert asset['stage_seconds'] == {'base_cut': 2}


def test_late_mutation_responses_respect_new_selection():
    handlers = '\n'.join(line for line in SOURCE.splitlines() if line.startswith((
        "$('retryButton').onclick", "for(const id of ['cloneButton','reworkButton'])",
        "$('assets').addEventListener('click'", "$('previewButton').onclick", "$('cancelStart').onclick")))
    run("""
const elements={},$=id=>elements[id]??={addEventListener:(event,handler)=>{elements[id].handler=handler}};
let dirty=false,current,selectedId,finish,pending,rendered=0,opened=0;
const api=()=>new Promise(resolve=>{finish=resolve}),toast=()=>{},operation=(button,fn)=>{pending=fn();return pending};
const renderRuntime=()=>{rendered++},renderEditor=()=>{rendered++},openBatch=async()=>{opened++};
""" + handlers + """
(async()=>{
for(const action of ['retryButton','cloneButton','reworkButton','previewButton','startNow','publish']){
current={id:'a',media_managed:true,revision:4,runtime:{status:'review'},assets:[{request_id:'r'}]};selectedId='a';
if(action==='publish')$('assets').handler({target:{closest:s=>s==='[data-publish]'?{dataset:{publish:'0'}}:null}});
else $(action).onclick();
selectedId='b';finish({id:'a',revision:5});await pending;
assert.equal(current.revision,4,action);assert.equal(rendered,0,action);assert.equal(opened,0,action);
}
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_existing_previews_remain_counted_while_one_candidate_is_rendering():
    render = SOURCE[SOURCE.index('function runtimeMessage('):SOURCE.index('const previewRequests=')]
    execution = next(line for line in SOURCE.splitlines() if line.startswith('function executionAsset('))
    run("""
const elements={},$=id=>elements[id]??={textContent:'',innerHTML:'',hidden:false};
const document={querySelector:()=>$('reviewTab')};
const renderSourcePrefetch=()=>{},renderRequestProgress=()=>{},renderAssets=()=>{},controls=()=>{},setErrorHtml=()=>{};
const statusLabel=s=>s,badgeClass=()=>'',materialMessage=s=>s||'',esc=s=>s,observationText=()=>'',eventMarkup=()=>'',operationEvents=()=>[];
const EditorBusy={message:(s,fallback='')=>s||fallback};
let current={media_managed:true,filename:'网页填写',feedback_count:0,runtime:{status:'producing',download_complete:true,events:[]},assets:[
{request_id:'r1',preview:'/preview/r1',status:'qc_passed',candidate:{status:'processing',stage:'base_cut'}},
{request_id:'r2',preview:'/preview/r2',status:'qc_passed'}]};
""" + execution + '\n' + render + """
renderRuntime();
assert.equal($('reviewTab').textContent,'成片预览 (2)');
assert.equal($('reviewCount').textContent,'2 条可预览 · 0 条反馈');
assert.equal($('previewReadyTitle').textContent,'2 条成片可在线播放');
assert.equal($('previewReady').hidden,false);
assert.equal($('progressCount').textContent,'1/2 条制作完成 · 1 条制作中');
current.assets[1].production_scope='reused';renderRuntime();
assert.equal($('progressCount').textContent,'0/1 条制作完成 · 1 条沿用已有成片 · 1 条制作中');
assert.ok($('steps').innerHTML.includes('class="step active">03<b>制作与预览'));
current.assets=[current.assets[0]];renderRuntime();
assert.equal($('previewReady').hidden,false);
assert.equal($('reviewTab').textContent,'成片预览 (1)');
assert.equal($('progressCount').textContent,'0/1 条制作完成 · 1 条制作中');
current.assets=[{request_id:'r2',preview:'/old',production_scope:'reused'}];renderRuntime();
assert.equal($('progressCount').textContent,'1 条沿用已有成片，本轮无需制作');
""")
