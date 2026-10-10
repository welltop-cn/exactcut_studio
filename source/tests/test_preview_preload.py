import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_preview_media_loads_only_on_user_play():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    assert 'preload="none"' in source
    assert "video.preload='auto'" not in source
    assert 'preparePreviewLoads' not in source
    assert 'previewObserver' not in source


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
