"""SAM stays on CPU even with a stale GPU configuration; no model is loaded."""
import sys
from types import ModuleType

import pytest
from PIL import Image
from exactcut import mosaic_sam2, mosaic_tracking


@pytest.mark.parametrize('configured', [None, 'mps', 'cuda', 'cpu'])
def test_tracking_settings_override_gpu_without_mutating_config(monkeypatch, configured):
    cfg = {'enabled': True, 'device': configured}
    monkeypatch.setattr(mosaic_tracking, 'load_config', lambda: {'cutting': {'tracking': cfg}})
    assert mosaic_tracking.settings()['device'] == 'cpu'
    assert cfg['device'] == configured


@pytest.mark.parametrize('configured', [None, 'mps', 'cuda'])
def test_worker_builds_cpu_predictor_for_old_requests(tmp_path, monkeypatch, configured):
    torch = ModuleType('torch')
    torch.set_num_threads = lambda count: None
    sam = ModuleType('sam2.build_sam')
    class ReachedPredictor(Exception):
        pass
    devices = []
    def build(*args, **kwargs):
        devices.append(kwargs['device'])
        raise ReachedPredictor
    sam.build_sam2_video_predictor = build
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'sam2', ModuleType('sam2'))
    monkeypatch.setitem(sys.modules, 'sam2.build_sam', sam)
    def decode(*args):
        Image.new('RGB', (16, 16)).save(tmp_path / 'frames/000000.jpg')
    monkeypatch.setattr(mosaic_sam2, 'run', decode)
    with pytest.raises(ReachedPredictor):
        mosaic_sam2._track({'output': str(tmp_path), 'source': 'synthetic.mp4',
                           'metadata': {'fps_num': 30, 'fps_den': 1},
                           'target': {'start': 0, 'end': 1},
                           'config': {'checkpoint': 'unused.pt', 'device': configured}})
    assert devices == ['cpu']
