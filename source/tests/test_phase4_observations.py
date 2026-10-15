from datetime import datetime, timezone
import threading
import time
from unittest.mock import patch
from test_phase1_recovery import workspace
from exactcut import workflow as w
from exactcut.error_guidance import guidance, redact
from exactcut.progress import observation
from exactcut.util import write_json_atomic, now_iso


def test_heartbeat_does_not_advance_checkpoint_and_updates_do_not_lose_fields(workspace):
    path=w.folder(workspace['id'])
    with w.execution(path, interval=.01):
        w.checkpoint(path,'base_cut',segment=1,total=3,completed=0)
        checkpoint=w.runtime(path)['last_progress_at']
        pulse=w.runtime(path)['heartbeat_at']
        for index in range(30):
            w.update(path,custom=index)
            time.sleep(.001)
        deadline=time.monotonic()+1
        while w.runtime(path)['heartbeat_at']==pulse and time.monotonic()<deadline:
            time.sleep(.01)
        running=w.runtime(path)
        assert running['heartbeat_at']!=pulse
        assert running['last_progress_at']==checkpoint
        assert running['custom']==29 and running['progress_details']['completed']==0
    ended=w.runtime(path)
    assert not ended['executor_running'] and ended['execution_ended_at']


def test_observation_staleness_pause_and_finished_elapsed():
    now=datetime(2026,10,7,0,2,tzinfo=timezone.utc)
    data={'execution_started_at':'2026-10-07T00:00:00+00:00','stage_started_at':'2026-10-07T00:00:10+00:00',
          'last_progress_at':'2026-10-07T00:00:10+00:00','heartbeat_at':'2026-10-07T00:01:59+00:00',
          'executor_running':True,'stage':'base_cut'}
    live=observation(data,now=now)
    assert live['elapsed_seconds']==120 and live['last_progress_seconds']==110
    assert '尚无新节点' in live['notice'] and live['heartbeat_seconds']==1
    assert observation(data,paused=True,now=now)['notice']=='已暂停，进度已保留'
    data['heartbeat_at']='2026-10-07T00:00:00+00:00'
    assert '未收到执行心跳' in observation(data,now=now)['notice']
    data.update(execution_ended_at='2026-10-07T00:01:00+00:00',executor_running=False)
    assert observation(data,now=now)['elapsed_seconds']==60
    assert observation(data,now=now)['notice']==''
    assert observation({},now=now)['elapsed_seconds'] is None


def test_failed_execution_stops_pulse_and_retry_clears_old_error(workspace):
    path=w.folder(workspace['id'])
    w.update(path,error='old',stage='qc')
    try:
        with w.execution(path,interval=.01):
            assert w.runtime(path)['error'] is None
            assert w.runtime(path)['stage'] is None
            raise RuntimeError('synthetic interruption')
    except RuntimeError:
        pass
    assert not w.runtime(path)['executor_running']


def test_redaction_preserves_useful_error_and_hides_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','my-secret-key-value')
    raw='429 my-secret-key-value Bearer top-secret api_key=private https://user:pass@api.test/path?token=hidden'
    safe=redact(raw)
    assert '429' in safe and 'api.test/path' in safe
    for secret in ('my-secret-key-value','top-secret','private','user:pass','token=hidden'):
        assert secret not in safe
    assert guidance(raw,'ai_emphasis')['code']=='ai'


def test_error_actions_are_specific():
    assert guidance('可用空间不足')['code']=='disk_space'
    assert guidance('片段 1 时长不符：20s vs 2s')['code']=='qc'
    assert guidance('缺少剪辑必需正片')['code']=='source'
    assert guidance('排版超出边界','hook_overlay')['code']=='hook'
    assert guidance('等待 FBBM 就绪超时',continuation=True)['code']=='fbbm_continue'
    assert guidance('network error','preview')['code']=='preview'


def test_fbbm_retry_keeps_original_record_and_does_not_upload(workspace):
    w.start_batch(workspace['id'],1)
    path=w.folder(workspace['id']);rid=workspace['assets'][0]['request_id']
    video=path/'synthetic.mp4';video.write_bytes(b'test-output')
    from exactcut.util import file_sha256
    sha=file_sha256(video)
    entry={'status':'upload_queued','approved_sha256':sha,'publication':{'feishu_record_id':'original-record'},'error':'old failure'}
    write_json_atomic(path/'publications.json',{rid:entry})
    manifest,_=w.request_for(path,rid)
    with patch.object(w,'preview_file',return_value=(video,{'output_sha256':sha})), \
         patch('exactcut.publish.publish_to_queue') as upload, \
         patch('exactcut.publish.sync_record_once',return_value={'record_id':'original-record','action':'ready','video_id':'video-1'}) as sync:
        assert w.publish(workspace['id'],rid)==0
        upload.assert_not_called()
        sync.assert_called_once_with('original-record')
    saved=w.read_json(path/'publications.json')[rid]
    assert saved['status']=='fbbm_ready' and 'error' not in saved
    assert w.runtime(path)['stage']=='fbbm' and not w.runtime(path)['executor_running']


def test_same_platform_status_is_not_new_progress(workspace):
    w.start_batch(workspace['id'],1)
    path=w.folder(workspace['id']);rid=workspace['assets'][0]['request_id']
    video=path/'synthetic.mp4';video.write_bytes(b'test')
    from exactcut.util import file_sha256
    sha=file_sha256(video)
    write_json_atomic(path/'publications.json',{rid:{'approved_sha256':sha,'publication':{'feishu_record_id':'original'}}})
    results=[{'record_id':'original','action':'syncing','fb_status':'processing','checked_at':'first'},
             {'record_id':'original','action':'syncing','fb_status':'processing','checked_at':'second'},
             {'record_id':'original','action':'ready','fb_status':'ready','video_id':'done'}]
    with patch.object(w,'preview_file',return_value=(video,{'output_sha256':sha})), \
         patch('exactcut.publish.sync_record_once',side_effect=results), \
         patch.object(w.time,'sleep'),patch.object(w,'checkpoint',wraps=w.checkpoint) as checkpoints:
        assert w.publish(workspace['id'],rid)==0
    polls=[c.kwargs['advanced'] for c in checkpoints.call_args_list if 'advanced' in c.kwargs]
    assert polls==[True,False,True]


def test_api_errors_are_actionable_and_redacted(workspace,monkeypatch):
    from test_phase2_preview_api import serve,call
    monkeypatch.setattr(w,'save_draft',lambda *a: (_ for _ in ()).throw(ValueError('可用空间不足 api_key=private-test-key')))
    app=serve()
    try:
        status,body=call(app,'POST',f"/api/batches/{workspace['id']}/save",{'revision':1,'draft':workspace['draft']})
        assert status==400 and body['error_info']['code']=='disk_space'
        assert 'private-test-key' not in str(body)
    finally:
        app.shutdown();app.server_close()


def test_unverified_delivery_guidance_does_not_offer_blind_retry():
    result = guidance('待核实：交付阶段中断，只能核实原回执', 'fbbm')
    assert result['title'] == '交付结果待核实'
    assert '管理员' in result['action']
    assert '重试' not in result['action']


def test_fbbm_timeout_guidance_uses_recorded_quota_reason():
    message = '等待 FBBM 就绪超时，可重试继续查询原记录'
    result = guidance(message, 'fbbm', fbbm_status='quota_deferred')
    assert result['code'] == 'fbbm_quota'
    assert 'Meta API' in result['title']
    assert '无需重新制作' in result['action']
    assert guidance(message, 'fbbm')['code'] == 'fbbm_timeout'
    assert guidance(message, 'fbbm', continuation=True)['code'] == 'fbbm_continue'


def test_stale_quota_status_does_not_mask_current_delivery_error():
    for message in ['执行中断，可重试继续原记录', '执行进程已结束，可查看日志后重试']:
        result = guidance(message, 'fbbm', fbbm_status='quota_deferred')
        assert result['code'] == 'interrupted'
    result = guidance('待核实：交付阶段中断，只能核实原回执', 'fbbm', fbbm_status='quota_deferred')
    assert result['code'] == 'delivery_unverified'
    result = guidance('状态查询暂时失败，将自动重试：1255001', 'fbbm', fbbm_status='quota_deferred')
    assert result['code'] == 'delivery_read_retry'
