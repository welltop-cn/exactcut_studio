import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_preview_media_is_deferred_and_stale_response_cannot_restart_video():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 let resolveFetch,calls=0,loads=0,aborted=false;
 const message={textContent:'',append(){}},button={hidden:true};
 const video={dataset:{previewRequest:'R1',previewBatch:'a'},isConnected:true,src:'',pause(){},load(){loads++},removeAttribute(){this.src=''},querySelectorAll(){return []},closest(){return {querySelector(s){return s.includes('retry')?button:message}}}};
 const ctx=vm.createContext({AbortController,Date,encodeURIComponent,setTimeout,clearTimeout,
  requestAnimationFrame:f=>f(),current:{id:'a',assets:[{request_id:'R1',preview:'/preview'}]},activeTab:'progress',selectedId:'a',openingBatchId:null,
  appUrl:p=>p,$:id=>id==='workspace'?{hidden:false}:{querySelectorAll:()=>[video]},
  fetch:(url,options)=>{calls++;options.signal.addEventListener('abort',()=>aborted=true);return new Promise(r=>resolveFetch=r)}});
 vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('const previewRequests='),source.indexOf('function renderAssets(')),ctx);
 vm.runInContext('preparePreviewLoads()',ctx);assert.equal(calls,0);
 ctx.activeTab='review';vm.runInContext('preparePreviewLoads()',ctx);assert.equal(calls,1);
 ctx.activeTab='progress';vm.runInContext('stopPreviewLoads()',ctx);assert.equal(aborted,true);
 resolveFetch({ok:true,json:async()=>({url:'https://oss.invalid/signed',expires_in:900})});
 await new Promise(r=>setTimeout(r,0));assert.equal(video.src,'');
 ctx.activeTab='review';vm.runInContext('preparePreviewLoads()',ctx);assert.equal(calls,2);
 resolveFetch({ok:true,json:async()=>({url:'https://oss.invalid/fresh',expires_in:900})});
 await new Promise(r=>setTimeout(r,0));assert.equal(video.src,'https://oss.invalid/fresh');assert.equal(video.preload,'auto');
 vm.runInContext('stopPreviewLoads()',ctx);assert.equal(video.src,'');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_switch_from_preview_sets_destination_tab_before_rendering():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
const seen=[];
const ctx=vm.createContext({activeTab:'review',hookPreviews:new Map(),
 localStorage:{setItem(){}},history:{replaceState(){}},showPage(){},
 setTab(name){ctx.activeTab=name},
 renderEditor(){seen.push(ctx.activeTab)},renderRuntime(){seen.push(ctx.activeTab)}});
vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('function displayBatch('),source.indexOf('async function openBatch(')),ctx);
ctx.item={id:'b',runtime:{status:'review'},assets:[{preview:'/video'}]};
vm.runInContext('displayBatch(item)',ctx);
assert.deepEqual(seen,['progress','progress'],'renderers must not inherit the old preview tab');
assert.equal(ctx.activeTab,'progress');
seen.length=0;ctx.activeTab='review';ctx.item.runtime.status='draft';
vm.runInContext('displayBatch(item)',ctx);
assert.deepEqual(seen,['editor','editor']);assert.equal(ctx.activeTab,'editor');
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_expired_preview_renews_on_play_and_seek_preserving_position_and_pause():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 let now=1000,calls=0,plays=0;
 const message={},retry={hidden:true};
 const video={dataset:{previewRequest:'R1',previewBatch:'a'},isConnected:true,paused:true,currentTime:0,duration:120,
 pause(){this.paused=true},play(){plays++;this.paused=false;return Promise.resolve()},
 load(){this.currentTime=0},removeAttribute(){this.src=''},querySelectorAll(){return []},
 closest(){return {querySelector:s=>s.includes('retry')?retry:message}}};
 const ctx=vm.createContext({AbortController,Date:{now:()=>now},encodeURIComponent,setTimeout,clearTimeout,
  current:{id:'a',assets:[{request_id:'R1',preview:'/preview'}]},activeTab:'review',selectedId:'a',appUrl:p=>p,$:id=>id==='workspace'?{hidden:false}:{querySelectorAll:()=>[video]},
  fetch:async()=>{calls++;return {ok:true,json:async()=>({url:'https://oss.invalid/'+calls,expires_in:900})}}});
 vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
vm.runInContext(source.slice(source.indexOf('const previewRequests='),source.indexOf('function renderAssets(')),ctx);ctx.video=video;
 await vm.runInContext('loadPreview(video)',ctx);
 video.currentTime=43;video.paused=false;now+=901000;video.onplay();
 await new Promise(r=>setTimeout(r,0));assert.equal(calls,2);video.onloadedmetadata();assert.equal(video.currentTime,43);assert.equal(plays,1);
 video.pause();video.currentTime=78;now+=901000;
 assert.equal(typeof video.onseeking,'function','expired seek must refresh before another Range request');video.onseeking();
 await new Promise(r=>setTimeout(r,0));assert.equal(calls,3);video.onloadedmetadata();assert.equal(video.currentTime,78);assert.equal(plays,1);assert.equal(video.paused,true);
 now+=901000;video.currentTime=81;video.onerror();
 await new Promise(r=>setTimeout(r,0));assert.equal(calls,4);video.onloadedmetadata();assert.equal(video.currentTime,81);assert.equal(plays,1,'preload failure must not auto-play paused video');
 ctx.fetch=async()=>{calls++;return {ok:false}};now+=901000;video.onplay();
 await new Promise(r=>setTimeout(r,0));assert.equal(retry.hidden,false);const failedCalls=calls;
 await new Promise(r=>setTimeout(r,10));assert.equal(calls,failedCalls,'failure must not spin retries');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_preparing_response_polls_until_signed_url_and_never_uses_local_source():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 let resolves=[],calls=0;
 const message={textContent:''},retry={hidden:true};
 const video={dataset:{previewRequest:'R1',previewBatch:'a'},isConnected:true,src:'',paused:true,currentTime:0,
  pause(){},load(){},removeAttribute(){this.src=''},querySelectorAll(){return []},
  closest(){return {querySelector:s=>s.includes('retry')?retry:message}}};
 const ctx=vm.createContext({AbortController,DOMException,Date,encodeURIComponent,setTimeout,clearTimeout,
  current:{id:'a',assets:[{request_id:'R1',preview:'/preview'}]},activeTab:'review',selectedId:'a',appUrl:p=>p,
  $:id=>id==='workspace'?{hidden:false}:{querySelectorAll:()=>[video]},
  fetch:()=>{calls++;return new Promise(r=>resolves.push(r))}});
 vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
 vm.runInContext(source.slice(source.indexOf('const previewRequests='),source.indexOf('function renderAssets(')),ctx);
 ctx.video=video;
 const pending=vm.runInContext('loadPreview(video)',ctx);
 while(resolves.length<1)await new Promise(r=>setTimeout(r,5));
 resolves.shift()({ok:true,json:async()=>({status:'preparing',retry_after_ms:200,job_id:'x'})});
 await new Promise(r=>setTimeout(r,30));
 assert.equal(video.src,'','preparing must not attach a source');
 assert.equal(calls,1);assert.equal(message.textContent,'正在准备预览视频…');
 while(resolves.length<1)await new Promise(r=>setTimeout(r,5));
 assert.equal(calls,2);
 resolves.shift()({ok:true,json:async()=>({url:'https://oss.invalid/final',expires_in:900})});
 await pending;
 assert.equal(video.src,'https://oss.invalid/final');
 assert.equal(video.dataset.previewState,'ready');
 assert.equal(retry.hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_failed_preview_retries_upload_only_from_retry_button():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 const urls=[],message={},retry={hidden:true};
 const replies=[{ok:false},{ok:true,json:async()=>({status:'preparing',retry_after_ms:1})},{ok:true,json:async()=>({url:'https://oss.invalid/1',expires_in:900})}];
 const video={dataset:{previewRequest:'R1',previewBatch:'a'},isConnected:true,paused:true,pause(){},load(){},removeAttribute(){},querySelectorAll(){return []},
  closest(){return {querySelector:s=>s.includes('retry')?retry:message}}};
 const ctx=vm.createContext({AbortController,Date,encodeURIComponent,setTimeout,clearTimeout,DOMException,
  current:{id:'a',assets:[{request_id:'R1',preview:'/preview'}]},activeTab:'review',selectedId:'a',appUrl:p=>p,$:id=>id==='workspace'?{hidden:false}:{querySelectorAll:()=>[video]},
  fetch:async url=>{urls.push(url);return replies.shift()}});
 vm.runInContext(source.split('\n').find(line=>line.startsWith('function rememberSavedDraft(')),ctx);
 vm.runInContext(source.slice(source.indexOf('const previewRequests='),source.indexOf('function renderAssets(')),ctx);ctx.video=video;
 await vm.runInContext('loadPreview(video)',ctx);
 assert.equal(retry.hidden,false);assert.deepEqual(urls,['/api/batches/a/preview-url/R1']);
 retry.onclick();await new Promise(r=>setTimeout(r,400));
 assert.deepEqual(urls.slice(1),['/api/batches/a/preview-url/R1?retry=1','/api/batches/a/preview-url/R1']);
 assert.equal(video.dataset.previewState,'ready');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
