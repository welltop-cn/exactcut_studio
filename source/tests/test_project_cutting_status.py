"""Project detail describes active edits even while the previous video is playable."""
import pytest

from exactcut import batch_control, material_versions, mosaic_queue, workflow
from test_workflow import workspace, imported, complete_asset


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('statuses,expected', [
    (['queued'], '剪切/打码排队中'),
    (['running'], '剪切/打码处理中'),
    (['cancelling'], '剪切/打码处理中'),
    (['queued', 'running'], '剪切/打码处理中'),
    (['queued', 'cancelling'], '剪切/打码处理中'),
])
def test_detail_tracks_cutting_until_finished(workspace, monkeypatch, managed, statuses, expected):
    path, request, video, record = complete_asset(imported())
    workflow.update(path, 'review', '成片已就绪，可播放查看')
    if managed:
        material_versions.enroll(path)
    tasks = [{'id': str(i), 'batch_id': path.name, 'session_id': 'session',
              'job': {'status': status}} for i, status in enumerate(statuses)]
    # Another project's running task must not affect this project's status.
    tasks.append({'id': 'other', 'batch_id': 'other', 'session_id': 'session',
                  'job': {'status': 'running'}})
    monkeypatch.setattr(mosaic_queue, 'load', lambda: {'tasks': tasks})
    detail = workflow.detail(path.name)
    runtime = detail['runtime']
    assert runtime['status'] == 'review'
    assert runtime['cutting_active'] and batch_control.cutting_active(path.name)
    assert runtime['message'] == expected + '；已有成片仍可播放'
    assert runtime['cutting_message'] == runtime['message']
    assert '成片已就绪' not in runtime['message']
    assert detail['assets'][0]['preview']
    # Finished/failed/cancelled edits no longer replace the ready message.
    for terminal in ['completed', 'failed', 'cancelled']:
        for task in tasks[:-1]:
            task['job']['status'] = terminal
        finished = workflow.detail(path.name)['runtime']
        assert not finished['cutting_active'] and not batch_control.cutting_active(path.name)
        assert not finished['cutting_message']
        assert '成片已就绪' in finished['message']
    assert '剪切/打码' not in workflow.runtime(path)['message']


def test_frontend_cutting_messages_and_polling_without_browser():
    """Run real rendering/polling functions with small DOM boundary doubles."""
    import shutil
    import subprocess
    from pathlib import Path

    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    runtime = source[source.index('function runtimeMessage('):source.index('const previewRequests=')]
    polling = next(line for line in source.splitlines() if line.startswith('function scheduledPoll('))
    cards = source[source.index('function executionAsset('):source.index('function renderQueue(')]
    script = r'''
const assert=require('node:assert/strict');
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false});return nodes.get(id)};
const document={hidden:false,querySelector:()=>$('reviewTab')};
const esc=s=>String(s??'');const materialMessage=s=>s;
const materialLabel=s=>s;const materialEventDetails=()=>'';
const durationLabel=s=>s+' 秒';const stageNames={};
const renderSourcePrefetch=()=>{},renderAssets=()=>{},controls=()=>{};
const observationText=()=>'',setErrorHtml=()=>{},eventMarkup=()=>'',operationEvents=()=>[];
const statusLabel=s=>s==='review'?'待预览':s,badgeClass=()=>'';
const EditorBusy={message:s=>s};
const renderRequestProgress=(assets,r)=>$('requestProgress').innerHTML=assets.map(a=>productionProgress(a,r)).join('');
let current={filename:'网页填写',assets:[{request_id:'R1',status:'qc_passed',preview:'/old'}],
  runtime:{status:'review',download_complete:true,events:[],message:'成片已就绪，可播放查看'}};
let batchCache=[],activeStatuses=['producing'],calls=0;const poll=()=>calls++;
''' + cards + runtime + polling + r'''
for(const message of ['剪切/打码排队中','剪切/打码处理中',null]){
 current.runtime.cutting_active=true;current.runtime.cutting_message=message;
 renderRuntime();
 assert.equal($('batchStatus').textContent,'剪切/打码中');
 assert.equal($('liveMessage').textContent,message||'剪切/打码处理中');
 assert.equal($('progressMessage').textContent,$('liveMessage').textContent);
 assert.equal($('progressMessage').hidden,false);
 assert.ok(!$('requestProgress').innerHTML.includes('成片已就绪，可前往预览'));
 assert.equal(current.assets[0].preview,'/old');
}
scheduledPoll.lastAttempt=Date.now()-4000;scheduledPoll();assert.equal(calls,1);
current.runtime.cutting_active=false;current.runtime.cutting_message='';renderRuntime();
assert.equal($('liveMessage').textContent,'成片已就绪，可播放查看');
assert.equal($('batchStatus').textContent,'待预览');assert.equal($('progressMessage').hidden,true);
scheduledPoll.lastAttempt=Date.now()-4000;scheduledPoll();assert.equal(calls,1);
scheduledPoll.lastAttempt=Date.now()-16000;scheduledPoll();assert.equal(calls,2);
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
