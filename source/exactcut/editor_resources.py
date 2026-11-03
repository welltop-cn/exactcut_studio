"""Shared execution leases for interactive workers and actual media stages.

Submission never calls acquire: the persistent dispatcher claims execution slots.
There is no per-account admission or execution quota; fairness belongs to its queue.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import os
from functools import wraps
from . import cutting,processing_cancel
from .runtime import data_root
from .util import load_config

_ROLES={'preview':('preview_tasks',2),'scan':('scan_tasks',2),'preprocess':('preprocess_tasks',2),
        'media':('media_tasks',5),'final_media':('final_media_tasks',1),'frame':('frame_tasks',2),'transfer':('transfer_tasks',2)}
_active=ContextVar('editor_execution_stages',default=frozenset())
_leases=ContextVar('editor_execution_leases',default={})
_reserved=ContextVar('editor_initial_resource',default=None)
_final=ContextVar('editor_final_group',default=False)
_production=False


def settings():
    cfg=load_config().get('editor_resources',{})
    if not isinstance(cfg,dict):raise cutting.CutError('交互资源配置必须为对象',503)
    result={}
    for key,default in _ROLES.values():
        value=cfg.get(key,default)
        if type(value) is not int or not 1<=value<=32:raise cutting.CutError(f'执行资源配置 {key} 必须为1到32的整数',503)
        result[key]=value
    threads=cfg.get('encoder_threads',2)
    if type(threads) is not int or not 1<=threads<=8:raise cutting.CutError('交互资源配置 encoder_threads 必须为1到8的整数',503)
    return {**result,'encoder_threads':threads}


def production_process():
    """Production children (workflow, material_download, source_prefetch) never take shared editor slots."""
    global _production;_production=True


def production():return _production


def encoding():
    from .media import encoder_scope
    return encoder_scope(settings()['encoder_threads'])


class Lease:
    def __init__(self,streams):self.streams=tuple(streams)
    def filenos(self):return tuple(stream.fileno() for stream in self.streams if not stream.closed)
    def close(self):
        for stream in reversed(self.streams):stream.close()
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()


def acquire(kind,actor='admin'):
    """Nonblocking dispatcher probe; never a public submission admission check."""
    if kind not in _ROLES:raise ValueError('unsupported execution resource')
    if not isinstance(actor,str) or not actor or len(actor)>32:raise cutting.CutError('交互任务缺少有效的认证账号',503)
    key,_=_ROLES[kind];capacity=settings()[key]
    for index in range(capacity):
        try:return Lease([cutting.lock_file(data_root()/'.web-state/execution-locks'/f'{kind}-{index}.lock')])
        except cutting.CutError as exc:
            if exc.status!=429:raise
    raise cutting.ResourceBusy('frame' if kind=='frame' else 'compute')


def wait(kind,actor='admin',*,timeout=1800):
    """Only bounded existing workers may wait during a stage transition."""
    deadline=time.monotonic()+timeout
    while True:
        processing_cancel.check()
        try:return acquire(kind,actor)
        except cutting.ResourceBusy:
            if time.monotonic()>=deadline:raise cutting.CutError('等待处理资源超时，请重试',503)
            time.sleep(.05)


@contextmanager
def stage(kind):
    held=_active.get()
    if _production or kind in held:
        yield;return
    pending=_reserved.get()
    if pending and pending[0]==kind:
        lease=pending[1];_reserved.set(None)
    else:lease=wait(kind)
    token=_active.set(held|{kind});lease_token=_leases.set({**_leases.get(),kind:lease})
    try:
        with processing_cancel.retain([lease]):yield
    finally:_leases.reset(lease_token);_active.reset(token);lease.close()


def media_stage():
    """Nested media inside a short frame operation keeps the same single lease."""
    return stage('frame' if 'frame' in _active.get() else 'final_media' if _final.get() else 'media')


def media_operation(function):
    @wraps(function)
    def guarded(*args,**kwargs):
        with media_stage():return function(*args,**kwargs)
    return guarded


@contextmanager
def reservation(value):
    token=_reserved.set(value)
    try:yield
    finally:
        remaining=_reserved.get()
        if remaining:remaining[1].close()
        _reserved.reset(token)


def release_reservation():
    value=_reserved.get()
    if value:value[1].close();_reserved.set(None)


@contextmanager
def final_group():
    token=_final.set(True)
    try:yield
    finally:_final.reset(token)


def export_media():
    lease=_leases.get().get('media') or _leases.get().get('final_media')
    if lease is None:return []
    return [{'fd':fd,'device':os.fstat(fd).st_dev,'inode':os.fstat(fd).st_ino} for fd in lease.filenos()]


class InheritedLease:
    """Validate live inherited media FDs again at spawn, preventing FD reuse."""
    def __init__(self,bindings):self.bindings=bindings
    def filenos(self):
        result=[]
        for binding in self.bindings:
            try:stat=os.fstat(binding['fd'])
            except OSError:continue
            if (stat.st_dev,stat.st_ino)!=(binding['device'],binding['inode']):raise ValueError('继承的媒体资源句柄已变化')
            result.append(binding['fd'])
        return tuple(result)


@contextmanager
def inherited_media(bindings):
    if not bindings:
        with stage('media'):yield
        return
    # This is an internal SAM request capability, never a client API field.
    allowed={(path.stat().st_dev,path.stat().st_ino) for pattern in ('media-*.lock','final_media-*.lock')
             for path in (data_root()/'.web-state/execution-locks').glob(pattern)}
    if any((row.get('device'),row.get('inode')) not in allowed for row in bindings):raise ValueError('无效的继承媒体资源')
    lease=InheritedLease(bindings)
    if len(lease.filenos())!=len(bindings):raise ValueError('继承媒体资源已关闭')
    token=_active.set(_active.get()|{'media'});lease_token=_leases.set({**_leases.get(),'media':lease})
    try:
        with processing_cancel.retain([lease]):yield
    finally:_leases.reset(lease_token);_active.reset(token)
