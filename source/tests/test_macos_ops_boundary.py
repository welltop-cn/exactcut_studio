import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('script', ['source/deploy/ops.py', 'source/deploy/tunnel_recovery.py',
                                    'operations/controller/ops.py', 'operations/controller/tunnel_recovery.py'])
def test_legacy_macos_entrypoints_refuse_linux_before_touching_state(script, monkeypatch, tmp_path, capsys):
    path = Path(__file__).resolve().parents[2] / script
    spec = importlib.util.spec_from_file_location('macos_boundary', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(sys, 'argv', [str(path), 'status'])
    monkeypatch.setattr(module, 'OPS', tmp_path / 'must-not-be-created')
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert 'macOS' in capsys.readouterr().err
    assert not module.OPS.exists()
