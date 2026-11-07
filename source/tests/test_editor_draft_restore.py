"""Full render snapshots preserve unselected segments through real editor handlers."""
import shutil
import subprocess
from pathlib import Path

from test_cutting_api import ready
from test_phase2_preview_api import fixture

import pytest


def test_render_draft_and_unselected_segments_survive_generation():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = r'''
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const section=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end));
const elements={},$=id=>elements[id]??={disabled:false,pause(){},removeAttribute(){},load(){}};
const store=new Map(),localStorage={getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v)};
const key=()=> 'targets',draftKey=()=> 'draft',oldDraftKey=()=> 'old';
let savedRaw=null,legacyRaw=null,oldDraftRaw=null,targets=[],deletions=[],manualClips=[],aiEvents=[];
let ignoredEvents=[],drawingEvents=[],clipEdits={},clipEffects={},effect={},legacyDraft=false;
let savedIgnoredEvents=[],savedDrawingEvents=[],savedAiEvents=[],savedClipEdits={},savedManualClips=[];
let savedClipEffects={},savedEffect={},savedTargets=[],savedDeletions=[],editing=null;
const context={session:{source_sha256:'fixture',metadata:{frame_count:1000}}};
const meta=()=>context.session.metadata,regionId=()=> 'uid',simplified=e=>({...e});
const simpleEffect=()=>true,validBox=()=>true,checkedReport=r=>r;
const belongs=(t,e)=>t.eventId===e.id,eventRange=e=>clipEdits[e.id]||e;
const allEvents=()=>[...aiEvents,...manualClips],legacyJob=()=>false;
const clearRemovedClip=()=>{},paint=()=>{},status=()=>{};
eval(section('  const validShape=', '  function pausePlayback('));
eval(section('  const editorDraft=', '  function validBox(').replace('const editorDraft=', 'var editorDraft='));
eval(section('  function restoreParameters(', '  async function batchRequest('));
eval(section('  function timelineItems(', '  function timelineTitle('));
eval(section('  function pendingCount(', '  function paintBatch('));
const original={schema_version:1,
 aiEvents:Array.from({length:20},(_,i)=>({id:'ai-'+i,start:i*10,end:i*10+8,label:'建议'+i,reason:'检查',status:'needs_review',candidates:[]})),
 manualClips:[{id:'manual-one',start:300,end:320,label:'自定义名字',reason:'手工',status:'needs_review',candidates:[]},
 {id:'manual-two',start:330,end:350,label:'另一名字',status:'needs_review',candidates:[]}],
 targets:[{eventId:'ai-0',uid:'saved',start:0,end:8,seed_frame:0,x:1,y:1,w:5,h:5,motion:'fixed',label:'区域名字',reason:'手工',adopted:true},
 {eventId:'ai-4',uid:'inactive',start:40,end:48,seed_frame:40,x:1,y:1,w:5,h:5,motion:'fixed',label:'未选区域',reason:'保留',adopted:false}],
 deletions:[{eventId:'ai-1',start:10,end:18}],ignoredEvents:['ai-2'],drawingEvents:['ai-3'],
 clipEdits:{'ai-5':{start:51,end:57}},clipEffects:{'ai-0':{style:'solid'}},effect:{style:'pixel'}};
// Legacy ignored segments become unselected, preserving their ranges and regions.
const normalized={...structuredClone(original),ignoredEvents:[]};
restoreParameters({editor_draft:original});
assert.deepStrictEqual(editorDraft(),normalized);
assert.deepStrictEqual(original.ignoredEvents,['ai-2']);
assert.equal(timelineItems().length,22);assert.equal(pendingCount(),20);
assert(save());assert.deepStrictEqual(JSON.parse(store.get('draft')),normalized);
// Locked job must restore its own snapshot even if another local draft exists.
store.set('draft','invalid local draft');restoreParameters({editor_draft:original});
assert.deepStrictEqual(editorDraft(),normalized);
let batchJob={id:'previous'},resultBefore=true,shown=0,submitted=0,confirmed=[];
const selectedBox=()=>null,showResult=()=>shown++,batchRequest=()=>submitted++;
const window={confirm:message=>(confirmed.push(message),false)};
eval(section("  $('mosaicBatchStart').onclick=", "  $('mosaicBatchView').onclick="));
$('mosaicBatchStart').onclick();
assert.deepStrictEqual(confirmed,[]);
assert.equal(batchJob,null);assert.equal(resultBefore,false);assert.equal(shown,1);assert.equal(submitted,1);
assert.deepStrictEqual(editorDraft(),normalized);
// Unselected segments remain in the draft and are summarized without a confirmation.
assert(processingSummary(1,pendingCount()).includes('不处理 20 段'));
deletions=allEvents().filter(e=>!targets.some(t=>belongs(t,e)&&t.adopted!==false))
  .map(e=>({eventId:e.id,start:e.start,end:e.end}));
assert.equal(pendingCount(),0);$('mosaicBatchStart').onclick();
assert.equal(confirmed.length,0);assert.equal(submitted,2);
// Leaving an already discarded result keeps the draft and never invokes reset/discard.
(async()=>{
 let mutationBusy=false,epoch=1,closed=0,requests=[];
 const active=()=>true,paintBatch=()=>{},awaitingDecision=()=>false,pendingJob=()=>false;
 const dialog={close:()=>closed++},base=()=>'/fixture';
 let batchToken=0,batchLoading=false,batchError='',resultMode=false;
 const adoptedTargets=()=>targets.filter(t=>t.adopted!==false),deleteRanges=()=>deletions.map(d=>[d.start,d.end]);
 const api=async (url,body)=>{requests.push({url,body});return {status:'completed',discarded_at:'now'};};
 eval(section('  async function batchRequest(', '  function restoreBatch('));
 const expected=structuredClone(editorDraft());await batchRequest(true);
 assert.deepStrictEqual(requests[0].body.editor_draft,expected);requests=[];
 const save=()=>true;
 batchJob={status:'completed',discarded_at:'now'};
 window.confirm=()=>true;
 eval(section("  $('mosaicDiscard').onclick=", "  $('mosaicReplace').onclick="));
 await $('mosaicDiscard').onclick();assert.equal(closed,1);assert.deepStrictEqual(requests,[]);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path = Path(__file__).parents[1] / 'exactcut/web/mosaic.js'
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# Real durable job/session/scan files, with synthetic media only.


@pytest.mark.integration
def test_render_snapshot_survives_discard_and_server_reopen(ready):
    from exactcut import mosaic_session as sessions, mosaic_render as render
    from exactcut import mosaic_scan, cutting_scan
    from exactcut.util import read_json, write_json_atomic

    bid, rid, sha, _ = ready
    session = sessions.open_session(bid, rid, sha)
    sid = session['id']
    root = sessions.session_path(bid, sid)
    events = [dict(id=f'ai-{i}', start=i*3, end=i*3+2, label=f'建议{i}',
                   reason='检查', status='needs_review', candidates=[]) for i in range(20)]
    draft = dict(schema_version=1, targets=[], deletions=[dict(eventId='ai-0', start=0, end=2)],
                 aiEvents=events, ignoredEvents=['ai-1'], drawingEvents=['ai-2'],
                 manualClips=[dict(id=f'manual-{i}', start=65+i*3, end=67+i*3,
                                   label=f'手动名字{i}', status='needs_review', candidates=[]) for i in range(2)],
                 clipEdits={'ai-3': dict(start=10, end=12)}, clipEffects={}, effect=None)
    scan = dict(id='a'*32, status='completed', source_sha256=sha, reset_at='', events=events,
                identity=mosaic_scan.identity(session, cutting_scan.settings()))
    write_json_atomic(root/'scan.json', scan)
    job = render.start(bid, sid, [], '', ranges=[[0, 2]], editor_draft=draft)
    path = root/f"render-{job['id']}.json"
    write_json_atomic(path, {**read_json(path), 'status': 'completed'})
    discarded = render.discard_result(bid, sid, job['id'], '')
    assert discarded['discarded_at']
    assert render.latest(bid, sid)['job']['editor_draft'] == draft
    assert sessions.read_session(bid, sid)['reset_at'] == ''
    assert read_json(root/'scan.json') == scan
    assert mosaic_scan.state(bid, sid)['report']['events'] == events
