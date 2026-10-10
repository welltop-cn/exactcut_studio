import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_preview_preloads_visible_media_once_without_autoplay():
    node = shutil.which('node')
    if not node:
        pytest.skip('requires node')
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    assert 'function preparePreviewLoads()' in source
    helper = source.split('function preparePreviewLoads()', 1)[1].split('\nfunction ', 1)[0]
    script = r'''
const assert=require('node:assert/strict');
let activeTab='progress',previewObserver=null,callback;
const videos=Array.from({length:4},()=>({dataset:{},preload:'none',loads:0,load(){this.loads++},play(){throw Error('autoplay')}}));
const $=()=>({querySelectorAll:()=>videos});
class IntersectionObserver {constructor(fn){callback=fn}observe(){}unobserve(){}disconnect(){}}
const fn=new Function('return '+JSON.parse(require('fs').readFileSync(0,'utf8')))();
// Evaluate in this lexical scope to use the same tab and observer state.
eval('preparePreviewLoads='+fn.toString());
preparePreviewLoads();assert.equal(callback,undefined);
activeTab='review';preparePreviewLoads();
callback([{target:videos[0],isIntersecting:true},{target:videos[1],isIntersecting:false}]);
assert.equal(videos[0].preload,'auto');assert.equal(videos[0].loads,1);assert.equal(videos[1].loads,0);
preparePreviewLoads();callback([{target:videos[0],isIntersecting:true}]);assert.equal(videos[0].loads,1);
activeTab='editor';callback([{target:videos[1],isIntersecting:true}]);assert.equal(videos[1].loads,0);
'''
    result = subprocess.run([node, '-e', script], input=json.dumps('function preparePreviewLoads()'+helper), text=True, capture_output=True)
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
