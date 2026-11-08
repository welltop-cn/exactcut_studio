"""C0：当前全量扫描和后续跳过优化共同遵守的等价性基线。"""
import copy
import queue
import socket
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from exactcut import batch_control, material_versions as m, material_workers, webserver as web, workflow as w
from exactcut.runtime import instance_marker
from exactcut.util import read_json, write_json_atomic
from skip_equivalence_helpers import (
    api_snapshot, assert_api_equal, assert_state_unchanged, state_digest,
)
from test_material_versions import candidate_job, enrolled, token
from test_workflow import workspace, imported


@pytest.fixture
def finished(workspace, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setattr(web, 'ROOT', workspace)
    monkeypatch.setattr(web, 'JOBS', workspace / '.web-state/jobs')
    web.JOBS.mkdir(parents=True)
    monkeypatch.setattr(web, 'WORK', queue.Queue())
    monkeypatch.setattr(batch_control, 'PROCESSES', batch_control.FakeProcessTable({}))
    monkeypatch.setattr(m, 'delivery_enabled', lambda: True)

    def forbidden(*args, **kwargs):
        pytest.fail('等价性测试禁止联网或启动真实子进程')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    parents, candidates, deliveries = [], [], []

    def save_job(path, phase, status, *, identity=None, **extra):
        job = dict(id=identity or token(), batch_id=path.name, phase=phase, lane=phase,
                   action='start' if phase in {'fetch', 'render'} else 'publish',
                   status=status, created=1, owner='alice', actor='alice',
                   instance=instance_marker(), command=[sys.executable, '-c', 'pass'])
        if status in {'completed', 'failed', 'interrupted'}:
            # 终态任务保留完整计时，接口无需用当前时间推算耗时。
            job.update(started=3, finished=4)
        job.update(extra)
        web.save(job)
        return job

    for _ in range(2):
        _, parent, state = enrolled(media=True)
        write_json_atomic(parent / 'access.json', {'owner': 'alice'})
        rid = state['draft']['rows'][0]['request_id']
        vid = state['assets'][rid]['current_version']
        state = m.queue_delivery(parent, dict(revision=state['revision'], mutation_id=token(),
                                             request_id=rid, version_id=vid,
                                             sha256=state['versions'][vid]['record']['output_sha256']))
        delivery = next(e for e in state['outbox'].values() if e['kind'] == 'delivery')
        child = material_workers.materialize(parent, delivery)
        write_json_atomic(child / 'publications.json', {rid: {'status': 'publish_failed',
                                                            'approved_sha256': delivery['sha256']}})
        w.update(child, 'partial')
        upload = save_job(child, 'upload', 'completed', identity=delivery['job_id'], request_id=rid)
        save_job(child, 'poll', 'completed',
                 identity=uuid.uuid5(uuid.NAMESPACE_URL, upload['id'] + ':poll').hex, request_id=rid)
        draft = copy.deepcopy(state['draft'])
        draft['rows'][0]['hook_text'] += ' 修改候选'
        state = m.save_draft(parent, dict(revision=state['revision'], mutation_id=token(), draft=draft))
        state = m.confirm(parent, dict(revision=state['revision'], mutation_id=token()))
        _, candidate, event, failed_job = candidate_job(parent, state)
        failed_job.update(status='failed', command=[sys.executable, '-c', 'pass'],
                          created=2, started=3, finished=4)
        web.save(failed_job)
        w.update(candidate, 'interrupted')
        fetch = save_job(candidate, 'fetch', 'completed', identity=event['job_id'])
        save_job(candidate, 'render', 'completed', request_id=rid,
                 identity=uuid.uuid5(uuid.NAMESPACE_URL, fetch['id'] + ':render').hex)
        with m.locked(parent):
            state = m._read(parent)
            state['versions'][event['version_id']]['status'] = 'failed'
            for entry in state['outbox'].values():
                entry['status'] = 'materialized'
            state['revision'] += 1
            m._write(parent, state)
        material_workers._merge_delivery(parent, delivery, read_json(child / 'publications.json')[rid])
        parents.append(parent)
        candidates.append((candidate, event, failed_job))
        deliveries.append((child, delivery))

    legacy = []
    for status in ('review', 'complete'):
        path = w.folder(imported()['id'])
        write_json_atomic(path / 'access.json', {'owner': 'alice'})
        w.update(path, status, request_parallel=False)
        legacy.append(path)
    save_job(legacy[1], 'render', 'interrupted')
    pending, owned = [], {}

    def tick(count=1):
        for _ in range(count):
            web.worker_tick(pending, owned)
            assert not owned, '稳态及注入不应启动进程'

    return SimpleNamespace(root=workspace, parents=parents, candidates=candidates,
                           deliveries=deliveries, legacy=legacy, tick=tick, save_job=save_job,
                           pending=pending)


def relative(path, finished):
    return path.relative_to(finished.root / '.web-state').as_posix()


def test_steady_five_ticks_do_not_change_business_json(finished):
    before = state_digest(finished.root)
    for _ in range(5):
        finished.tick()
        after = state_digest(finished.root)
        assert_state_unchanged(before, after)
        assert before.digest == after.digest


@pytest.mark.parametrize('injection', ['i3', 'i5', 'i7', 'i9'])
def test_injection_recovers_only_expected_files(finished, injection):
    # 预热无动作轮，后续优化会在这里建立缓存；注入必须使缓存失效。
    finished.tick(2)
    before = state_digest(finished.root)
    parent = finished.parents[0]
    candidate, event, failed_job = finished.candidates[0]
    media_file = parent / 'media-state.json'
    if injection == 'i3':
        child, delivery = finished.deliveries[0]
        file = child / 'publications.json'
        publications = read_json(file)
        publications[delivery['asset_id']].update(status='fbbm_ready', note='子进程晚回写')
        write_json_atomic(file, publications)
        changed = [file, media_file]
    elif injection == 'i5':
        path = finished.legacy[0]
        # 必须非纳管、无 queued/live 任务或活子进程，且 runtime() 结果仍为 ACTIVE。
        # 此旧项目没有 confirmed，request_parallel=False，不会被成片汇总重算为 review。
        # 大数据原型只改 status，可能被 live/queued 排除或被 production_summary 重算。
        assert not m.managed(path) and not m.internal(path)
        assert not batch_control.own_live_children(path)
        assert not any(j.get('batch_id') == path.name and j['status'] in {'queued', 'running'}
                       for j in web.stored_jobs())
        runtime = read_json(path / 'runtime.json')
        runtime['status'] = 'producing'
        write_json_atomic(path / 'runtime.json', runtime)
        assert w.runtime(path)['status'] in w.ACTIVE
        changed = [path / 'runtime.json', path / 'control.json']
    elif injection == 'i7':
        assert read_json(web.JOBS / (failed_job['id'] + '.json'))['status'] == 'failed'
        with m.locked(parent):
            state = m._read(parent)
            state['versions'][event['version_id']]['status'] = 'processing'
            state['revision'] += 1
            m._write(parent, state)
        changed = [media_file]
    else:
        file = web.JOBS / (failed_job['id'] + '.json')
        job = read_json(file)
        job.update(status='queued', not_before=web.time.time() + 10**7,
                   command=[sys.executable, '-c', 'pass'])
        write_json_atomic(file, job)
        changed = [file]
    injected = state_digest(finished.root)
    revision = m.read(parent)['revision']
    finished.tick()
    if injection == 'i3':
        actual = m.read(parent)['deliveries'][delivery['delivery_id']]
        assert actual['status'] == 'fbbm_ready'
        assert actual['publication'] == publications[delivery['asset_id']]
        assert m.read(parent)['revision'] == revision + 1
    elif injection == 'i5':
        runtime = read_json(path / 'runtime.json')
        assert runtime['status'] == 'interrupted'
        assert runtime['message'] == '执行已中断，可重试制作或继续上传'
        assert read_json(path / 'control.json')['suspended'] == []
        assert not read_json(path / 'control.json')['paused']
    elif injection == 'i7':
        version = m.read(parent)['versions'][event['version_id']]
        assert version['status'] == 'failed'
        assert version['error'] == '执行进程退出；原成片已保留，可重新确认制作'
        assert m.read(parent)['revision'] == revision + 1
    else:
        # 当前行为：保留未来重试时间，不分配新候选、不拉起进程、不改版本。
        assert read_json(file) == job
        assert any(j['id'] == job['id'] for j, _ in finished.pending)
        assert m.read(parent)['versions'][event['version_id']]['status'] == 'failed'
        assert_state_unchanged(injected, state_digest(finished.root))
    after = state_digest(finished.root)
    assert_state_unchanged(before, after, excluding=[relative(p, finished) for p in changed])
    finished.tick()
    assert_state_unchanged(after, state_digest(finished.root))


def test_completed_fetch_repairs_missing_render_once(finished):
    finished.tick(2)
    before = state_digest(finished.root)
    path = finished.legacy[0]
    # 暂停只阻止调度，保留真实 advance_stage 的补建及幂等逻辑。
    write_json_atomic(path / 'control.json', {'paused': True, 'children': [], 'suspended': []})
    fetch = finished.save_job(path, 'fetch', 'completed')
    successor_id = uuid.uuid5(uuid.NAMESPACE_URL, fetch['id'] + ':render').hex
    target = web.JOBS / (successor_id + '.json')
    assert not target.exists()
    finished.tick()
    successor = read_json(target)
    assert successor['parent_id'] == fetch['id']
    assert successor['batch_id'] == path.name
    assert successor['phase'] == successor['lane'] == 'render'
    assert successor['status'] == 'queued'
    assert successor['command'] == web.stage_command(successor, 'render')
    after = state_digest(finished.root)
    assert_state_unchanged(before, after, excluding=[relative(p, finished) for p in
                           [path / 'control.json', web.JOBS / (fetch['id'] + '.json'), target]])
    finished.tick()
    assert_state_unchanged(after, state_digest(finished.root))
    assert len([j for j in web.stored_jobs() if j.get('parent_id') == fetch['id']]) == 1


def test_sidebar_and_details_identical_for_all_accounts(finished):
    handler = web.Handler.__new__(web.Handler)
    accounts = [dict(username='admin', role='admin'), dict(username='alice', role='user'),
                dict(username='outsider', role='user')]
    state = state_digest(finished.root)
    before = api_snapshot(handler, accounts)
    visible = {p.name for p in finished.parents + finished.legacy}
    for username in ('admin', 'alice'):
        assert {b['id'] for b in before[username]['sidebar']['batches']} == visible
        assert all(before[username]['details'][key][0] == 200 for key in visible)
    assert before['outsider']['sidebar']['batches'] == []
    assert all(code == 404 for code, _ in before['outsider']['details'].values())
    for _ in range(5):
        finished.tick()
        assert_api_equal(before, api_snapshot(handler, accounts))
    assert_state_unchanged(state, state_digest(finished.root))
