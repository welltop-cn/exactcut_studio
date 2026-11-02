"""Compatibility for SAM's asynchronous CPU state offload on PyTorch MPS.

SAM runs in its own single-job process. Its non-blocking CPU/MPS state copies
can be consumed before completion on the verified MPS runtime. Keep the same
model, arithmetic and CPU offload policy, but complete these transfers before
returning to SAM. No process-wide Torch patch is installed in the web server.
"""
from contextlib import contextmanager


def fingerprint(cfg):
    return {'mps_transfers':'blocking_v1'} if cfg.get('device','mps')=='mps' else {}


@contextmanager
def blocking_transfers(torch):
    original=torch.Tensor.to
    def transfer(tensor,*args,**kwargs):
        destination=kwargs.get('device',args[0] if args else None)
        if kwargs.get('non_blocking') and (tensor.device.type=='mps' or str(destination).startswith('mps')):
            kwargs={**kwargs,'non_blocking':False}
        return original(tensor,*args,**kwargs)
    torch.Tensor.to=transfer
    try:yield
    finally:torch.Tensor.to=original
