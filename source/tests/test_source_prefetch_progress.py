"""原片预取进度文案与状态区分。"""
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from exactcut import source_prefetch as prefetch
from exactcut.util import write_json_atomic


def test_format_episode_progress_shows_mb_and_percent():
    text = prefetch.format_episode_progress({
        'episode': 'EP2', 'status': 'downloading',
        'received_bytes': 1048576, 'total_bytes': 4194304,
    })
    assert text.startswith('EP2 下载中')
    assert 'MB' in text and '25%' in text
    assert prefetch.format_episode_progress({'episode': 'EP4', 'status': 'indexing'}) == 'EP4 建帧索引'


def test_status_keeps_downloading_and_marks_indexing(tmp_path, monkeypatch):
    drama = '101494'
    path = tmp_path / 'batch'
    path.mkdir()
    write_json_atomic(path / 'source-prefetch.json', {
        'drama_id': drama, 'episodes': ['EP1', 'EP2'], 'frame_episodes': ['EP1', 'EP2'],
    })
    job = {'id': 'a' * 32, 'project': drama, 'episodes': ['EP1', 'EP2'],
           'status': 'running', 'created': 1, 'action': 'prefetch'}
    write_json_atomic(prefetch.result_path(path, job['id']), {
        'status': 'running', 'updated_at': '2026-01-01T00:00:00+00:00',
        'episodes': {
            'EP1': {'status': 'downloading', 'received_bytes': 50, 'total_bytes': 100},
            'EP2': {'status': 'indexing'},
        },
    })
    monkeypatch.setattr(prefetch, 'cached', lambda d, ep: ep == 'EP2')
    monkeypatch.setattr(prefetch, 'frame_index_ready', lambda *a, **k: False)
    monkeypatch.setattr(prefetch, 'signature', lambda *a, **k: None)
    manifest = SimpleNamespace(frame_episodes={'EP1', 'EP2'}, requests=[])
    states = {e['episode']: e for e in prefetch.status(path, drama, ['EP1', 'EP2'], [job], manifest)['episodes']}
    assert states['EP1']['status'] == 'downloading'
    assert states['EP1']['received_bytes'] == 50
    assert states['EP2']['status'] == 'indexing'


def test_episode_readiness_message_uses_latest_result(tmp_path, monkeypatch):
    drama = '101494'
    path = tmp_path / 'batch'
    (path / 'source-prefetch-results').mkdir(parents=True)
    write_json_atomic(path / 'source-prefetch.json', {
        'drama_id': drama, 'episodes': ['EP4'], 'frame_episodes': ['EP4'],
    })
    write_json_atomic(path / 'source-prefetch-results' / ('b' * 32 + '.json'), {
        'updated_at': '2026-01-02T00:00:00+00:00',
        'episodes': {'EP4': {'status': 'downloading', 'received_bytes': 2097152, 'total_bytes': 4194304}},
    })
    monkeypatch.setattr(prefetch, 'frame_index_ready', lambda *a, **k: False)
    monkeypatch.setattr(prefetch, 'cached', lambda *a, **k: False)
    message = prefetch.episode_readiness_message(path, drama, 'EP4')
    assert message.startswith('EP4 下载中')
    assert '请稍后再保存并校验' in message


def test_resolve_frame_clips_mentions_episode_progress(tmp_path, monkeypatch):
    from exactcut.source_frames import resolve_frame_clips, ClipValidationError
    from exactcut import source_frames
    path = tmp_path / 'batch'
    path.mkdir()
    monkeypatch.setattr(source_frames, '_video', lambda drama, episode: path / f'{episode}.mp4')
    monkeypatch.setattr(source_frames, '_fingerprint', lambda source: 'sig')
    (path / 'EP2.mp4').write_bytes(b'x')
    monkeypatch.setattr(prefetch, 'episode_readiness_message',
                        lambda *a, **k: 'EP2 建帧索引，请稍后再保存并校验')
    row = {'request_id': 'R1', 'clips': [{
        'episode': '2', 'time_format': 'source_frames_v1',
        'start_m': '0', 'start_s': '1', 'start_f': '0',
        'end_m': '0', 'end_s': '2', 'end_f': '0',
    }]}
    with pytest.raises(ClipValidationError) as exc:
        resolve_frame_clips(row, '101494', path)
    assert 'EP2' in str(exc.value)
    assert '建帧索引' in str(exc.value)
