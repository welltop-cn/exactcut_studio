"""只读预热、finished-skip 与真实调度隔离边界的最小整合回归。"""
from contextlib import contextmanager
import json

from exactcut import diagnostics as diag, finished_skip as fs, tick_snapshot as snap
from exactcut import webserver as web, workflow as w
from exactcut.util import read_json, write_json_atomic
from test_concurrency import runner, queued
from test_workflow import workspace
from test_worker_bad_job_isolation import isolated
from test_worker_tick_behavior_lock import scheduler, batch, finish_owned, saved


def test_readonly_warm_cache_cannot_bypass_scheduler_isolation(scheduler, isolated, monkeypatch):
    pending, owned = scheduler
    monkeypatch.setenv('EXACTCUT_SKIP_FINISHED', '1')
    monkeypatch.setattr(fs._local, 'memory', fs.Memory(), raising=False)
    finished = batch('f')
    write_json_atomic(finished / 'runtime.json', {'status': 'complete', 'events': []})
    skips = []
    original_check = fs.Check.__init__

    def checked(self, *args, **kwargs):
        original_check(self, *args, **kwargs)
        if self.path == finished: skips.append(self.skip)

    monkeypatch.setattr(fs.Check, '__init__', checked)
    web.worker_tick(pending, owned)
    web.worker_tick(pending, owned)
    assert True in skips, '必须实际命中 finished-skip，而非只设置开关'

    invalid = queued('a', 'fetch', batch='invalid-batch')
    invalid_path = w.BATCHES / 'invalid-batch'
    invalid_path.mkdir()
    write_json_atomic(invalid_path / 'draft.json', {})
    missing = batch('d')
    stale = queued('b', 'fetch', batch=missing.name)
    stale.update(action='prefetch', status='running')
    web.save(stale)
    broken = web.JOBS / 'broken.json'
    original_bytes = b'{bad\n  '
    broken.write_bytes(original_bytes)
    healthy = queued('c', 'fetch')
    scope = snap.scope
    skips.clear()

    @contextmanager
    def warmed():
        with scope():
            readonly = list(web.stored_jobs())
            assert stale['id'] in {j['id'] for j in readonly}
            snap.job_list(web.JOBS, lambda: readonly)
            assert saved(invalid)['status'] == 'queued'
            assert not (web.JOBS.parent / 'job-quarantine').exists()
            # 原始 JSON 仍在缓存中；草稿消失后也必须重新验证。
            (missing / 'draft.json').unlink()
            yield

    with monkeypatch.context() as patch:
        patch.setattr(snap, 'scope', warmed)
        web.worker_tick(pending, owned)
    assert True in skips, '隔离坏任务的同轮也必须实际命中 finished-skip'
    assert list(owned) == [healthy['id']], '坏任务不能占槽或阻断同轮正常任务'
    assert saved(healthy)['status'] == 'running'
    assert saved(invalid)['status'] == 'failed'
    assert saved(stale)['status'] == 'interrupted'
    for job in (invalid, stale): assert saved(job)['quarantine']['reason']
    assert broken.read_bytes() == original_bytes
    assert read_json(web.JOBS.parent / 'job-quarantine/broken.json.json')['reason']
    finish_owned(owned)
    diag._throttled.clear()
    web.recover_jobs(enqueue=True)
    web.worker_tick(pending, owned)
    assert saved(healthy)['status'] == 'completed' and not owned
    assert broken.read_bytes() == original_bytes
    alerts = [json.loads(line) for file in (isolated / 'logs').glob('events-*.jsonl')
              for line in file.read_text().splitlines()]
    alerts = [row for row in alerts if row['event'] == 'job.quarantined']
    assert sorted(row['job_id'] for row in alerts) == sorted([invalid['id'], stale['id'], 'broken'])
    assert all(row['level'] == 'error' and row['reason'] for row in alerts)
