"""MPS state copies are synchronous in an isolated predictor, with safe cache migration."""
from types import SimpleNamespace
import pytest
from exactcut import mosaic_tracking as tracking,mosaic_session as sessions,mosaic_render as render
from exactcut.util import json_sha256
from test_cutting_api import ready
from test_phase2_preview_api import fixture
from test_mosaic_inference_cache_concurrency import setup
from test_mosaic_render import make_track


def test_mps_transfer_scope_forces_copy_completion_and_restores_original_on_error():
    from exactcut.mosaic_mps import blocking_transfers
    calls=[]
    class Tensor:
        def __init__(self,device):self.device=SimpleNamespace(type=device)
        def to(self,*args,**kwargs):calls.append(kwargs.get('non_blocking'));return self
    torch=SimpleNamespace(Tensor=Tensor);original=Tensor.to
    with pytest.raises(RuntimeError):
        with blocking_transfers(torch):
            Tensor('mps').to('cpu',non_blocking=True)
            Tensor('cpu').to('mps',non_blocking=True)
            Tensor('cpu').to('cpu',non_blocking=True)
            raise RuntimeError('fixture')
    assert calls==[False,False,True]
    assert Tensor.to is original


@pytest.mark.integration
@pytest.mark.parametrize('device',['mps','cpu'])
def test_pre_fix_mps_cache_is_recomputed_but_cpu_legacy_cache_still_works(ready,tmp_path,monkeypatch,device):
    bid,session,target,cfg=setup(ready,tmp_path,monkeypatch);cfg={**cfg,'device':device};root=sessions.session_path(bid,session['id'])
    old_key=json_sha256({'source':session['source_sha256'],'reset':session.get('reset_at',''),'target':target,'config':cfg,'inference_engine':1})[:32]
    make_track(root/f'inference-{old_key}',target,session['metadata'])
    for key in tracking.legacy_inference_identities(session,target,cfg):make_track(root/f'inference-{key}',target,session['metadata'])
    calls=[]
    def infer(source,directory,meta,target,cfg):calls.append(directory);return make_track(directory,target,meta)['result']
    monkeypatch.setattr(tracking,'run_tracker',infer)
    track=render.prepare_track(bid,session['id'],session,target,cfg,root/'new-target')
    assert len(calls)==(1 if device=='mps' else 0)
    assert track['result']['frames']
