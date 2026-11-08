"""素材下载中反复请求 sources 不应重复建预取任务。"""
import json
import queue
import threading

import pytest

from exactcut import runtime, source_prefetch, workflow as w
from exactcut import webserver as web
from test_source_frames import source  # noqa: F401
from test_web_base_path import app  # noqa: F401
from test_source_frame_timecode import draft, frame_clip

pytestmark = pytest.mark.integration


@pytest.fixture
def env(source, monkeypatch):
    _, item, _, _ = source
    monkeypatch.setattr(runtime, 'require_material_fetch', lambda: None)
    monkeypatch.setattr(web, 'JOBS', w.ROOT / '.web-state/jobs')
    web.JOBS.mkdir(exist_ok=True)
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setattr(source_prefetch, 'cached', lambda *a, **k: False)
    return item['id']


def prefetch_jobs(batch_id=None):
    jobs = [json.loads(p.read_text()) for p in web.JOBS.glob('*.json')]
    return [j for j in jobs if j.get('action') == 'prefetch' and (batch_id is None or j.get('batch_id') == batch_id)]


def set_status(job_id, status):
    path = web.JOBS / f'{job_id}.json'
    job = json.loads(path.read_text())
    job['status'] = status
    path.write_text(json.dumps(job))


def test_repeated_requests_reuse_queued_and_running_job(env):
    data = draft(frame_clip(episode='EP2'))
    first = web.prefetch_sources(env, {'draft': data}, 'admin')
    assert [e['status'] for e in first['episodes']] == ['queued']
    for _ in range(5):
        again = web.prefetch_sources(env, {'draft': data}, 'admin')
        assert [e['status'] for e in again['episodes']] == ['queued']
    assert len(prefetch_jobs(env)) == 1
    set_status(prefetch_jobs(env)[0]['id'], 'running')
    web.prefetch_sources(env, {'draft': data}, 'admin')
    assert len(prefetch_jobs(env)) == 1
    assert web.WORK.qsize() == 1


def test_concurrent_requests_create_one_job(env):
    data = draft(frame_clip(episode='EP2'))
    barrier = threading.Barrier(6)
    errors = []

    def call():
        try:
            barrier.wait()
            web.prefetch_sources(env, {'draft': data}, 'admin')
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(prefetch_jobs(env)) == 1


@pytest.mark.parametrize('ended', ['failed', 'interrupted'])
def test_ended_job_can_be_retried(env, ended):
    data = draft(frame_clip(episode='EP2'))
    web.prefetch_sources(env, {'draft': data}, 'admin')
    set_status(prefetch_jobs(env)[0]['id'], ended)
    web.prefetch_sources(env, {'draft': data, 'retry': True}, 'admin')
    assert len(prefetch_jobs(env)) == 2


def test_completed_job_without_material_allows_new_job(env):
    data = draft(frame_clip(episode='EP2'))
    web.prefetch_sources(env, {'draft': data}, 'admin')
    set_status(prefetch_jobs(env)[0]['id'], 'completed')
    result = web.prefetch_sources(env, {'draft': data}, 'admin')
    assert [e['status'] for e in result['episodes']] == ['queued']
    assert len(prefetch_jobs(env)) == 2


def test_changed_episodes_get_new_job_for_new_material_only(env):
    web.prefetch_sources(env, {'draft': draft(frame_clip(episode='EP2'))}, 'admin')
    data = draft(frame_clip(episode='EP2'))
    data['rows'][0]['clips'].append(frame_clip(episode='EP3'))
    result = web.prefetch_sources(env, {'draft': data}, 'admin')
    assert {e['episode']: e['status'] for e in result['episodes']} == {'EP2': 'queued', 'EP3': 'queued'}
    jobs = prefetch_jobs(env)
    assert len(jobs) == 2
    assert sorted(tuple(j['episodes']) for j in jobs) == [('EP2',), ('EP3',)]
    web.prefetch_sources(env, {'draft': data}, 'admin')
    assert len(prefetch_jobs(env)) == 2


def test_other_project_job_does_not_block(env):
    other = w.create_blank()['id']
    data = draft(frame_clip(episode='EP2'))
    web.prefetch_sources(other, {'draft': data}, 'admin')
    web.prefetch_sources(env, {'draft': data}, 'admin')
    assert len(prefetch_jobs(other)) == 1
    assert len(prefetch_jobs(env)) == 1
