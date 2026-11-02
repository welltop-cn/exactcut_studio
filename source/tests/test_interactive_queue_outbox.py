"""A committed local descriptor must remain queryable if report publication failed."""
from pathlib import Path
import pytest
from exactcut import interactive_queue as queue
from exactcut.util import read_json
from test_interactive_queue import state,submit


def missing_report(state,monkeypatch):
    path=state/'session-1'/('render-'+f'{1:032x}'+'.json');path.parent.mkdir()
    original=queue.write_json_atomic
    def write(target,value):
        if Path(target)==path:raise OSError('injected report publication failure')
        return original(target,value)
    with monkeypatch.context() as patch:
        patch.setattr(queue,'write_json_atomic',write)
        with pytest.raises(OSError,match='injected'):submit(state,1)
    entry=queue.snapshot()['pending'][0]
    assert not path.exists()
    return entry,path


def test_duplicate_retry_reconstructs_missing_report_without_duplicate_queue_entry(state,monkeypatch):
    entry,path=missing_report(state,monkeypatch)
    retry=submit(state,1)
    assert retry['task_id']==entry['task_id']
    assert path.is_file() and read_json(path)['status']=='queued'
    assert len(queue.snapshot()['pending'])==1


def test_new_waiting_preview_can_supersede_outbox_with_missing_report(state,monkeypatch):
    old,path=missing_report(state,monkeypatch)
    job={**old['job'],'id':f'{2:032x}'};new_path=path.parent/('render-'+job['id']+'.json')
    queue.enqueue('clip',old['session'],new_path,job,{},state_lock=path.parent/'render-state.lock')
    assert read_json(path)['status']=='superseded'
    assert read_json(new_path)['status']=='queued'
    assert [row['job']['id'] for row in queue.snapshot()['pending']]==[job['id']]
