"""Real preview reports bind only when a new execution is confirmed."""
import copy
import pytest

from exactcut import hook_preview, interactive_queue, material_versions as m, workflow as w, renderer
from exactcut.models import BatchManifest
from exactcut.util import read_json, load_config, write_json_atomic, file_sha256
from test_hook_preview_source_identity import hook_input, finish
from test_phase2_preview_api import fixture
from test_cutting import make_video
from test_material_versions import token

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
