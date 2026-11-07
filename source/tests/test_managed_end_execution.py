"""A managed batch must finish a job without the legacy runtime writer raising."""
import pytest
from exactcut import material_versions as mV
from exactcut import workflow as w
from test_material_versions import enrolled
from test_workflow import workspace  # noqa: F401  (isolates Excel/AI import)


def test_end_execution_on_managed_batch_does_not_write_legacy_runtime(workspace):
    m, path, state = enrolled(media=True)
    rid = state['draft']['rows'][0]['request_id']
    before = (path / 'runtime.json').read_text()
    # Managed completion is written by material_workers; the legacy writers must not run.
    w.end_execution(path, rid)
    w.end_execution(path)
    assert (path / 'runtime.json').read_text() == before
    assert not (path / 'production' / f'{rid}.json').exists()
    assert m.read(path)['revision'] == state['revision']


def test_end_execution_on_legacy_batch_still_updates_runtime(workspace):
    from test_workflow import imported
    item = imported()
    path = w.folder(item['id'])
    w.end_execution(path)
    runtime = (path / 'runtime.json').read_text()
    assert '"executor_running":false' in runtime.replace(' ', '')
