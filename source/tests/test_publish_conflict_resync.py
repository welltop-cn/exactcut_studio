"""A stale revision on a managed write resyncs the project instead of failing forever."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_managed_revision_conflict_resyncs_and_reports_it():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = r'''
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const grab=name=>{const i=source.indexOf(name),open=source.indexOf('){',i)+1;let depth=0,j=open;for(;j<source.length;j++){const c=source[j];if(c==='{')depth++;else if(c==='}'){depth--;if(depth===0){j++;break}}}return source.slice(i,j)};
let current={id:'b'.repeat(32),media_managed:true,revision:7};
const batchCache=[],resynced=[];
const randomId=()=> 'c'.repeat(32),appUrl=p=>p;
const sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
const EditorBusy={error:(response,body)=>Object.assign(Error(body.error||'busy'),{status:response.status})};
const refreshSelectedBatch=async()=>{resynced.push('refresh');current.revision=8};
const location={replace(){}};
let calls=0;
const fetch=async()=>{calls+=1;return calls===1?{ok:false,status:409,json:async()=>({error:'项目已在其他页面更新，请刷新后重试'})}:{ok:true,status:202,json:async()=>({id:'b'.repeat(32)})}};
const AbortController=class{constructor(){this.signal={addEventListener(){},aborted:false}}abort(){}};
const setTimeout=(fn,ms)=>0,clearTimeout=()=>{};
eval(grab('function isRevisionConflict('));
eval(grab('async function api('));
(async()=>{
  await assert.rejects(api('/api/batches/'+'b'.repeat(32)+'/publish',{request_id:'r'}),
    error=>error.status===409&&/已同步最新状态/.test(error.message));
  assert.deepStrictEqual(resynced,['refresh'],'a stale revision must refresh the project once');
  assert.equal(calls,1);
  const ok=await api('/api/batches/'+'b'.repeat(32)+'/publish',{request_id:'r'});
  assert.deepStrictEqual(ok,{id:'b'.repeat(32)},'the retry after resync must succeed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path = Path(__file__).parents[1] / 'exactcut/web/app.js'
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
