"""Timeline hit targets and navigation ownership use the real mosaic handlers."""
import shutil
import subprocess
from pathlib import Path

import pytest

from test_mosaic_ui import editor, open_auto, scan_result

@pytest.mark.ui
@pytest.mark.parametrize('length', [1, 45])
@pytest.mark.parametrize('kind,label', [
    ('suggestion', 'AI 建议'), ('mosaic', '打码'),
    ('deletion', '删除'), ('ignored', '忽略'),
])
def test_short_blocks_keep_tooltips_and_clickable_width(editor, length, kind, label):
    scan_result(editor)
    editor.evaluate('''length => {
      session.metadata.frame_count=7200;
      scanJob.events=Array.from({length:12},(_,i)=>({...scanJob.events[0],
        id:'e'+(i+1),start:1830+i*90,end:1830+i*90+length,seed_frame:1830+i*90}));
    }''', length)
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    if kind != 'suggestion':
        editor.locator('[data-mosaic-card="e1"]').get_by_role('checkbox', name=label, exact=True).click()
    blocks = editor.locator('[data-timeline-event]')
    assert blocks.count() == 12
    assert blocks.evaluate_all('els=>els.every(el=>el.getBoundingClientRect().width>=8)')
    block = editor.locator('[data-timeline-event="e1"]')
    assert kind in block.get_attribute('class')
    assert label in block.get_attribute('title')
    assert ('1:01.0–1:02.5' if length == 45 else '1:01.0–1:01.0') in block.get_attribute('title')
    assert block.locator('.mosaic-block-label').is_hidden()
    block.click()
    assert editor.locator('[data-scan-event="e1"]').get_attribute('aria-pressed') == 'true'


@pytest.mark.ui
@pytest.mark.parametrize('navigation', ['jump', 'slider', 'next'])
@pytest.mark.parametrize('release', ['before_jump', 'after_jump'])
def test_new_navigation_wins_over_trim_callbacks(editor, navigation, release):
    scan_result(editor)
    editor.evaluate('session.metadata.frame_count=3600')
    open_auto(editor)
    editor.locator('#mosaicScan').click()
    editor.locator('[data-proposal="e1:0"]').click()
    editor.locator('#mosaicFrame').wait_for(state='visible')
    editor.clock.install()
    editor.clock.pause_at(editor.evaluate('Date.now()'))
    editor.evaluate('''() => {
      const video=$('mosaicSource');let time=0;
      Object.defineProperty(video,'duration',{get:()=>120,configurable:true});
      Object.defineProperty(video,'readyState',{get:()=>4,configurable:true});
      Object.defineProperty(video,'currentTime',{get:()=>time,set:v=>{time=v;},configurable:true});
      window.trimCallbacks=[];
      const schedule=window.setTimeout;
      window.setTimeout=(fn,delay,...args)=>{
        if(delay===100)trimCallbacks.push(()=>fn(...args));
        return schedule(fn,delay,...args);
      };
    }''')
    handle = editor.locator('[data-timeline-event="e1"] [data-edge="end"]').bounding_box()
    rail = editor.locator('#mosaicTimelineTracks').bounding_box()
    x, y = handle['x']+handle['width']/2, handle['y']+handle['height']/2
    editor.mouse.move(x, y)
    editor.mouse.down()
    editor.mouse.move(x+rail['width']/120, y)
    if release == 'before_jump':
        editor.mouse.up()
    if navigation == 'jump':
        jump = editor.locator('#mosaicFrameNumber')
        jump.fill('1:05')
        # Callbacks already dequeued when cancelled must leave typing intact.
        editor.evaluate('trimCallbacks.splice(0).forEach(fn=>fn())')
        assert jump.input_value() == '1:05'
        jump.press('Enter')
    elif navigation == 'slider':
        editor.locator('#mosaicSeek').evaluate("el=>{el.value=1950;el.dispatchEvent(new Event('input'));}")
    else:
        editor.locator('#mosaicNext').evaluate('el=>el.click()')
    expected = editor.locator('#mosaicFrameNumber').input_value()
    position = editor.locator('#mosaicSource').evaluate('v=>v.currentTime')
    if navigation != 'next':
        assert expected == '01:05:00'
        assert position == pytest.approx(65+.05/30)
    if release == 'after_jump':
        editor.mouse.move(x+rail['width']/100, y)
        editor.mouse.up()
    editor.evaluate('trimCallbacks.splice(0).forEach(fn=>fn())')
    editor.clock.run_for(500)
    assert editor.locator('#mosaicFrameNumber').input_value() == expected
    assert editor.locator('#mosaicSource').evaluate('v=>v.currentTime') == position


def test_timeline_and_seek_handlers_with_delayed_callbacks():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = r'''
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const section=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end));
const elements={},callbacks=[],frames=[],media=[];
const $=id=>elements[id]??=( {style:{setProperty(){}},value:'',hidden:false} );
const timeline={querySelectorAll:()=>[],hasPointerCapture:()=>false};
const video={duration:120};
let context={},seekSequence=0,timeJumpEditing=false,timer,controller=null,wanted=0,ready=0;
let timelineDrag=null,timelineSignature='',scanEvent=null,editing=null,epoch=1;
const image={},rate=()=>30,meta=()=>({frame_count:3600}),editLocked=()=>false;
const items=['suggestion','mosaic','deletion','ignored'].map((kind,i)=>({
  kind,id:'e'+i,start:1830+i*90,end:1875+i*90,label:'区域',event:{id:'e'+i}
}));
const timelineItems=()=>items,esc=String,clock=String;
const paint=()=>eval(source.match(/\$\('mosaicSeek'\)\.value=wanted;[^\n]*/)[0]);
const sourceBinding={seek:time=>media.push(time)};
const pausePlayback=()=>{},retainSelection=()=>{},loadFrame=()=>frames.push(wanted);
const setTimeout=fn=>(callbacks.push(fn),callbacks.length),clearTimeout=()=>{};
const scheduleThumbnails=()=>{},timelineFrame=e=>e.frame;
const timelineLimits=()=>({left:0,right:3600}),status=()=>{};
const guard=fn=>fn,save=()=>true,setEventRange=()=>{};
const viewScanEvent=()=>seek(60,true),viewTarget=()=>seek(60,true);
eval(section('  function seek(', '  async function recognize('));
eval(section('  function timelineTitle(', '  function timelineFrame('));
eval(section('  timeline.onpointermove=', '  timeline.onpointercancel='));
paintTimeline();
const html=$('mosaicTimelineBlocks').innerHTML+$('mosaicTimelineDeletions').innerHTML;
for(const label of ['AI 建议','打码','删除','忽略'])assert(html.includes('title="'+label));
assert(html.includes('1:01.0–1:02.5'));
assert.equal((html.match(/min-width:8px/g)||[]).length,4);
// Tooltips keep their type and fractional time while an edge moves.
seek(60);
let drag={pointer:1,version:epoch,item:items[1],button:{style:{}},start:1920,
  x:0,edge:'end',moved:false,seekSequence};
timelineDrag=drag;timeline.onpointermove({pointerId:1,frame:1923});
assert(drag.button.title.includes('打码'));
assert(drag.button.title.endsWith('1:04.0–1:05.6'));
assert.equal(wanted,1967);
timeline.onpointerup({pointerId:1});assert.equal(wanted,1967);
// Simulate typing while a cancelled timeout is already queued.
const inputHandler=source.match(/\$\('mosaicFrameNumber'\)\.oninput=(.*);/)[1];
eval('('+inputHandler+')')();$('mosaicFrameNumber').value='1:05';
callbacks.splice(0).forEach(fn=>fn());paint();
assert.equal(frames.length,0);assert.equal($('mosaicFrameNumber').value,'1:05');
// Any newer explicit seek wins over late move, release, timeout and rAF work.
for(const late of ['move','release','timeout','raf']){
  seek(60);drag={...drag,range:{start:1920,end:1968},seekSequence};timelineDrag=drag;
  const preview=()=>seek(1967,true,drag.seekSequence);
  seek(1950);const count=media.length;
  if(late==='move')timeline.onpointermove({pointerId:1,frame:1930});
  if(late==='release')timeline.onpointerup({pointerId:1});
  if(late==='timeout'||late==='raf')preview();
  callbacks.splice(0).forEach(fn=>fn());
  assert.equal(wanted,1950,late);assert.equal(media.length,count,late);
  assert.equal(media.at(-1),(1950+.05)/30,late);
  assert(frames.every(frame=>frame===1950),late);frames.length=0;
}
'''
    path = Path(__file__).parents[1] / 'exactcut/web/mosaic.js'
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
