import base64
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from exactcut import workflow as w
from exactcut.intake import read_excel, validate_draft, download_plan
from exactcut.util import write_json_atomic, file_sha256

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'templates/精确剪辑_FBBM_填写模板.xlsx'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(w, 'read_excel', read_excel)  # Workflow tests isolate the external LLM; covered separately.
    monkeypatch.setattr(w, 'ROOT', tmp_path)
    monkeypatch.setattr(w, 'BATCHES', tmp_path / '.web-state/batches')
    monkeypatch.setattr(w, 'FEEDBACK', tmp_path / 'feedback')
    return tmp_path


def imported():
    item = w.import_excel({'filename': 'request.xlsx', 'content': base64.b64encode(TEMPLATE.read_bytes()).decode()})
    path = w.folder(item['id']) / 'draft.json'
    stored = json.loads(path.read_text())
    stored['draft']['author'] = '张三'  # Test fixture: director has been entered.
    write_json_atomic(path, stored)
    return w.detail(item['id'])


def complete_asset(batch):
    w.start_batch(batch['id'], batch['revision'])
    path = w.folder(batch['id'])
    manifest = json.loads((path / 'confirmed.json').read_text())
    request = manifest['requests'][0]
    video = path / 'production/videos' / request['output_filename']
    video.parent.mkdir(parents=True)
    video.write_bytes(b'pretend-video-for-upload-integrity-test')
    record = {'status': 'qc_passed', 'output_sha256': file_sha256(video), 'output': str(video)}
    write_json_atomic(path / 'production/results.json', {'requests': {request['request_id']: record}})
    w.update(path, 'review', 'Ready')
    return path, request, video, record


def test_excel_identifies_drama_and_ignores_empty_helper_rows():
    draft, warnings = read_excel(TEMPLATE)
    assert draft['drama_id'] == '101460'
    assert len(draft['rows']) == 1
    assert draft['rows'][0]['source'].endswith('第 7 行')
    assert 'EP1 00:15.000-02:04.000' in draft['rows'][0]['timeline_text']
    assert validate_draft(draft)[1] == []


def test_later_episodes_come_from_edited_timeline():
    draft, _ = read_excel(TEMPLATE)
    draft['rows'][0]['timeline_text'] += '\n第 12 集 00:10～00:15'
    manifest, errors = validate_draft(draft)
    assert not errors
    assert download_plan(manifest)['episode_limit'] == 12
    assert download_plan(manifest)['required'] == ['EP1', 'EP12']
    assert download_plan(manifest)['extra'] == ['EP12']


def test_invalid_content_never_confirms(workspace):
    item = imported()
    item['draft']['rows'][0]['timeline_text'] = 'EP1 10s-5s'
    saved = w.save_draft(item['id'], {'revision': item['revision'], 'draft': item['draft']})
    assert saved['errors']
    with pytest.raises(ValueError):
        w.start_batch(item['id'], saved['revision'])
    assert not (w.folder(item['id']) / 'confirmed.json').exists()


def test_draft_versions_and_confirmation_are_locked(workspace):
    batch = imported()
    saved = w.save_draft(batch['id'], {'revision': batch['revision'], 'draft': batch['draft']})
    with pytest.raises(ValueError, match='其他页面'):
        w.save_draft(batch['id'], {'revision': batch['revision'], 'draft': batch['draft']})
    command = w.start_batch(batch['id'], saved['revision'])
    assert command[-3:-1] == ['produce', batch['id']]
    assert command[-1].startswith('--instance=')
    with pytest.raises(ValueError, match='不可修改'):
        w.save_draft(batch['id'], {'revision': saved['revision'], 'draft': saved['draft']})
    with pytest.raises(ValueError, match='正在执行'):
        w.start_batch(batch['id'], saved['revision'])


def test_paths_and_duplicate_filenames_are_rejected(workspace):
    with pytest.raises(ValueError):
        w.folder('../../etc')
    batch = imported()
    bad = copy.deepcopy(batch['draft'])
    bad['rows'][0]['request_id'] = '../../etc'
    assert validate_draft(bad)[1]
    w.start_batch(batch['id'], 1)
    other = imported()
    assert other['assets'][0]['filename'] != batch['assets'][0]['filename']
    stored = json.loads((w.folder(other['id']) / 'draft.json').read_text())
    stored['draft']['rows'][0]['seq'] = batch['draft']['rows'][0]['seq']
    write_json_atomic(w.folder(other['id']) / 'draft.json', stored)
    with pytest.raises(ValueError, match='命名.*重复'):
        w.start_batch(other['id'], 1)


def test_only_qc_passed_exact_preview_can_be_published(workspace):
    batch = imported()
    path, request, video, record = complete_asset(batch)
    rid = request['request_id']
    with pytest.raises(ValueError, match='版本'):
        w.queue_publish(batch['id'], rid, 'old-hash')
    video.write_bytes(b'changed-video')
    with pytest.raises(ValueError, match='版本'):
        w.queue_publish(batch['id'], rid, record['output_sha256'])
    record['status'] = 'failed'
    write_json_atomic(path / 'production/results.json', {'requests': {rid: record}})
    with pytest.raises(ValueError, match='未通过'):
        w.preview_file(batch['id'], rid)


def test_publish_is_separate_and_reuses_remote_record_on_retry(workspace, monkeypatch):
    batch = imported()
    path, request, video, record = complete_asset(batch)
    rid = request['request_id']
    cmd = w.queue_publish(batch['id'], rid, record['output_sha256'])
    assert 'publish' in cmd and 'produce' not in cmd
    with patch('exactcut.publish.publish_to_queue', return_value={'feishu_record_id': 'record1', 'sha256': record['output_sha256'], 'oss_key': __import__('exactcut.publish', fromlist=['OssPublisher']).OssPublisher().key_for(video)[0]}) as upload, \
         patch('exactcut.publish.sync_record_once', return_value={'record_id': 'record1', 'action': 'failed'}):
        assert w.publish(batch['id'], rid) == 2
        assert upload.call_count == 1
    w.queue_publish(batch['id'], rid, record['output_sha256'])
    with patch('exactcut.publish.publish_to_queue') as upload, \
         patch('exactcut.publish.sync_record_once', return_value={'record_id': 'record1', 'action': 'ready', 'video_id': 'fb123'}):
        assert w.publish(batch['id'], rid) == 0
        upload.assert_not_called()
    assert w.detail(batch['id'])['assets'][0]['video_id'] == 'fb123'
    with pytest.raises(ValueError, match='无需重复'):
        w.queue_publish(batch['id'], rid, record['output_sha256'])


def test_feedback_persists_exact_asset_version(workspace):
    batch = imported()
    path, request, video, record = complete_asset(batch)
    feedback = w.add_feedback(batch['id'], {'request_id': request['request_id'], 'category': '剧情衔接', 'message': '00:12 衔接突兀'})
    assert feedback['sha256'] == record['output_sha256']
    assert w.detail(batch['id'])['feedback'] == [feedback]
    assert w.feedback_summary()['categories'] == {'剧情衔接': 1}
    assert w.feedback_summary()['entries'][0]['message'] == '00:12 衔接突兀'
    assert len(list((workspace / 'feedback').glob('*.json'))) == 1


def test_production_does_not_publish_and_uses_separate_outputs(workspace):
    batch = imported()
    w.start_batch(batch['id'], 1)
    with patch.object(w, 'download') as download, patch('exactcut.prepare.prepare_project') as prepare, \
         patch('exactcut.orchestrator.execute_batch', return_value={'requests': {'R1': {'status': 'qc_passed'}}}) as execute:
        assert w.produce(batch['id']) == 0
        download.assert_called_once()
        prepare.assert_not_called()
        assert execute.call_args.kwargs['output_dir'] == w.folder(batch['id']) / 'production'
        assert not execute.call_args.kwargs.get('publish')
        assert w.runtime(w.folder(batch['id']))['status'] == 'review'


def test_eight_available_episodes_are_complete_and_missing_required_blocks(workspace, monkeypatch):
    cache_root = workspace / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(cache_root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    batch = imported()
    path = w.folder(batch['id'])
    manifest, _ = validate_draft(batch['draft'])
    project = workspace / 'projects' / manifest.drama_id
    project.mkdir(parents=True, exist_ok=True)
    write_json_atomic(project / 'project.json', {'project_id': manifest.drama_id})
    cache = cache_root / manifest.drama_id
    cache.mkdir(parents=True)
    for episode in range(1, 9):
        (cache / f'EP{episode}.mp4').write_bytes(b'video')
    marker = workspace / 'fetch-called'
    fetch = workspace / 'fetch.py'
    fetch.write_text(f'open({str(marker)!r}, "w").write("called\\n")\n')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {fetch}')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda video: None)
    with patch('exactcut.media.ffprobe', return_value={'format':{'duration':'130'},'streams':[{'codec_type':'video'}]}):
        w.download(path, manifest, project)
        assert w.runtime(path)['download_complete']
        assert w.runtime(path)['reused_local_episodes'] == ['EP1']
        assert not marker.exists()
        manifest.requests[0].timeline[0].episode = 'EP12'
        with pytest.raises(RuntimeError, match='EP12'):
            w.download(path, manifest, project)
    assert (cache / 'EP1.mp4').read_bytes() == b'video'
    assert all((cache / f'EP{episode}.mp4').is_file() for episode in range(1, 9))
    text = (Path(__file__).resolve().parents[1] / 'exactcut' / 'workflow.py').read_text()
    assert 'Downloads/auto-cut' not in text and '.openclaw' not in text


def test_download_keeps_legacy_video_and_cache_hit(workspace, monkeypatch):
    from exactcut.models import BatchManifest, CutRequest, TimelineItem
    cache_root = workspace / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(cache_root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_ALLOW_MATERIAL_FETCH', '0')
    batch = imported()
    path = w.folder(batch['id'])
    manifest = BatchManifest(
        drama_id='101573', drama_title='剧', business_date='20261008', author='张三',
        source_workbook='x.xlsx', requests=[CutRequest(
            request_id='R1', seq=1, output_filename='out.mp4',
            timeline=[
                TimelineItem(type='episode', episode='EP1', start_ms=0, end_ms=1000),
                TimelineItem(type='episode', episode='EP2', start_ms=0, end_ms=1000),
            ],
        )],
    )
    project = workspace / 'projects' / '101573'
    project.mkdir(parents=True)
    write_json_atomic(project / 'project.json', {'project_id': '101573'})
    legacy = project / 'source' / 'videos'
    legacy.mkdir(parents=True)
    (legacy / 'EP1.mp4').write_bytes(b'legacy')
    cached = cache_root / '101573'
    cached.mkdir(parents=True)
    (cached / 'EP2.mp4').write_bytes(b'cached')
    marker = workspace / 'fetch-called'
    fetch = workspace / 'fetch.py'
    fetch.write_text(f'open({str(marker)!r}, "w").write("called\\n")\n')
    monkeypatch.setenv('EXACTCUT_MATERIAL_FETCH_CMD', f'{sys.executable} {fetch}')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda video: None)
    with patch('exactcut.media.ffprobe', return_value={'format': {'duration': '130'}, 'streams': [{'codec_type': 'video'}]}):
        w.download(path, manifest, project)
    assert w.runtime(path)['reused_local_episodes'] == ['EP1', 'EP2']
    assert not marker.exists()
    assert (legacy / 'EP1.mp4').read_bytes() == b'legacy'
    assert not (cached / 'EP1.mp4').exists()
    assert (cached / 'EP2.mp4').read_bytes() == b'cached'


def test_download_reports_current_episode_progress_and_resets_after_completion(workspace, monkeypatch):
    import exactcut.source_cache as source_cache
    from exactcut.models import BatchManifest, CutRequest, TimelineItem
    cache_root = workspace / 'sources'
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(cache_root))
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    item = w.create_blank('测试')
    path = w.folder(item['id'])
    manifest = BatchManifest(
        drama_id='101573', drama_title='剧', business_date='20261008', author='测试',
        source_workbook='x.xlsx', requests=[CutRequest(
            request_id='R1', seq=1, output_filename='out.mp4',
            timeline=[TimelineItem(type='episode', episode='EP1', start_ms=0, end_ms=1000)],
        )],
    )
    project = workspace / 'projects' / '101573'
    project.mkdir(parents=True)
    write_json_atomic(project / 'project.json', {'project_id': '101573'})
    directory = cache_root / '101573'
    directory.mkdir(parents=True)
    video = directory / 'EP1.mp4'

    def fake_ensure(drama_id, episodes, on_progress=None):
        assert drama_id == '101573'
        assert list(episodes) == ['EP1']
        video.write_bytes(b'x' * 2048)
        on_progress('EP1', 1_048_576, 4_194_304)
        on_progress('EP1', 2_097_152, 4_194_304)
        return {'EP1': video}

    monkeypatch.setattr(source_cache, 'ensure', fake_ensure)
    monkeypatch.setattr(source_cache, 'reused_episodes', lambda: [])
    calls = []
    real_update = w.update

    def spy(update_path, *args, **kwargs):
        calls.append(dict(kwargs))
        return real_update(update_path, *args, **kwargs)

    monkeypatch.setattr(w, 'update', spy)
    with patch('exactcut.media.ffprobe', return_value={'format': {'duration': '130'}, 'streams': [{'codec_type': 'video'}]}):
        w.download(path, manifest, project)

    progress = [record for record in calls if record.get('download_current_episode') == 'EP1']
    assert len(progress) == 2
    first, second = progress
    assert first['download_current_received'] == 1_048_576
    assert first['download_current_total'] == 4_194_304
    assert first['download_files'] == 1
    assert first['download_bytes'] == 1_048_576 + 2048
    assert first['download_activity_at']
    assert second['download_current_received'] == 2_097_152
    assert second['download_speed_bps'] > 0
    runtime = w.runtime(path)
    assert runtime['download_complete'] is True
    assert runtime['download_current_episode'] is None
    assert runtime['download_speed_bps'] is None


def test_authenticated_video_range_and_feedback_http(workspace):
    import threading
    import requests
    from http.server import ThreadingHTTPServer
    from exactcut.webserver import Handler
    batch = imported()
    path, request, video, record = complete_asset(batch)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.password = 'test-password-with-more-than-24-chars'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = requests.Session(); client.trust_env = False
    base = f'http://127.0.0.1:{server.server_port}'
    url = base + '/api/batches/' + batch['id'] + '/video/' + request['request_id']
    try:
        assert client.get(url).status_code == 401
        client.auth = ('admin', server.password)
        response = client.get(url, headers={'Range': 'bytes=2-8'})
        assert response.status_code == 206
        assert response.content == video.read_bytes()[2:9]
        assert response.headers['Content-Range'].startswith('bytes 2-8/')
        assert client.get(url, headers={'Range':'bytes=99999-'}).status_code == 416
        response = client.post(base + f'/api/batches/{batch["id"]}/feedback', json={'request_id': request['request_id'], 'category': '剪辑节奏', 'message': '节奏过慢'}, headers={'X-Exactcut-Request':'1'})
        assert response.status_code == 201
        assert client.get(base+'/api/feedback').json()['total'] == 1
    finally:
        server.shutdown(); server.server_close()


def test_title_card_copy_is_not_rewritten_by_time_normalization():
    from exactcut.intake import normalized_timeline
    assert normalized_timeline('字卡|分手到重逢：最后一秒|1.5s') == '字卡|分手到重逢：最后一秒|1.5s'


def test_new_intake_reads_director_and_uses_lowercase_agent(workspace):
    item = w.import_excel({'filename': 'request.xlsx', 'content': base64.b64encode(TEMPLATE.read_bytes()).decode()})
    assert item['draft']['source_author'] == '张三'
    assert item['draft']['author'] == '张三'
    assert not item['errors']
    item['draft'].update(author='徐明飞', creator2='AGENT')
    saved = w.save_draft(item['id'], {'revision': 1, 'draft': item['draft']})
    assert saved['draft']['creator2'] == 'agent'
    assert saved['assets'][0]['filename'] == '20260902_typezxraw_徐明飞_agent_1_#101460#.mp4'


def test_creative_script_delimiters_and_minute_second_times():
    from exactcut.intake import normalized_timeline, key_of
    assert key_of('创意脚本') == 'timeline'
    assert key_of('标题短文案') == 'hook_text'
    raw = '正文画面——EP2：1s-8s，，EP1：20s-2.27s，，EP2：1.52s-2.14s'
    assert normalized_timeline(raw, 'minutes_seconds') == 'EP2 00:01.000-00:08.000\nEP1 00:20.000-02:27.000\nEP2 01:52.000-02:14.000'
    assert normalized_timeline('EP4，，EP5：1.22s', 'minutes_seconds') == 'EP4 全集\nEP5 01:22.000-结尾'
    # Existing standard segment syntax retains real decimal-second semantics.
    assert normalized_timeline('EP1 1.52s-2.27s') == 'EP1 00:01.520-00:02.270'


def test_incomplete_script_segments_do_not_get_silently_dropped():
    draft, _ = read_excel(TEMPLATE)
    draft['rows'][0]['timeline_text'] = 'EP4'
    manifest, errors = validate_draft(draft)
    assert not errors
    assert manifest.requests[0].timeline[0].full_episode
    draft['rows'][0]['timeline_text'] = 'EP5:01:22'
    manifest, errors = validate_draft(draft)
    assert not errors
    assert manifest.requests[0].timeline[0].to_end
    assert manifest.requests[0].timeline[0].start_ms == 82000


def test_whole_episode_resolves_downloaded_duration_without_changing_confirmation(tmp_path):
    from exactcut.media import resolve_full_episodes
    draft, _ = read_excel(TEMPLATE)
    draft['rows'][0]['timeline_text'] = 'EP4 全集\nEP4 10s-20s\nEP4'
    manifest, errors = validate_draft(draft)
    assert not errors
    with patch('exactcut.media.resolve_episode_video', return_value=tmp_path/'EP4.mp4'), patch('exactcut.media.duration_ms', return_value=123456) as probe:
        resolved = resolve_full_episodes(manifest, tmp_path)
    assert probe.call_count == 1
    assert [(x.start_ms,x.end_ms) for x in resolved.requests[0].timeline] == [(0,123456),(10000,20000),(0,123456)]
    assert manifest.requests[0].timeline[0].full_episode
    assert manifest.requests[0].timeline[0].end_ms is None
    assert not resolved.requests[0].timeline[0].full_episode


def test_to_end_uses_real_duration_and_rejects_start_past_end(tmp_path):
    from exactcut.media import resolve_full_episodes
    draft, _ = read_excel(TEMPLATE)
    draft['rows'][0]['timeline_text'] = 'EP5 01:22-结尾'
    manifest, errors = validate_draft(draft)
    assert not errors
    with patch('exactcut.media.resolve_episode_video', return_value=tmp_path/'EP5.mp4'), patch('exactcut.media.ffprobe', return_value={'format':{'duration':'130'},'streams':[{'codec_type':'video'}]}):
        item = resolve_full_episodes(manifest,tmp_path).requests[0].timeline[0]
        assert (item.start_ms,item.end_ms,item.duration_ms)==(82000,130000,48000)
    with patch('exactcut.media.resolve_episode_video', return_value=tmp_path/'EP5.mp4'), patch('exactcut.media.duration_ms', return_value=81000):
        with pytest.raises(RuntimeError,match='实际时长'):
            resolve_full_episodes(manifest,tmp_path)


def test_document_director_plus_order_typo_and_decimal_scope():
    from exactcut.intake import normalized_timeline, conversion_notes
    raw = '正文画面——、EP3：2s-35s+53s-1.45s，，EP1：0.5s-1.49s，，EP3，，EP1：32s-1..44s'
    assert normalized_timeline(raw,'minutes_seconds').splitlines() == [
        'EP3 00:02.000-00:35.000', 'EP3 00:53.000-01:45.000',
        'EP1 00:05.000-01:49.000', 'EP3 全集', 'EP1 00:32.000-01:44.000']
    assert any('1..44s' in note for note in conversion_notes(raw, True))
    assert normalized_timeline('EP1 0.5s-1.49s') == 'EP1 00:00.500-00:01.490'
    draft,_=read_excel(TEMPLATE)
    draft['time_notation']='minutes_seconds'
    draft['rows'][0]['timeline_text']='EP1 0s-1.60s'
    assert any('秒位不能达到 60' in e for e in validate_draft(draft)[1])


def test_multidrama_import_groups_archives_without_creating_authorization(workspace):
    from datetime import datetime
    class Sheet:
        title='Sheet1';max_row=4;max_column=4
        def iter_rows(self, **kwargs):
            return iter([
                ('创意脚本','剧ID','标题短文案','日期'),
                ('EP1：0.5s-1.49s',101573,'',datetime(2026,9,22)),
                ('EP2',101574,'Hook B',datetime(2026,9,22)),
                ('EP1：1.22s',101573,'Hook C',datetime(2026,9,22)),
            ])
    class Book:
        worksheets=[Sheet()]
        def close(self):pass
    with patch('exactcut.intake.load_workbook',return_value=Book()):
        result=w.import_excel({'filename':'multi.xlsx','content':base64.b64encode(TEMPLATE.read_bytes()).decode()})
    assert [(b['drama_id'],b['count']) for b in result['imported_batches']]==[('101573',2),('101574',1)]
    assert result['draft']['rows'][0]['hook_text']==''
    assert result['draft']['rows'][0]['structure']==''
    for b in result['imported_batches']:
        project=workspace/'projects'/b['drama_id']
        archive=project/'requests'/('import-'+b['id'])
        assert (archive/'original.xlsx').read_bytes()==TEMPLATE.read_bytes()
        data=json.loads((archive/'normalized.json').read_text())
        assert '片段1' in data['draft']['rows'][0]
        assert not (project/'project.json').exists()
    other=w.detail(result['imported_batches'][1]['id'])
    assert other['draft']['rows'][0]['source']=='Sheet1 · 第 3 行'


def test_pause_archive_http_and_execution_guards(workspace):
    import threading
    import requests
    from http.server import ThreadingHTTPServer
    from exactcut.webserver import Handler
    batch = imported()
    path = w.folder(batch['id'])
    w.update(path, 'queued', 'Waiting')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.password = 'test-password-with-more-than-24-chars'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/api/batches/{batch["id"]}'
    try:
        with requests.Session() as client:
            client.auth = ('admin', server.password)
            client.headers['X-Exactcut-Request'] = '1'
            assert client.post(base+'/pause', json={}).json()['runtime']['status'] == 'paused'
            assert client.post(base+'/start', json={'revision': batch['revision']}).status_code == 400
            assert client.post(base+'/archive', json={}).json()['runtime']['archived']
            assert client.post(base+'/resume', json={}).status_code == 400
            restored = client.post(base+'/unarchive', json={}).json()
            assert restored['runtime']['paused']
            assert client.post(base+'/resume', json={}).json()['runtime']['status'] == 'queued'
            assert w.runtime(path)['status'] == 'queued'
            assert (path / 'draft.json').exists()
    finally:
        server.shutdown()
        server.server_close()


def test_permanent_delete_only_archived_batch_and_preserves_shared_files(workspace):
    from exactcut import batch_control
    batch = imported()
    path = w.folder(batch['id'])
    with pytest.raises(ValueError, match='确认'):
        w.delete_batch(batch['id'], None)
    with pytest.raises(ValueError, match='归档'):
        w.delete_batch(batch['id'], batch['id'])
    sibling = imported()
    media = workspace / 'projects' / batch['draft']['drama_id'] / 'source/videos/EP1.mp4'
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b'shared media')
    w.FEEDBACK.mkdir()
    (w.FEEDBACK / (batch['id']+'-test.json')).write_text('{}')
    jobs = workspace / '.web-state/jobs'
    jobs.mkdir()
    (jobs / 'test.json').write_text(json.dumps({'batch_id':batch['id'],'status':'queued'}))
    (jobs / 'test.log').write_text('log')
    batch_control.change(path, 'archive')
    assert w.delete_batch(batch['id'], batch['id']) == {'deleted':batch['id']}
    assert not path.exists()
    assert not (media.parents[2] / 'requests' / ('import-'+batch['id'])).exists()
    assert not list(w.FEEDBACK.glob(batch['id']+'-*'))
    assert not list(jobs.iterdir())
    assert media.read_bytes() == b'shared media'
    assert w.folder(sibling['id']).exists()
    assert batch['id'] not in [b['id'] for b in w.summaries()]


def test_delete_terminates_archived_process_before_removing_files(workspace, monkeypatch):
    import subprocess
    import sys
    from exactcut import batch_control
    batch = imported()
    path = w.folder(batch['id'])
    token = 'a'*32
    child = subprocess.Popen([sys.executable, '-X', 'exactcut_process_token=' + token,
                               '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        batch_control.remember_child(path, batch_control.identify(child, process_token=token, lane='encode'))
        batch_control.change(path, 'archive')
        w.delete_batch(batch['id'], batch['id'])
        assert child.wait(timeout=3) != 0
        assert not path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_accounts_http_isolation_sessions_and_management(workspace):
    import requests
    import threading
    from http.server import ThreadingHTTPServer
    from exactcut.webserver import Handler
    from exactcut import accounts
    batch = imported()  # Legacy batches belong to admin.
    db = workspace / 'accounts.sqlite3'
    password = 'administrator-test-password'
    accounts.initialize(db, password)
    accounts.manage(db,'admin',{'username':'alice','name':'Alice','password':'alice-password-123'})
    accounts.manage(db,'admin',{'username':'bob','name':'Bob','password':'bob-password-123'})
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    server.password, server.auth_db = password, db
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    def client(username,password):
        c=requests.Session();c.headers['X-Exactcut-Request']='1'
        assert c.post(base+'/api/login',json={'username':username,'password':password}).status_code==200
        return c
    try:
        admin=client('admin',password);alice=client('alice','alice-password-123');bob=client('bob','bob-password-123')
        assert alice.get(base+'/api/batches').json()['batches']==[]
        created=alice.post(base+'/api/batches/import',json={'filename':'a.xlsx','content':base64.b64encode(TEMPLATE.read_bytes()).decode(),'owner':'bob'}).json()
        bid=created['id'];url=base+'/api/batches/'+bid
        assert accounts.owner(w.folder(bid))=='alice'
        assert created['draft']['author']=='张三'
        assert len(admin.get(base+'/api/batches').json()['batches'])==2
        assert len(alice.get(base+'/api/batches').json()['batches'])==1
        for suffix in ['', '/manifest','/video/R001','/download-log']:
            assert bob.get(url+suffix).status_code==404
        for action in ['save','start','publish','clone','feedback','pause','resume','archive','unarchive','delete']:
            assert bob.post(url+'/'+action,json={}).status_code==404,action
        for legacy in ['/api/state','/api/users','/api/jobs/'+'a'*32+'/log']:
            assert alice.get(base+legacy).status_code==403
        for legacy in ['/api/upload','/api/projects','/api/jobs','/api/users']:
            assert alice.post(base+legacy,json={}).status_code==403
        created['draft']['author']='Bob'
        saved=alice.post(url+'/save',json={'draft':created['draft'],'revision':created['revision']}).json()
        assert saved['draft']['author']=='Bob'
        assert accounts.owner(w.folder(bid))=='alice'
        clone=alice.post(url+'/clone',json={}).json()
        assert accounts.owner(w.folder(clone['id']))=='alice'
        w.FEEDBACK.mkdir()
        write_json_atomic(w.FEEDBACK/'a.json',{'batch_id':bid,'category':'剪辑节奏','created_at':'2026-01-01'})
        assert bob.get(base+'/api/feedback').json()['total']==0
        assert alice.get(base+'/api/feedback').json()['total']==1
        assert alice.post(base+'/api/logout',json={}).status_code==200
        assert alice.get(url).status_code==401
        alice=client('alice','alice-password-123')
        assert admin.post(base+'/api/users',json={'action':'disable','username':'alice'}).status_code==200
        assert alice.get(url).status_code==401
        assert admin.post(base+'/api/users',json={'action':'enable','username':'alice'}).status_code==200
        alice=client('alice','alice-password-123')
        assert admin.post(base+'/api/users',json={'action':'reset_password','username':'alice','password':'changed-password-123'}).status_code==200
        assert alice.get(url).status_code==401
        assert accounts.authenticate(db,'alice','alice-password-123') is None
        assert accounts.authenticate(db,'alice','changed-password-123')['username']=='alice'
        assert accounts.authenticate(db,'admin',password)['role']=='admin'
    finally:
        server.shutdown();server.server_close()


def test_can_queue_sibling_while_uploading_and_preserve_both(workspace):
    batch = imported()
    path, request, video, record = complete_asset(batch)
    w.queue_publish(batch['id'], request['request_id'], record['output_sha256'])
    with pytest.raises(ValueError, match='已在上传队列'):
        w.queue_publish(batch['id'], request['request_id'], record['output_sha256'])
    first = json.loads((path / 'publications.json').read_text())[request['request_id']]
    with patch.object(w, 'preview_file', return_value=(video, record)):
        w.queue_publish(batch['id'], 'R002', record['output_sha256'])
    first['status'] = 'fbbm_ready'
    w.store_publication(path, request['request_id'], first)
    publications = json.loads((path / 'publications.json').read_text())
    assert publications['R002']['status'] == 'upload_queued'
    assert publications[request['request_id']]['status'] == 'fbbm_ready'
