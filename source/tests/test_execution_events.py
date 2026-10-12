import shutil
import subprocess
from pathlib import Path

from exactcut import batch_control, result_store, workflow as w
from exactcut.util import read_json
from test_workflow import workspace, imported
from test_intra_project_results import two_requests


def test_parallel_stages_and_completion_are_durable_events(workspace):
    path, manifest, rid = two_requests(imported())
    w.update(path, request_parallel=True)
    with w.execution(path, rid, interval=60, phase='render'):
        # The renderer persists its stage before calling the workflow callback.
        result_store.patch(path / 'production', rid, stage='base_cut')
        w.checkpoint(path, 'base_cut', request_id=rid)
        w.checkpoint(path, 'base_cut', request_id=rid, frame=2)
        w.checkpoint(path, 'hook_overlay', request_id=rid)
        for request in manifest['requests']:
            result_store.patch(path / 'production', request['request_id'], status='qc_passed')
    w.update(path)
    messages = [e['message'] for e in read_json(path / 'runtime.json')['events']]
    assert messages.count(f'{rid}：剪辑片段') == 1
    assert f'{rid}：排版与合成 Hook' in messages
    assert f'{rid}：开始制作' in messages
    assert messages.count('制作结束，2 条可预览，0 条失败') == 1


def test_history_keeps_start_after_many_events_and_controls(workspace):
    batch = imported()
    path = w.folder(batch['id'])
    w.update(path, message='项目开始')
    for n in range(210):
        w.update(path, message=f'步骤 {n}')
    control = batch_control.read(path)
    for n in range(55):
        batch_control.write(path, control, f'控制 {n}')
    events = batch_control.present(path, w.runtime(path))['events']
    messages = [e['message'] for e in events]
    assert '项目开始' in messages
    assert '控制 0' in messages
    assert len(events) >= 266


def test_web_renders_all_events_in_time_order_and_escapes_messages():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    line = next(line for line in source.splitlines() if line.startswith("$('events').innerHTML="))
    script = """
const assert=require('node:assert/strict');
const box={}; const $=()=>box;
const esc=s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;');
const r={events:Array.from({length:60},(_,i)=>({at:new Date(1700000000000+i*1000).toISOString(),message:'step-'+i+'<x>'})).reverse()};
""" + "const current=null;\n" + source[source.index("function materialLabel("):source.index("function productionProgress(")] + "\n" + "box.innerHTML=eventMarkup(r.events.sort((a,b)=>a.at.localeCompare(b.at)));" + """
assert.equal((box.innerHTML.match(/class="event"/g)||[]).length,60);
assert.ok(box.innerHTML.indexOf('step-0&lt;')<box.innerHTML.indexOf('step-59&lt;'));
assert.ok(!box.innerHTML.includes('<x>'));
"""
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_new_project_records_creation(workspace):
    batch = imported()
    assert any('项目已创建' in e['message'] for e in batch['runtime']['events'])


def test_production_cards_distinguish_running_queue_pause_and_finished():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    assert 'function productionProgress(' in source
    helper = 'function productionProgress(' + source.split('function productionProgress(', 1)[1].split('\nfunction ', 1)[0]
    script = r'''
const assert=require('node:assert/strict');
const esc=s=>String(s??'').replaceAll('<','&lt;');
const durationLabel=s=>s+' 秒';
const stageNames={base_cut:'剪辑片段',hook_overlay:'字幕合成'};
const observationText=o=>o.step+' · '+o.stage_seconds+' 秒';
''' + "const current=null;\n" + source[source.index("function materialLabel("):source.index("function productionProgress(")] + "\n" + helper + r'''
const running=productionProgress({request_id:'r1',title:'<素材>',status:'processing',stage:'base_cut',observation:{step:'剪辑片段',stage_seconds:80,details:{total:5,completed:2}}},{});
assert.ok(running.includes('value="2" max="5"'));
assert.ok(running.includes('2/5'));
assert.ok(running.includes('&lt;素材>'));
const encoding=productionProgress({request_id:'r1',status:'processing',stage:'hook_overlay',observation:{step:'字幕合成',stage_seconds:180,details:{}}},{});
assert.ok(encoding.includes('已用时'));
assert.ok(encoding.includes('<progress aria-label='));
assert.ok(!encoding.includes('value='));
const ending=productionProgress({request_id:'r1',status:'processing',stage:'ending_overlay'},{});
assert.ok(ending.includes('<span class="current" aria-current="step">静态片尾</span>'));
assert.ok(ending.includes('<span class="reached" >字幕合成</span>'));
assert.ok(ending.includes('<span class="" >质检</span>'));
const queued=productionProgress({request_id:'r2',status:'pending'},{});
assert.ok(queued.includes('等待制作位置'));
assert.ok(!queued.includes('<progress'));
const paused=productionProgress({request_id:'r3',status:'processing',stage:'base_cut'}, {paused:true});
assert.ok(paused.includes('已暂停'));
assert.ok(!paused.includes('<progress'));
const done=productionProgress({request_id:'r4',status:'fbbm_queued',preview:'/preview',timing:{queue_seconds:60,total_seconds:200,complete:true}},{});
assert.ok(done.includes('制作完成'));
assert.ok(done.includes('制作排队 60 秒'));
assert.ok(done.includes('总耗时 200 秒，其中制作排队 60 秒'));
assert.ok(!done.includes('等待 FBBM'));
assert.ok(!done.includes('stage-track'));
assert.ok(!done.includes('成片已就绪，可前往预览'));
const reused=productionProgress({request_id:'r4',preview:'/old',production_scope:'reused',timing:{total_seconds:669,queue_seconds:438,complete:true}},{});
assert.ok(reused.includes('沿用已有成片'));assert.ok(!reused.includes('669'));assert.ok(!reused.includes('438'));
const pending=productionProgress({request_id:'r4',preview:'/old',production_scope:'pending',timing:{total_seconds:669}},{});
assert.ok(pending.includes('配置已修改，待确认制作'));assert.ok(!pending.includes('669'));assert.ok(!pending.includes('制作完成'));
assert.ok(!pending.includes('查看制作过程'));
const fresh=productionProgress({request_id:'new',production_scope:'pending',hook_emphasis:{source:'ai_semantic',highlights:[{phrase:'旧重点'}]}},{});
assert.ok(fresh.includes('待确认制作'));assert.ok(!fresh.includes('配置已修改'));assert.ok(!fresh.includes('仍可预览'));assert.ok(!fresh.includes('旧重点'));assert.ok(!fresh.includes('查看制作过程'));
const rerender=productionProgress({request_id:'r1',hook_emphasis:{source:'ai_semantic',highlights:[{phrase:'旧重点'}]},candidate:{status:'processing',stage:'base_cut'}},{});
assert.ok(!rerender.includes('旧重点'));


'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_material_labels_replace_only_known_whole_ids():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = '\n'.join(line for line in source.splitlines() if line.startswith(('function materialLabel(', 'function materialMessage(')))
    script = """
const assert=require('node:assert/strict');
const current={draft:{rows:[{request_id:'R1',seq:3},{request_id:'R1b',seq:4},{request_id:'R.2',seq:5}]},assets:[]};
""" + helpers + """
assert.equal(materialMessage('R1：开始；R1b：结束；R.2：失败'),'素材3：开始；素材4：结束；素材5：失败');
assert.equal(materialMessage('R1extra other_R1 R12 unknown'),'R1extra other_R1 R12 unknown');
assert.equal(materialLabel('unknown'),'unknown');
"""
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    feedback = next(line for line in source.splitlines() if line.startswith('async function feedbackPage('))
    assert 'materialMessage(e.message)' not in feedback
    assert 'white-space:pre-wrap' in feedback


def test_material_events_group_and_collapse_only_no_progress_repeats():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = source[source.index('function materialLabel('):source.index('function productionProgress(')]
    script = r'''
const assert=require('node:assert/strict');
const current={id:'batch',draft:{rows:[{request_id:'R1'},{request_id:'R1b'}]},assets:[]};
const esc=s=>String(s).replaceAll('<','&lt;');
''' + helpers + r'''
const messages=['项目已创建','准备正片：EP1','等待资源，稍后继续本阶段','准备正片：EP1','等待资源，稍后继续本阶段','R1：剪辑片段','R1b：剪辑片段','R1：剪辑片段','R1：失败 <x>','R1：手动重试','R1：剪辑片段','已下载 EP1','准备正片：EP2'];
const events=messages.map((message,i)=>({message,at:new Date(1700000000000+i*1000).toISOString()}));
assert.deepEqual(displayEvents(events).map(e=>e.message),['项目已创建','准备正片：EP1','等待资源，稍后继续本阶段','已下载 EP1','准备正片：EP2']);
assert.deepEqual(displayEvents(events,'R1').map(e=>e.message),['R1：剪辑片段','R1：失败 <x>','R1：手动重试','R1：剪辑片段']);
assert.equal(displayEvents(events,'R1b').length,1);
assert.equal(events.length,13);
assert.ok(eventMarkup(displayEvents(events,'R1')).includes('&lt;x>'));
assert.ok(!eventMarkup(displayEvents(events)).includes('素材1'));
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_material_event_expansion_survives_refresh_and_opens_on_failure():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = source[source.index('function materialLabel('):source.index('function productionProgress(')]
    script = r'''
const assert=require('node:assert/strict');
let current={id:'batch',assets:[],draft:{rows:[]}};
const esc=s=>String(s??'');
let nodes=[{dataset:{eventRequest:'r1',eventBatch:'batch',failed:'false'},open:true}];
const box={querySelectorAll:()=>nodes,set innerHTML(value){nodes=[{dataset:{eventRequest:'r1',eventBatch:current.id,failed:value},open:value==='true'}]}};
const $=()=>box;
const productionProgress=a=>String(a.status==='failed');
''' + helpers + r'''
renderRequestProgress([{status:'processing'}],{});assert.equal(nodes[0].open,true);
nodes[0].open=false;renderRequestProgress([{status:'failed'}],{});assert.equal(nodes[0].open,true);
nodes[0].open=false;renderRequestProgress([{status:'failed'}],{});assert.equal(nodes[0].open,false);
current.id='other';renderRequestProgress([{status:'processing'}],{});assert.equal(nodes[0].open,false);
assert.ok(materialEventDetails({request_id:'r1',status:'failed'},{events:[]}).includes(' open>'));
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_readable_history_shows_steps_once_in_one_timeline():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = source[source.index('function materialLabel('):source.index('function productionProgress(')]
    script = r'''
const assert=require('node:assert/strict');
const current={id:'b',draft:{rows:[{request_id:'R1',seq:1}]},assets:[]};
const esc=s=>String(s??'').replaceAll('<','&lt;');
''' + helpers + r'''
const messages=['内容已确认，等待下载与制作','准备正片：EP1','R1：开始制作','R1：检查可用空间','R1：AI 理解重点词','R1：剪辑片段','R1：排版与合成 Hook','R1：已完成，待预览','已请求暂停，保留当前进度'];
const events=messages.map((message,i)=>({message,at:new Date(1700000000000+i*1000).toISOString()}));
assert.equal(operationEvents(events).length,2);
const html=materialEventDetails({request_id:'R1',hook_emphasis:{source:'user_none',highlights:[]}}, {events});
const main=html.split('<details class="technical-events"')[0];
assert.ok(main.includes('查看制作过程'));assert.ok(main.includes('添加字幕'));
assert.ok(main.includes('AI 重点：未启用'));assert.ok(!main.includes('素材1：'));
assert.ok(main.includes('检查可用空间'));assert.ok(main.includes('AI 理解重点词'));
assert.ok(!html.includes('技术详情'));assert.equal((html.match(/检查可用空间/g)||[]).length,1);
assert.equal(emphasisSummary({source:'ai_semantic',highlights:[],cache_reused:true}),'AI 重点：复用已有分析，无需强调');
assert.equal(emphasisSummary({source:'ai_semantic',highlights:[]}), 'AI 重点：分析完成，无需强调');
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_current_production_events_do_not_fall_back_to_previous_version():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    helpers = source[source.index('function materialLabel('):source.index('function productionProgress(')]
    script = r'''
const assert=require('node:assert/strict');
const current={id:'b',draft:{rows:[{request_id:'R1',seq:1}]},assets:[]};
const esc=s=>String(s??'');
''' + helpers + r'''
const old={at:'2026-10-11T01:28:48Z',message:'R1：已完成，待预览'};
const fresh={at:'2026-10-11T01:50:42Z',message:'R1：开始制作'};
const html=materialEventDetails({request_id:'R1',production_events:[fresh]}, {events:[old,fresh]});
assert.ok(!html.includes('制作完成，可以预览'));
assert.ok(!html.includes('技术详情'));
assert.ok(!materialEventDetails({request_id:'R1',production_events:[]}, {events:[old]}).includes('制作完成，可以预览'));
assert.ok(materialEventDetails({request_id:'R1'}, {events:[old]}).includes('制作完成，可以预览'));
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
