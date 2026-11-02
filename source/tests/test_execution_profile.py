"""Compare the candidate's real merged values; never read production secrets/runtime."""
import pytest
from pathlib import Path
from exactcut import execution_profile,concurrency
from exactcut.util import read_json,write_json_atomic


def test_candidate_dev_matches_same_version_production_template_values(tmp_path,monkeypatch):
    template=read_json(Path(concurrency.__file__).parents[1]/'config/production.json')
    sanitized={**template,'environment':'test'}
    production_fixture=tmp_path/'production-public-baseline.json';write_json_atomic(production_fixture,sanitized)
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test');monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(production_fixture));expected=execution_profile.snapshot()
    assert expected['ordinary']=={'fetch':2,'render':3,'upload':2,'poll':4,'max_active_children':11,'enabled':True}
    assert expected['subtitles']=={'max_active_tasks':2,'max_queued_tasks':30} and expected['mosaic_final']==1
    assert expected['ffmpeg_threads']==4 and expected['sam_threads']==4
    dev=tmp_path/'dev.json';write_json_atomic(dev,{'environment':'test'})
    monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(dev))
    assert execution_profile.require_equal(expected)==expected


@pytest.mark.parametrize('difference',[{'concurrency':{'render':1}},{'concurrency':{'max_active_children':1}},
    {'preview_subtitles':{'max_queued_tasks':200}},{'editor_resources':{'preview_tasks':1}},
    {'editor_resources':{'media_tasks':2}},{'editor_resources':{'encoder_threads':1}},{'media':{'encoder_threads':1}}])
def test_effective_business_override_fails_alignment_check(tmp_path,monkeypatch,difference):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test');monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path))
    path=tmp_path/'dev.json';write_json_atomic(path,{'environment':'test'});monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(path))
    expected=execution_profile.snapshot();write_json_atomic(path,{'environment':'test',**difference})
    with pytest.raises(ValueError,match='并发配置'):execution_profile.require_equal(expected)
