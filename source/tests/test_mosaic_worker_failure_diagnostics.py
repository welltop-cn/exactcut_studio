"""A failed temporary cache must leave a bounded, redacted model diagnostic."""
import json
from pathlib import Path
import subprocess
import pytest
from exactcut import diagnostics,mosaic_tracking as tracking,processing_cancel,storage,cutting

pytestmark = pytest.mark.integration


def test_worker_failure_has_diagnostic_even_when_temporary_directory_is_removed(tmp_path,monkeypatch):
    diagnostics.configure(tmp_path)
    directory=tmp_path/'inference-work';source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    monkeypatch.setattr(storage,'require_space',lambda *a:None)
    def fail(command,**kwargs):
        kwargs['stdout'].write(b'RuntimeError: GPU command failed; api_key=private-fixture-value\n')
        return subprocess.CompletedProcess(command,1)
    monkeypatch.setattr(processing_cancel,'run',fail)
    try:
        with pytest.raises(cutting.CutError):tracking.run_tracker(source,directory,{'width':160,'height':96},{'start':0,'end':6},{})
        import shutil
        shutil.rmtree(directory)
        rows=[json.loads(line) for path in (tmp_path/'logs').glob('*.jsonl') for line in path.read_text().splitlines()]
        row=next(row for row in rows if row['event']=='mosaic.tracker_process_failed')
        assert 'GPU command failed' in json.dumps(row)
        assert 'private-fixture-value' not in json.dumps(row)
        assert row['return_code']==1
    finally:diagnostics.disable()
