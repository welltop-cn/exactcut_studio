import pytest
from exactcut import source_cache, source_prefetch, stage_results, webserver as web, workflow as w
from exactcut.util import read_json
from test_source_prefetch import app, console, prefetch, draft_with, jobs


def test_prefetch_busy_episode_preserves_deferred_result_and_exit75(console, monkeypatch):
    assert prefetch(console, draft_with('EP1'))[0] == 200
    job = jobs()[0]
    def busy(*a, **kw): raise stage_results.ResourceDeferred(1234567890, '正片锁忙')
    monkeypatch.setattr(source_cache, 'ensure', busy)
    with pytest.raises(stage_results.ResourceDeferred): source_prefetch.run(job['batch_id'], job['id'])
    saved = read_json(source_prefetch.result_path(w.folder(job['batch_id']), job['id']))
    assert saved['status'] == 'waiting_resource' and saved['not_before'] == 1234567890
    code = stage_results.run_child(source_prefetch.run, job['batch_id'], job['id'])
    assert code == 75
