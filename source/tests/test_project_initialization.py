"""New managed dramas must work without legacy confirmation side effects."""
from concurrent.futures import ThreadPoolExecutor
import shutil

import pytest

from exactcut import material_workers, media, workflow as w
from exactcut.util import read_json, write_json_atomic
from test_material_versions import enrolled, token
from test_workflow import workspace

pytestmark = pytest.mark.integration


@pytest.fixture
def candidate(workspace, monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT', str(workspace))
    monkeypatch.setenv('EXACTCUT_SOURCE_ROOT', str(workspace / 'sources'))
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    m, parent, state = enrolled()
    state = m.confirm(parent, {'revision': state['revision'], 'mutation_id': token()})
    event = next(iter(state['outbox'].values()))
    child = material_workers.materialize(parent, event)
    project = workspace / 'projects' / event['manifest']['drama_id']
    # Draft timing inspection may create a cache directory, but no configuration.
    assert not (project / 'project.json').exists()
    if project.exists(): shutil.rmtree(project)
    # Replace only media probing; keep real source resolution and authorization checks.
    for request in event['manifest']['requests']:
        for item in request['timeline']:
            if item['type'] == 'episode':
                file = workspace / 'sources' / event['manifest']['drama_id'] / (item['episode'] + '.mp4')
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'fixture-media')
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda path: None)
    monkeypatch.setattr(media, 'ffprobe', lambda path: {'streams': [{'codec_type': 'video'}], 'format': {'duration': '600'}})
    return child, project, event


def test_managed_fetch_initializes_new_drama_before_real_source_validation(candidate):
    child, project, event = candidate
    assert not project.exists()
    assert w.fetch(child.name) == 0
    assert read_json(project / 'project.json')['project_id'] == event['manifest']['drama_id']
    assert w.runtime(child)['download_complete']


def test_managed_fetch_preserves_existing_authorization(candidate):
    child, project, event = candidate
    config = {'project_id': event['manifest']['drama_id'], 'title': 'existing', 'authorized_episodes': ['EP99']}
    write_json_atomic(project / 'project.json', config)
    original = (project / 'project.json').read_bytes()
    assert w.fetch(child.name) == 2
    assert '未授权' in w.runtime(child)['message']
    assert (project / 'project.json').read_bytes() == original


def test_project_initialization_concurrent_and_idempotent(candidate):
    child, project, event = candidate
    from exactcut.models import BatchManifest
    manifest = BatchManifest.model_validate(event['manifest'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: w.initialize_project(project, manifest), range(12)))
    config = read_json(project / 'project.json')
    config['excluded_episodes'] = ['EP2']
    write_json_atomic(project / 'project.json', config)
    w.initialize_project(project, manifest)
    assert read_json(project / 'project.json') == config
    assert all((project / name).is_dir() for name in ('requests', 'source/videos', 'data/episodes', 'outputs'))


def test_managed_fetch_rejects_symlink_project(candidate, tmp_path):
    child, project, event = candidate
    target = tmp_path / 'outside'; target.mkdir()
    project.parent.mkdir(parents=True, exist_ok=True)
    project.symlink_to(target, target_is_directory=True)
    assert w.fetch(child.name) == 2
    assert not (target / 'project.json').exists()
