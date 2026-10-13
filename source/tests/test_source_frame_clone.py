from copy import deepcopy
import pytest
from exactcut import workflow as w, source_frames, intake, material_versions
from exactcut.util import read_json, write_json_atomic
from test_source_frames import source, make_video
from test_web_base_path import app
from test_source_frame_timecode import frame_clip


def indexed_project(source, managed=False):
    _, item, _, video = source
    draft = deepcopy(item['draft'])
    draft.update(drama_id='101573', author='clone-test')
    draft['rows'][0]['clips'] = [frame_clip()]
    path = w.folder(item['id'])
    meta = source_frames.prepare(path, draft, 'EP1')
    saved = w.save_draft(item['id'], {'revision': item['revision'], 'draft': draft})
    if managed:
        material_versions.enroll(path)
    return path, meta, video


@pytest.mark.parametrize('managed', [False, True])
def test_clone_copies_current_index_and_remains_independent(source, managed):
    path, meta, _ = indexed_project(source, managed)
    (path/'source-picker'/meta['key']/'0.png').write_bytes(b'not-needed')
    result = w.clone_batch(path.name)
    copied = w.folder(result['id'])/'source-picker'/meta['key']/'index.json'
    assert copied.is_file()
    assert not result['errors'], result['errors']
    assert not copied.with_name('0.png').exists()
    original = path/'source-picker'/meta['key']/'index.json'
    assert original.read_bytes() == copied.read_bytes()
    assert original.stat().st_ino != copied.stat().st_ino
    original.unlink()
    assert not w.detail(result['id'])['errors']
    assert not w.clone_batch(result['id'])['errors']


def test_clone_rejects_replaced_source_index(source):
    path, meta, video = indexed_project(source)
    make_video(video, vfr=True)
    result = w.clone_batch(path.name)
    assert result['errors']
    assert not list((w.folder(result['id'])/'source-picker').glob('*/index.json'))


def test_clone_skips_symlink_index(source, tmp_path):
    path, meta, _ = indexed_project(source)
    index = path/'source-picker'/meta['key']/'index.json'
    moved = tmp_path/'outside.json';index.rename(moved);index.symlink_to(moved)
    result = w.clone_batch(path.name)
    assert result['errors']
    assert not list((w.folder(result['id'])/'source-picker').glob('*/index.json'))
