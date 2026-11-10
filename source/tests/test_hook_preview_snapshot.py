"""Real preview reports bind only when a new execution is confirmed."""
import copy
import pytest

from exactcut import hook_preview, interactive_queue, material_versions as m, workflow as w, renderer
from exactcut.models import BatchManifest
from exactcut.util import read_json, load_config, write_json_atomic, file_sha256, json_sha256
from test_hook_preview_source_identity import hook_input, finish
from test_phase2_preview_api import fixture
from test_cutting import make_video
from test_material_versions import token
from exactcut.runtime import mutation_lock
from pathlib import Path

pytestmark = pytest.mark.integration


def confirm(batch, managed):
    path = w.folder(batch['id'])
    if managed:
        state = m.read(path)
        state = m.confirm(path, {'revision': state['revision'], 'mutation_id': token()})
        rid = state['draft']['rows'][0]['request_id']
        return state['versions'][state['assets'][rid]['candidate_version']]['manifest']
    w.start_batch(batch['id'], batch['revision'])
    return read_json(path / 'confirmed.json')


@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'managed'])
def test_confirm_freezes_server_preview_and_renderer_reuses_it(hook_input, monkeypatch, managed):
    batch, request, project = hook_input
    video = project / 'source/videos/EP1.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    make_video(video)
    if managed:
        m.enroll(w.folder(batch['id']))
        batch = w.detail(batch['id'])
    dispatcher = interactive_queue.Dispatcher(poll_seconds=.01)
    assert dispatcher.start()
    try:
        job = finish(batch, request, dispatcher)
        before = copy.deepcopy(w.detail(batch['id']))
        manifest = BatchManifest.model_validate(confirm(batch, managed))
        frozen = manifest.requests[0]
        assert frozen.hook_options.preview_key == job['result']['preview_key']
        assert w.detail(batch['id'])['draft'] == before['draft']
        if managed:
            assert m.read(w.folder(batch['id']))['assets'][request.request_id]['desired_generation'] == 1
        monkeypatch.setattr(renderer, 'approved_emphasis', lambda *a: pytest.fail('preview analysis must be reused'))
        monkeypatch.setattr(renderer, 'presentation', lambda *a: pytest.fail('preview placement must be reused'))
        config = load_config()
        config['render'].update(width=180, height=320, preset='ultrafast')
        timeline = [i.model_dump(mode='json') for i in frozen.timeline]
        result = renderer.render_request(project, frozen, timeline, project/'confirmed.mp4', config)
        assert result['hook_visual']['preview_approved']
        digest = file_sha256(project/'confirmed.mp4')
        # Retry/reconfirm without reports must preserve an already frozen manifest.
        # This is also the compatibility path for pre-fix confirmed manifests.
        for report in (w.folder(batch['id'])/'hook-previews').glob('*.json'):
            report.unlink()
        if not managed:
            w.update(w.folder(batch['id']), 'failed', 'fixture retry')
        assert confirm(w.detail(batch['id']), managed) == manifest.model_dump(mode='json')
        assert file_sha256(project/'confirmed.mp4') == digest
    finally:
        dispatcher.stop(timeout=None)


@pytest.mark.parametrize('invalid', ['input', 'config', 'source', 'shot_index', 'digest', 'request', 'batch', 'missing'])
@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'managed'])
def test_confirm_rejects_invalid_or_foreign_preview(hook_input, monkeypatch, invalid, managed):
    batch, request, project = hook_input
    video = project/'source/videos/EP1.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    make_video(video)
    if managed:
        m.enroll(w.folder(batch['id']))
        batch = w.detail(batch['id'])
    dispatcher = interactive_queue.Dispatcher(poll_seconds=.01)
    assert dispatcher.start()
    try:
        job = finish(batch, request, dispatcher)
        path = w.folder(batch['id'])
        report = path/'hook-previews'/(job['id']+'.json')
        key = job['result']['preview_key']
        if invalid == 'input':
            draft = copy.deepcopy(batch['draft'])
            draft['rows'][0]['hook_text'] += ' edited'
            if managed:
                m.save_draft(path, {'revision': batch['revision'], 'mutation_id': token(), 'draft': draft})
            else:
                batch = w.save_draft(batch['id'], {'revision': batch['revision'], 'draft': draft})
        elif invalid == 'config':
            config = load_config(); config['render']['width'] += 2
            monkeypatch.setattr(hook_preview, 'load_config', lambda: config)
        elif invalid == 'source':
            replacement = video.with_name('replacement.mp4')
            replacement.write_bytes(video.read_bytes())
            replacement.replace(video)
        elif invalid == 'shot_index':
            write_json_atomic(project/'data/episodes/EP1.video.json', {'shots': [], 'revision': 'changed'})
        elif invalid == 'digest':
            cached = project/'cache/hook-previews'/(key+'.json')
            doc = read_json(cached); doc['decision']['position'] = 'tampered'
            write_json_atomic(cached, doc)
        elif invalid == 'request':
            job['request_id'] = 'Rother'; write_json_atomic(report, job)
        elif invalid == 'batch':
            other = w.create_blank()
            target = w.folder(other['id'])/'hook-previews'/report.name
            target.parent.mkdir(parents=True); report.replace(target)
        else:
            # A valid shared cache pointer supplied by a client is insufficient
            # without this batch's own completed report.
            draft = copy.deepcopy(batch['draft'])
            draft['rows'][0]['hook_options']['preview_key'] = key
            if managed:
                m.save_draft(path, {'revision': batch['revision'], 'mutation_id': token(), 'draft': draft})
            else:
                batch = w.save_draft(batch['id'], {'revision': batch['revision'], 'draft': draft})
            report.unlink()
        frozen = BatchManifest.model_validate(confirm(batch, managed)).requests[0]
        assert frozen.hook_options.preview_key is None
        if invalid == 'missing':
            config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast')
            timeline = [i.model_dump(mode='json') for i in frozen.timeline]
            result = renderer.render_request(project, frozen, timeline, project/'fallback.mp4', config)
            assert not result['hook_visual'].get('preview_approved')
    finally:
        dispatcher.stop(timeout=None)


@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'managed'])
def test_late_preview_cannot_rewrite_confirmed_execution(hook_input, monkeypatch, managed):
    import threading
    import time
    batch, request, project = hook_input
    video = project/'source/videos/EP1.mp4'
    video.parent.mkdir(parents=True, exist_ok=True); make_video(video)
    path = w.folder(batch['id'])
    if managed:
        m.enroll(path); batch = w.detail(batch['id'])
    computed, release = threading.Event(), threading.Event()
    real_compute = hook_preview.compute
    def paused(*args):
        result = real_compute(*args)
        computed.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(hook_preview, 'compute', paused)
    dispatcher = interactive_queue.Dispatcher(poll_seconds=.01); assert dispatcher.start()
    try:
        job = hook_preview.submit(batch['id'], batch, 0, request, 'admin')
        assert computed.wait(5)
        manifest = confirm(batch, managed)
        assert manifest['requests'][0]['hook_options']['preview_key'] is None
        authority = path/('media-state.json' if managed else 'confirmed.json')
        before = authority.read_bytes()
        draft = copy.deepcopy(w.detail(batch['id'])['draft'])
        frozen = BatchManifest.model_validate(manifest).requests[0]
        config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast')
        renderer.render_request(project, frozen, [i.model_dump(mode='json') for i in frozen.timeline], project/'frozen.mp4', config)
        digest = file_sha256(project/'frozen.mp4')
        release.set(); deadline = time.monotonic()+5
        while job['status'] in {'queued', 'running'}:
            assert time.monotonic() < deadline
            time.sleep(.02); job = hook_preview.state(batch['id'], job['id'])
        assert job['status'] == ('completed' if managed else 'superseded')
        assert authority.read_bytes() == before
        assert w.detail(batch['id'])['draft'] == draft
        assert file_sha256(project/'frozen.mp4') == digest
    finally:
        release.set(); dispatcher.stop(timeout=None)


@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'managed'])
def test_confirm_does_not_read_video_bytes_and_repeat_skips_preview_work(hook_input, monkeypatch, managed):
    import fcntl
    batch, request, project = hook_input
    video = project/'source/videos/EP1.mp4'
    video.parent.mkdir(parents=True, exist_ok=True); make_video(video)
    path = w.folder(batch['id'])
    if managed:
        m.enroll(path); batch = w.detail(batch['id'])
    dispatcher = interactive_queue.Dispatcher(poll_seconds=.01); assert dispatcher.start()
    try:
        job = finish(batch, request, dispatcher)
    finally:
        dispatcher.stop(timeout=None)
    original_open = Path.open
    def guarded_open(file, *args, **kwargs):
        if file == video:
            held = []
            for lock in (w.ROOT/'.web-state/mutation.lock', w.ROOT/'.web-state/naming.lock'):
                with original_open(lock, 'a') as stream:
                    try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError: held.append(lock.name)
            pytest.fail(f'confirm read video bytes while locks held: {held}')
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded_open)
    with mutation_lock():
        if managed:
            with w.naming_lock(): manifest = confirm(batch, managed)
        else:
            manifest = confirm(batch, managed)
    assert manifest['requests'][0]['hook_options']['preview_key'] == job['result']['preview_key']
    def no_repeat_preview_reads(file, *args, **kwargs):
        if file.parent in (path/'hook-previews', project/'cache/hook-previews'):
            pytest.fail('reconfirm reread optional preview data for an existing version')
        return guarded_open(file, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_repeat_preview_reads)
    if not managed: w.update(path, 'failed', 'fixture retry')
    with mutation_lock():
        if managed:
            with w.naming_lock(): repeated = confirm(w.detail(batch['id']), managed)
        else:
            repeated = confirm(w.detail(batch['id']), managed)
    assert repeated == manifest


@pytest.mark.parametrize('broken', ['report-list', 'report-null', 'result-list', 'result-string',
                                   'completed-at', 'cache-list', 'cache-null', 'identity-list', 'decision-list'])
@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'managed'])
def test_malformed_optional_preview_falls_back_without_blocking_confirm(hook_input, broken, managed):
    batch, request, project = hook_input
    video = project/'source/videos/EP1.mp4'
    video.parent.mkdir(parents=True, exist_ok=True); make_video(video)
    path = w.folder(batch['id'])
    if managed:
        m.enroll(path); batch = w.detail(batch['id'])
    dispatcher = interactive_queue.Dispatcher(poll_seconds=.01); assert dispatcher.start()
    try:
        job = finish(batch, request, dispatcher)
    finally:
        dispatcher.stop(timeout=None)
    report = path/'hook-previews'/(job['id']+'.json')
    key = job['result']['preview_key']
    cache = project/'cache/hook-previews'/(key+'.json')
    if broken == 'report-list': write_json_atomic(report, [])
    elif broken == 'report-null': write_json_atomic(report, None)
    elif broken.startswith('result-'):
        job['result'] = [] if broken == 'result-list' else 'broken'
        write_json_atomic(report, job)
    elif broken == 'completed-at':
        job['completed_at'] = []
        write_json_atomic(report, job)
        second = {**job, 'completed_at': '2026-01-01'}
        write_json_atomic(report.with_name('f'*32+'.json'), second)
    elif broken == 'cache-list': write_json_atomic(cache, [])
    elif broken == 'cache-null': write_json_atomic(cache, None)
    else:
        doc = read_json(cache)
        doc['identity' if broken == 'identity-list' else 'decision'] = []
        # Keep the digest valid: structure is independently invalid.
        key = json_sha256(doc)
        write_json_atomic(cache.with_name(key+'.json'), doc)
        job['result']['preview_key'] = key; write_json_atomic(report, job)
    manifest = confirm(batch, managed)
    if broken != 'completed-at':
        assert manifest['requests'][0]['hook_options']['preview_key'] is None
    else:
        assert manifest['requests'][0]['hook_options']['preview_key'] == key


@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'managed'])
def test_renderer_rechecks_source_content_after_confirmation(hook_input, managed):
    batch, request, project = hook_input
    video = project/'source/videos/EP1.mp4'
    video.parent.mkdir(parents=True, exist_ok=True); make_video(video)
    path = w.folder(batch['id'])
    if managed:
        m.enroll(path); batch = w.detail(batch['id'])
    dispatcher = interactive_queue.Dispatcher(poll_seconds=.01); assert dispatcher.start()
    try:
        job = finish(batch, request, dispatcher)
    finally:
        dispatcher.stop(timeout=None)
    manifest = confirm(batch, managed)
    frozen = BatchManifest.model_validate(manifest).requests[0]
    assert frozen.hook_options.preview_key == job['result']['preview_key']
    before = file_sha256(video)
    replacement = video.with_name('replacement.mp4')
    make_video(replacement, frames=91); replacement.replace(video)
    assert file_sha256(video) != before
    config = load_config(); config['render'].update(width=180, height=320, preset='ultrafast')
    result = renderer.render_request(project, frozen, [i.model_dump(mode='json') for i in frozen.timeline], project/'changed-source.mp4', config)
    assert not result['hook_visual'].get('preview_approved')
    # The immutable pointer stays frozen; it is the renderer that rejects reuse.
    assert frozen.hook_options.preview_key == job['result']['preview_key']
