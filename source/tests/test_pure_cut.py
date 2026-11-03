import shutil
from unittest.mock import patch

import pytest

from exactcut.models import BatchManifest, CutRequest, TimelineItem
from exactcut.orchestrator import execute_batch
from exactcut.media import run_checked
from exactcut.util import load_config, write_json_atomic, read_json

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires ffmpeg')
def test_exact_cut_without_indexes_or_model_keys(tmp_path, monkeypatch):
    for key in ('GEMINI_API_KEY', 'OPENAI_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    write_json_atomic(tmp_path / 'project.json', {'project_id': '101460'})
    source = tmp_path / 'source/videos/EP1.mp4'
    source.parent.mkdir(parents=True)
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'color=c=blue:s=108x192:r=30', '-f', 'lavfi', '-i',
                 'anullsrc=r=44100:cl=stereo', '-t', '2', '-c:v', 'libx264',
                 '-preset', 'ultrafast', '-c:a', 'aac', str(source)])
    timeline = [TimelineItem(type='episode', episode='EP1', start_ms=500, end_ms=1000),
                TimelineItem(type='title_card', text='Earlier', duration_ms=500),
                TimelineItem(type='episode', episode='EP1', start_ms=0, end_ms=500)]
    request = CutRequest(request_id='R1', seq=1, output_filename='cut.mp4', timeline=timeline)
    manifest = BatchManifest(drama_id='101460', drama_title='Test', business_date='20260924',
                             author='test', source_workbook='test.xlsx', requests=[request])
    config = load_config()
    config['render'].update(width=108, height=192, preset='ultrafast')
    stages = []
    result = execute_batch(tmp_path, manifest, config, progress=lambda stage, _: stages.append(stage))
    assert result['requests']['R1']['status'] == 'qc_passed'
    assert stages[0] == 'rendering'
    assert stages[-3:] == ['qc', 'preview', 'qc_passed']
    assert stages.count('base_cut') == 6  # Start and verified completion of each of three units.
    assert stages.index('concat') > max(i for i, stage in enumerate(stages) if stage == 'base_cut')
    assert 'storage_check' in stages and 'base_cache_check' in stages
    audit = tmp_path / 'outputs/20260924/audit/R1'
    assert read_json(audit / 'clip_plan.json')['timeline'] == [t.model_dump(mode='json') for t in timeline]
    assert not (audit / 'gemini_final.json').exists()
    assert not (tmp_path / 'data').exists()
    cached = execute_batch(tmp_path, manifest, config)
    assert cached['requests']['R1']['cache_reused']
    with patch('exactcut.orchestrator.inspect_output', return_value={'allowed': False, 'errors': ['bad duration']}), \
         patch('exactcut.orchestrator.publish_to_queue') as publish:
        rejected = execute_batch(tmp_path, manifest, config, force=True, publish=True)
    assert rejected['requests']['R1']['status'] == 'failed'
    publish.assert_not_called()
