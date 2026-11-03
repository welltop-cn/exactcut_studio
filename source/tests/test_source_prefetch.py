import copy
import json
import queue
import shutil
import subprocess
from pathlib import Path

import pytest

from exactcut import webserver as web, workflow as w, source_cache as cache
from test_web_base_path import app, login, request


@pytest.fixture
def console(app, monkeypatch, tmp_path):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    monkeypatch.setattr(web, 'JOBS', tmp_path / '.web-state/jobs')
    web.JOBS.mkdir()
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '1')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '1')
    created = w.create_blank()
    from exactcut import accounts
    accounts.assign(w.folder(created['id']), {'username': 'admin'})
    return app, created, login(app)


def draft_with(*episodes):
    from exactcut.intake import blank_clip
    return {'drama_id': '101573', 'rows': [{'request_id': 'R1', 'clips': [
        {**blank_clip(), 'episode': episode, 'start_s': '02', 'end_s': '05'}
        for episode in episodes]}]}


def prefetch(console, draft, **extra):
    app, item, headers = console
    status, _, body = request(app, 'POST', f"/home/api/batches/{item['id']}/sources",
                              {'draft': draft, **extra}, headers)
    return status, json.loads(body)


def jobs():
    return list(web.stored_jobs())


def test_first_valid_clip_queues_sources_without_saving_or_confirming_draft(console):
    app, item, headers = console
    before = (w.folder(item['id']) / 'draft.json').read_bytes()
    status, result = prefetch(console, draft_with('EP1'))
    assert status == 200, result
    assert result['drama_id'] == '101573'
    assert [(e['episode'], e['status']) for e in result['episodes']] == [('EP1', 'queued')]
    assert len(jobs()) == 1 and jobs()[0]['action'] == 'prefetch'
    assert w.runtime(w.folder(item['id']))['status'] == 'draft'
    assert (w.folder(item['id']) / 'draft.json').read_bytes() == before
    assert not (w.folder(item['id']) / 'confirmed.json').exists()
    assert request(app, 'POST', f"/home/api/batches/{item['id']}/save",
                   {'revision': 1, 'draft': draft_with('EP1')}, headers)[0] == 200


def test_repeated_edits_deduplicate_and_new_segments_only_queue_missing_episodes(console):
    assert prefetch(console, draft_with('1'))[0] == 200
    assert prefetch(console, draft_with('EP1', 'EP1'))[0] == 200
    assert len(jobs()) == 1
    _, result = prefetch(console, draft_with('EP1', 'EP2', 'EP5'))
    assert [e['episode'] for e in result['episodes']] == ['EP1', 'EP2', 'EP5']
    assert len(jobs()) == 2
    assert any(job['episodes'] == ['EP2', 'EP5'] for job in jobs())
    assert all('EP3' not in job['episodes'] for job in jobs())


def test_invalid_id_and_unfinished_clips_do_not_download(console):
    draft = draft_with('EP1')
    draft['drama_id'] = '../101573'
    assert prefetch(console, draft)[0] == 400
    draft = draft_with('EP1')
    draft['rows'][0]['clips'][0]['end_s'] = '00'
    status, result = prefetch(console, draft)
    assert status == 200 and result['episodes'] == []
    assert jobs() == []


def test_sources_requires_auth_csrf_ownership_and_enabled_worker(console, monkeypatch):
    app, item, headers = console
    endpoint = f"/home/api/batches/{item['id']}/sources"
    body = {'draft': draft_with('EP1')}
    assert request(app, 'POST', endpoint, body)[0] == 401
    assert request(app, 'POST', endpoint, body, {'Cookie': headers['Cookie']})[0] == 403
    from exactcut import accounts
    accounts.assign(w.folder(item['id']), {'username': 'someone-else'})
    # Admin may access all batches, so check an authenticated producer instead.
    accounts.manage(app.auth_db, 'admin', {'username': 'alice', 'name': 'Alice', 'password': 'fixture-password'})
    _, h, _ = request(app, 'POST', '/home/api/login',
                       {'username': 'alice', 'password': 'fixture-password'}, {'X-Exactcut-Request': '1'})
    assert request(app, 'POST', endpoint, body,
                   {'Cookie': h['Set-Cookie'].split(';')[0], 'X-Exactcut-Request': '1'})[0] == 404
    monkeypatch.setenv('EXACTCUT_WORKER_ENABLED', '0')
    assert request(app, 'POST', endpoint, body, headers)[0] == 503
    assert jobs() == []


def test_finish_prefetch_does_not_interrupt_a_confirmed_queued_render(console):
    _, item, _ = console
    path = w.folder(item['id'])
    w.update(path, 'queued', '等待制作')
    job = {'id': 'a' * 32, 'batch_id': item['id'], 'action': 'prefetch', 'status': 'running'}
    web.finish_job(job, 0)
    assert w.runtime(path)['status'] == 'queued'


def test_queued_render_reports_prefetch_bytes_without_changing_durable_runtime(console):
    from exactcut import source_prefetch
    from exactcut.util import write_json_atomic
    app, item, headers = console
    prefetch(console, draft_with('EP1', 'EP2', 'EP6'))
    job = jobs()[0]
    job['status'] = 'running'; web.save(job)
    request(app, 'POST', f"/home/api/batches/{item['id']}/save",
            {'revision': 1, 'draft': draft_with('EP1', 'EP2')}, headers)
    path = w.folder(item['id'])
    w.update(path, 'queued', '等待制作', plan={'required': ['EP1', 'EP2']})
    directory = cache.source_root() / '101573'
    directory.mkdir(parents=True)
    (directory / 'EP1.mp4').write_bytes(b'x' * 2048)
    write_json_atomic(directory / 'EP1.json', {'complete': True, 'bytes': 2048})
    write_json_atomic(source_prefetch.result_path(path, job['id']), {'status': 'running', 'episodes': {
        'EP1': {'status': 'ready', 'signature': source_prefetch.signature('101573', 'EP1')},
        'EP2': {'status': 'downloading', 'received_bytes': 1048576, 'total_bytes': 4194304},
        'EP6': {'status': 'downloading', 'received_bytes': 99999999, 'total_bytes': 999999999}}})
    before = (path / 'runtime.json').read_bytes()
    status, _, body = request(app, 'GET', f"/home/api/batches/{item['id']}", headers=headers)
    assert status == 200
    runtime = json.loads(body)['runtime']
    assert runtime.get('download_prefetch') is True
    assert runtime['status'] == 'queued'
    assert runtime['download_current_episode'] == 'EP2'
    assert runtime['download_current_received'] == 1048576
    assert runtime['download_current_total'] == 4194304
    assert runtime['download_files'] == 1 and runtime['download_bytes'] == 1048576 + 2048
    assert runtime['download_complete'] is False
    assert (path / 'runtime.json').read_bytes() == before
    (directory / 'EP2.mp4').write_bytes(b'x' * 4096)
    write_json_atomic(directory / 'EP2.json', {'complete': True, 'bytes': 4096})
    runtime = web.batch_response(item['id'])['runtime']
    assert runtime['download_complete'] is True and runtime['download_files'] == 2
    assert runtime['download_bytes'] == 6144 and runtime['download_current_episode'] is None
    w.update(path, 'downloading', '正式下载', download_current_episode='EP2',
             download_current_received=3145728, download_current_total=4194304)
    runtime = web.batch_response(item['id'])['runtime']
    assert runtime.get('download_prefetch') is not True
    assert runtime['download_current_received'] == 3145728


def test_browser_shows_prefetch_progress_while_render_is_queued():
    node = shutil.which('node')
    if not node: pytest.skip('requires node')
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const src=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const a=src.indexOf('function runtimeMessage('),b=src.indexOf('\nfunction renderAssets(',a);
const elements=new Map(),el=id=>{if(!elements.has(id))elements.set(id,{style:{},querySelectorAll:()=>[]});return elements.get(id)};
const ctx=vm.createContext({current:{filename:'网页填写',assets:[],feedback_count:0,runtime:{status:'queued',
 plan:{required:['EP1','EP2']},download_prefetch:true,download_files:1,download_bytes:1048576+2048,
 download_current_episode:'EP2',download_current_received:1048576,download_current_total:4194304}},
 $:el,document:{querySelector:el},renderSourcePrefetch:()=>{},renderAssets:()=>{},preparePreviewLoads:()=>{},controls:()=>{},
 statusLabel:s=>s,badgeClass:()=>'',esc:s=>String(s??''),setErrorHtml:()=>{},observationText:()=>'',EditorBusy:{message:(s,fallback='')=>s||fallback}});
vm.runInContext(src.slice(src.indexOf('function materialLabel('),src.indexOf('function productionProgress(')),ctx);
vm.runInContext(src.slice(a,b),ctx);vm.runInContext('renderRuntime()',ctx);
assert(el('stageOverview').innerHTML.includes('EP2（2/2）'));
assert(el('stageOverview').innerHTML.includes('25%'));
assert(el('stageOverview').innerHTML.includes('value="1048576" max="4194304"'));
assert.equal(el('progressHint').hidden,true);
ctx.current.runtime.status='downloading';delete ctx.current.runtime.download_prefetch;
ctx.current.runtime.download_current_received=3145728;vm.runInContext('renderRuntime()',ctx);
assert(el('stageOverview').innerHTML.includes('75%'));
ctx.current.runtime.download_complete=true;
ctx.current.runtime.observation={step:'获取正片',elapsed_seconds:6,stage_seconds:3};
vm.runInContext('renderRuntime()',ctx);
assert.equal(el('executionObservation').hidden,true,'completed download must not show stale observation');
assert(el('stageOverview').innerHTML.includes('所需正片已就绪'));
assert(!el('stageOverview').innerHTML.includes('<progress'));
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_prefetch_ignores_old_total_ten_gate_but_disabled_fetch_does_not_queue_new_work(console, monkeypatch):
    monkeypatch.setattr(web, 'queue_size', lambda: 10)
    status, result = prefetch(console, draft_with('EP1'))
    assert status==200 and result['episodes'][0]['status']=='queued'
    accepted=jobs();assert len(accepted)==1
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH','0')
    assert prefetch(console,draft_with('EP5'))[0]==400
    assert jobs()==accepted


def test_prefetch_episode_references_survive_until_draft_is_saved(console):
    from exactcut import batch_control
    _, item, _ = console
    prefetch(console, draft_with('EP1', 'EP5'))
    assert {('101573', 'EP1'), ('101573', 'EP5')} <= cache.named_episodes()
    batch_control.change(w.folder(item['id']), 'archive')
    assert ('101573', 'EP1') not in cache.named_episodes()


def test_partial_composition_and_blank_clip_do_not_block_a_valid_segment(console):
    draft = {'drama_id': '101573', 'rows': [{'request_id': 'R1', 'composition': {
        'plots': [{'episode': '2', 'start': '00:02', 'end': '00:05'},
                  {'episode': '3', 'start': '', 'end': ''}]}}]}
    status, result = prefetch(console, draft)
    assert status == 200 and [e['episode'] for e in result['episodes']] == ['EP2']


def test_clearing_drama_cancels_queued_downloads_without_changing_draft(console, monkeypatch):
    from exactcut import source_prefetch
    prefetch(console, draft_with('EP1'))
    job = jobs()[0]
    app, item, headers = console
    status, _, body = request(app, 'POST', f"/home/api/batches/{item['id']}/sources",
                              {'cancel': True}, headers)
    assert status == 200 and json.loads(body)['episodes'] == []
    monkeypatch.setattr(cache, 'ensure', lambda *a, **k: pytest.fail('Cancelled source was fetched'))
    assert source_prefetch.run(job['batch_id'], job['id']) == 0
    assert ('101573', 'EP1') not in cache.named_episodes()
    web.finish_job(job, 0)
    _, result = prefetch(console, draft_with('EP1'))
    assert len(jobs()) == 2
    assert result['episodes'][0]['status'] == 'queued'


def test_real_cache_download_is_reused_and_source_duration_is_returned(console, monkeypatch):
    from exactcut import source_prefetch
    monkeypatch.setattr(cache, '_fetch_argv', lambda: ['fixture'])
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda p: None)
    calls = []
    def fetch(argv, drama, episode, dest, on_progress=None):
        calls.append((drama, episode))
        dest.write_bytes(b'fixture-video')
        dest.with_suffix('.json').write_text(json.dumps({'complete': True, 'bytes': dest.stat().st_size}))
        if on_progress: on_progress(episode, 13, 13)
    monkeypatch.setattr(cache, '_run_fetch', fetch)
    monkeypatch.setattr('exactcut.timing_review.source_duration', lambda p, **kw: 9000)
    assert prefetch(console, draft_with('EP1'))[0] == 200
    job = jobs()[0]
    assert source_prefetch.run(job['batch_id'], job['id']) == 0
    web.finish_job(job, 0)
    _, result = prefetch(console, draft_with('EP1', 'EP1'))
    assert calls == [('101573', 'EP1')]
    assert len(jobs()) == 1
    assert result['episodes'][0]['status'] == 'ready'
    assert result['timing_explanation'][0]['segments'][0]['source_duration_ms'] == 9000


def test_obsolete_queued_drama_is_skipped_and_failed_download_needs_explicit_retry(console, monkeypatch):
    from exactcut import source_prefetch
    first = draft_with('EP1')
    prefetch(console, first)
    old = jobs()[0]
    second = copy.deepcopy(first); second['drama_id'] = '101574'
    prefetch(console, second)
    monkeypatch.setattr(cache, 'ensure', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fixture failure')))
    assert source_prefetch.run(old['batch_id'], old['id']) == 0
    web.finish_job(old, 0)
    active = next(job for job in jobs() if job['project'] == '101574')
    assert source_prefetch.run(active['batch_id'], active['id']) == 2
    web.finish_job(active, 2)
    _, result = prefetch(console, second)
    assert result['episodes'][0]['status'] == 'failed' and len(jobs()) == 2
    prefetch(console, second, retry=True)
    assert len(jobs()) == 3


def test_recovery_uses_prefetch_result_without_changing_draft_state(console):
    from exactcut import source_prefetch
    prefetch(console, draft_with('EP1'))
    job = jobs()[0]
    from exactcut.runtime import instance_marker
    job.update(status='running', instance=instance_marker())
    web.save(job)
    from exactcut.util import write_json_atomic
    write_json_atomic(source_prefetch.result_path(w.folder(job['batch_id']), job['id']),
                      {'status': 'completed', 'episodes': {}})
    web.recover_jobs()
    assert next(web.stored_jobs())['status'] == 'completed'
    assert w.runtime(w.folder(job['batch_id']))['status'] == 'draft'


def test_actual_worker_downloads_fixture_and_preserves_editable_draft(console, monkeypatch, tmp_path):
    import os
    import sys
    import time
    if not shutil.which('ffmpeg'): pytest.skip('requires ffmpeg')
    fixture = tmp_path / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=108x192:r=30',
                    '-t', '9', '-c:v', 'libx264', '-preset', 'ultrafast', str(fixture)], check=True)
    fetch = tmp_path / 'fixture_fetch.py'
    fetch.write_text('import shutil,sys\nshutil.copyfile('+repr(str(fixture))+',sys.argv[3])\n')
    env = tmp_path / 'development.env'; env.write_text('EXACTCUT_ENVIRONMENT=development\n')
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(env))
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', sys.executable + ' ' + str(fetch))
    prefetch(console, draft_with('EP1'))
    job = jobs()[0]
    pending, owned = [], {}
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            web.worker_tick(pending, owned)
            saved = next(web.stored_jobs())
            if saved['status'] in {'completed', 'failed', 'interrupted'}: break
            time.sleep(.05)
        assert saved['status'] == 'completed'
        assert w.runtime(w.folder(job['batch_id']))['status'] == 'draft'
        assert web.WORK.unfinished_tasks == 0
        assert prefetch(console, draft_with('EP1'))[1]['episodes'][0]['status'] == 'ready'
        assert len(jobs()) == 1
    finally:
        from exactcut import batch_control
        import signal
        for active, process, log in owned.values():
            batch_control.signal_identity(active, signal.SIGKILL)
            process.wait(timeout=5)
            log.close()


def test_browser_prefetch_keeps_unsaved_edits_and_ignores_old_drama_response():
    node = shutil.which('node')
    if not node: pytest.skip('requires node')
    source = (Path(__file__).resolve().parents[1] / 'exactcut/web/app.js').read_text()
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const src=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const a=src.indexOf('function scheduleSourcePrefetch('),b=src.indexOf('\nfunction changed(',a);
assert(a>=0&&b>a);
const m=src.indexOf('function mergedTiming('),n=src.indexOf('\nfunction timingTable(',m);
const timing=vm.createContext({});vm.runInContext(src.slice(m,n),timing);
timing.local={segments:[{episode:'EP1',start_ms:2000,end_ms:5000,source_status:'missing',error:''}]};
timing.server={segments:[{episode:'EP1',start_ms:2000,end_ms:10000,source_status:'ready',error:'old bounds'}]};
assert.equal(vm.runInContext('mergedTiming(local,server).segments[0].source_status',timing),'missing');
assert.equal(vm.runInContext('mergedTiming(local,server).segments[0].error',timing),'');
(async()=>{
 const calls=[],timers=[];let finish;
 const ctx=vm.createContext({current:{id:'batch-a',draft:{drama_id:'101573',rows:[]},runtime:{status:'draft'}},
 currentUser:{features:{material_fetch:true}},dirty:true,prefetching:false,prefetchTimer:null,
 openingBatchId:null,selectedId:'batch-a',
 api:async(path,data)=>{calls.push([path,data]);return new Promise(resolve=>finish=resolve)},
 refreshTiming:()=>{},renderSourcePrefetch:()=>{},setTimeout:f=>{timers.push(f);return timers.length},clearTimeout:()=>{}});
 vm.runInContext(src.slice(a,b),ctx);
 const promise=vm.runInContext('prefetchSources()',ctx);
 assert.equal(calls[0][0],'/api/batches/batch-a/sources');
 assert.equal(calls[0][1].draft.drama_id,'101573');
 ctx.current.draft={drama_id:'101574',rows:[]};
 finish({drama_id:'101573',episodes:[{episode:'EP1',status:'ready'}],timing_explanation:[]});
 await promise;
 assert.equal(ctx.current.draft.drama_id,'101574');assert.equal(ctx.dirty,true);
 assert.equal(ctx.current.source_prefetch,undefined);
 ctx.current.draft={drama_id:'',rows:[]};ctx.current.timing_explanation=[{request_id:'R1',segments:[{episode:'EP1',source_status:'ready'}]}];
 let refreshed=0;ctx.refreshTiming=()=>refreshed++;
 ctx.api=async(path,data)=>{assert.equal(data.cancel,true);return {drama_id:'',episodes:[],timing_explanation:[]}};
 await vm.runInContext('prefetchSources()',ctx);
 assert.equal(ctx.current.timing_explanation.length,0);assert(refreshed>0);
 ctx.openingBatchId='batch-b';ctx.api=()=>{throw Error('must not prefetch the previous project while switching')};
 await vm.runInContext('prefetchSources()',ctx);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(source), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
