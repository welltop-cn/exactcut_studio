"""Metadata-only editor lag/retry reports; no real browser or video required."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_editor_lag_and_retry_reports_are_scoped_bounded_and_private():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const sent=[],events={},callbacks=[];let now=0,observer;
const dialog={open:true,id:'mosaicDialog'};
const video={dataset:{previewBatch:'b'.repeat(32),previewRequest:'R123',previewSession:'c'.repeat(32)}};
const window={addEventListener:(n,fn)=>events['window.'+n]=fn,fetch:async(u,o)=>{sent.push(JSON.parse(o.body));return {ok:true};}};
const document={baseURI:'http://localhost/home/',visibilityState:'visible',
 addEventListener:(n,fn)=>{(events[n]??=[]).push(fn);},
 getElementById:id=>id==='mosaicDialog'?dialog:id==='mosaicSource'?video:null};
class Observer {static supportedEntryTypes=['longtask'];constructor(fn){observer=fn;}observe(){}}
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window,document,URL,
 performance:{now:()=>now},PerformanceObserver:Observer,requestAnimationFrame:fn=>callbacks.push(fn)});
const target={closest:s=>s==='dialog'?dialog:null,id:'private-label'};
assert(events.pointerdown,'editor actions must be monitored');
for(const f of events.pointerdown)f({target});
now=350;callbacks.splice(0).forEach(f=>f());
assert(sent.some(e=>e.event==='editor_action_latency'&&e.duration_ms===350&&e.session_id==='c'.repeat(32)));
observer({getEntries:()=>[{startTime:1,duration:300}]});
assert(sent.some(e=>e.event==='editor_long_task'&&e.duration_ms===300));
const n=sent.length;observer({getEntries:()=>[{startTime:2,duration:310}]});assert.equal(sent.length,n,'rate limited');
now=6000;for(const f of events.pointerdown)f({target});
video.dataset.previewSession='d'.repeat(32);now=6500;callbacks.splice(0).forEach(f=>f());
assert.equal(sent.length,n,'changed session must not inherit action latency');
now=12000;for(const f of events.pointerdown)f({target});
for(const f of events.close)f({target:dialog});
now=12500;callbacks.splice(0).forEach(f=>f());assert.equal(sent.length,n,'close/reopen same session must not inherit old latency');
document.visibilityState='hidden';observer({getEntries:()=>[{startTime:6001,duration:500}]});assert.equal(sent.length,n);
window.reportEditorRetry({action:'retry_scheduled',source:'frame',batch_id:'b'.repeat(32),request_id:'R123',session_id:'c'.repeat(32),duration_ms:1000,password:'private-secret',url:'private-url'});
assert(sent.some(e=>e.event==='editor_retry'&&e.source==='frame'));
assert(!JSON.stringify(sent).includes('private'));
// Monitoring unsupported or throwing must not affect startup.
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window:{...window},document,URL,performance:{now:()=>0}});
'''
    path = Path(__file__).resolve().parents[1] / 'exactcut/web/diagnostics.js'
    subprocess.run([node, '-e', script, str(path)], check=True, capture_output=True, text=True)


from test_diagnostics import diag, records
from test_web_base_path import app


def test_editor_observation_http_retains_only_bounded_metadata(diag, app):
    import json
    from test_web_base_path import request, login
    _, root = diag
    headers = login(app)
    for event in ('editor_retry', 'editor_action_latency', 'editor_long_task'):
        body = {'event': event, 'action': 'pointer', 'source': 'mosaicSource',
                'batch_id': 'a'*32, 'request_id': 'R1', 'session_id': 'b'*32,
                'duration_ms': 350, 'body': 'private-frame-content'}
        assert request(app, 'POST', '/home/api/client-events', body, headers)[0] == 200
    rows = [r['browser'] for r in records(root) if r['event'] == 'browser.event']
    assert {r['event'] for r in rows} == {'editor_retry', 'editor_action_latency', 'editor_long_task'}
    assert all(r['duration_ms'] == 350 and r['session_id'] == 'b'*32 for r in rows)
    assert 'private-frame-content' not in json.dumps(rows)
