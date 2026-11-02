"""Independent, process-inherited interactive budgets; final compute stays separate."""
from . import cutting,diagnostics
from .util import load_config,json_sha256

_ROLES={'preview':('preview_tasks','preview_per_user','片段预览'),
        'scan':('scan_tasks','scan_per_user','识别'),
        'preprocess':('preprocess_tasks','preprocess_per_user','识别预处理')}


def settings():
    cfg=load_config().get('editor_resources',{})
    if not isinstance(cfg,dict):raise cutting.CutError('交互资源配置必须为对象',503)
    result={}
    for global_key,user_key,_ in _ROLES.values():
        for key,default in ((global_key,2),(user_key,1)):
            value=cfg.get(key,default)
            if type(value) is not int or not 1<=value<=32:raise cutting.CutError(f'交互资源配置 {key} 必须为1到32的整数',503)
            result[key]=value
        if result[user_key]>result[global_key]:raise cutting.CutError('交互资源配置的账号额度不能超过实例额度',503)
    threads=cfg.get('encoder_threads',2)
    if type(threads) is not int or not 1<=threads<=8:raise cutting.CutError('交互资源配置 encoder_threads 必须为1到8的整数',503)
    return {**result,'encoder_threads':threads}


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


def _slot(prefix,capacity):
    for index in range(capacity):
        try:return cutting.lock_file(cutting.lock_root()/'interactive'/f'{prefix}-{index}.lock')
        except cutting.CutError as exc:
            if exc.status!=429:raise
    return None


def acquire(kind,actor='admin'):
    """Refuse before admission when this actor or this instance exhausts a role."""
    if kind not in _ROLES:raise ValueError('unsupported interactive resource')
    if not isinstance(actor,str) or not actor or len(actor)>32:raise cutting.CutError('交互任务缺少有效的认证账号',503)
    global_key,user_key,label=_ROLES[kind];limits=settings();streams=[]
    try:
        owner=_slot(f'{kind}-user-{json_sha256(actor)}',limits[user_key])
        if owner is not None:
            streams.append(owner)
            global_slot=_slot(f'{kind}-global',limits[global_key])
            if global_slot is not None:
                streams.append(global_slot)
                return Lease(streams)
        scope='user' if owner is None else 'instance';limit=limits[user_key if owner is None else global_key]
        diagnostics.event('editor.capacity_busy',resource_kind=kind,scope=scope,capacity=limit)
        error=cutting.ResourceBusy('scan' if kind=='scan' else 'compute')
        error.args=(f"{'当前账号' if scope=='user' else '本实例'}的{label}并发容量已满（{limit}），请稍后重试；其他素材仍可编辑和提交最终队列。",)
        raise error
    except BaseException:
        for stream in reversed(streams):stream.close()
        raise
