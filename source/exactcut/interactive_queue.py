"""Persistent waiting for existing interactive workers, separate from execution leases."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import threading
import time
from .stage_results import ResourceDeferred
from pathlib import Path
from . import cutting,cutting_access,editor_resources,processing_cancel,diagnostics
from .runtime import data_root,mutation_lock,maintenance_file
from .util import read_json,write_json_atomic,json_sha256,now_iso

ROLES={'mosaic_scan':'scan','cut_scan':'scan','locate':'scan','track':'preview','clip':'preview','hook':'preview',
       'mosaic_prepare':'preprocess','cut_prepare':'preprocess','preview_oss':'preprocess','mosaic_frame':'frame','cut_frame':'frame'}
TERMINAL={'completed','failed','cancelled','superseded'}


def directory():return data_root()/'.web-state/interactive-queue'


@contextmanager
def locked():
    root=directory();root.mkdir(parents=True,exist_ok=True)
    with (root/'queue.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX);yield


def load():
    path=directory()/'queue.json'
    if not path.exists():return {'schema_version':1,'sequence':0,'pending':[],'active':[],'cursor':{}}
    value=read_json(path)
    if value.get('schema_version')!=1 or not all(isinstance(value.get(k),list) for k in ('pending','active')):
        raise cutting.CutError('交互等待队列无法读取，请保留现场联系管理员',503)
    return value


def save(value):write_json_atomic(directory()/'queue.json',value)


def snapshot():
    with locked():return load()


def relative(path):
    path=Path(path).resolve();root=data_root()
    if not path.is_relative_to(root):raise ValueError('交互任务路径超出当前数据目录')
    return str(path.relative_to(root))


def report(entry):return data_root()/entry['report']


def enqueue(kind,session,path,job,cfg,*,state_lock,payload=None):
    """Server adapters hold state_lock before calling; paths/runners are not client input."""
    if kind not in ROLES:raise ValueError('unsupported interactive task')
    actor=job.get('actor','admin')
    if not isinstance(actor,str) or not actor or len(actor)>32:raise cutting.CutError('交互任务缺少有效账号',503)
    task_id=json_sha256([kind,session['batch_id'],session['id'],job['id']])
    entry={'task_id':task_id,'kind':kind,'role':ROLES[kind],'actor':actor,'session':session,
           'report':relative(path),'state_lock':relative(state_lock),'job':{**job,'status':'queued'},
           'diagnostics':{key:value for key,value in diagnostics.current().items() if key in {'trace_id','client_request_id','request_id','batch_id','session_id','job_id'}},
           'config':cfg,'payload':payload or {},'permit':None if kind in {'hook','mosaic_prepare','cut_prepare','preview_oss'} else asdict(cutting_access.InteractiveReadPermit.issue(session,{**job,'status':'queued'}))}
    with locked():
        state=load()
        for previous in list(state['pending']+state['active']):
            if previous['task_id']==task_id:
                old=read_json(report(previous)) if report(previous).is_file() else previous['job']
                if old.get('status') not in TERMINAL:
                    if not report(previous).is_file():
                        restored={**previous['job'],'status':'running' if previous in state['active'] else 'queued'}
                        write_json_atomic(report(previous),restored)
                    return previous
                if previous in state['pending']:state['pending'].remove(previous)
        # Replace only an obsolete waiting preview of this same purpose/material.
        if kind in {'clip','track','hook','mosaic_frame','cut_frame'}:
            keep=[]
            for previous in state['pending']:
                if (previous['kind']==kind and previous['session']['batch_id']==session['batch_id']
                        and previous['session']['id']==session['id']
                        and (kind not in {'mosaic_frame','cut_frame'} or previous['actor']==actor)):
                    old=read_json(report(previous)) if report(previous).is_file() else dict(previous['job'])
                    old.update(status='superseded',progress='设置已更新，等待中的旧预览已替换');write_json_atomic(report(previous),old)
                else:keep.append(previous)
            state['pending']=keep
        state['sequence']+=1;entry.update(order=state['sequence'],enqueued_at=now_iso())
        state['pending'].append(entry);save(state)
        # The descriptor doubles as a small local outbox if this publication is interrupted.
        write_json_atomic(path,entry['job'])
    return entry


def _update(entry,**fields):
    with cutting.lock_file(data_root()/entry['state_lock'],blocking=True):
        path=report(entry);job=read_json(path) if path.is_file() else dict(entry['job'])
        if job['id']!=entry['job']['id']:return job
        if fields.get('status')=='running' and (job.get('status') in TERMINAL or job.get('cancel_requested')):return job
        job.update(fields);write_json_atomic(path,job);return job


def _run(entry,leases):
    kind=entry['kind'];session=entry['session'];bid=session['batch_id'];sid=session['id'];path=report(entry)
    cfg=entry['config'];job=read_json(path)
    if kind in {'mosaic_frame','cut_frame'}:
        from . import editor_frames
        return editor_frames.run(entry,leases)
    if kind in {'mosaic_prepare','cut_prepare'}:
        from . import editor_preparation
        return editor_preparation.run(entry,leases)
    if kind=='preview_oss':
        from . import preview_oss
        return preview_oss.run_queued(entry,leases)
    if kind=='hook':
        from . import hook_preview
        return hook_preview.run_queued(entry,leases)
    permit=cutting_access.InteractiveReadPermit(**entry['permit'])
    permit.require(session,job)
    from . import cutting_scan,mosaic_tracking
    expected=cutting_scan.settings() if kind in {'mosaic_scan','cut_scan','locate'} else mosaic_tracking.settings()
    if cfg!=expected:
        _update(entry,status='superseded',progress='处理配置已变化，请重新提交');return
    if kind in {'mosaic_scan','cut_scan'}:
        from . import cutting_scan
        key=cutting_scan.credential()
        if kind=='cut_scan':return cutting_scan._work(path,session,job,cfg,key,leases,permit)
        from . import mosaic_scan
        return mosaic_scan._work(bid,sid,path,job,cfg,key,leases,permit)
    if kind=='locate':
        from . import mosaic_locate,cutting_scan
        return mosaic_locate._work(bid,sid,path,job,cfg,cutting_scan.credential(),leases,permit)
    if kind=='track':
        from . import mosaic_tracking
        return mosaic_tracking._work(bid,sid,path,job,cfg,leases,permit)
    if kind=='clip':
        from . import mosaic_render
        return mosaic_render._work(bid,sid,path,job,cfg,leases,permit)


def initial_resource(entry):
    if entry['kind'] in {'mosaic_frame','cut_frame'}:return None
    if entry['kind'] in {'mosaic_prepare','cut_prepare','preview_oss'}:return None
    if entry['kind']=='hook':return None  # Starts with cache/semantic work; media is a later bounded stage.
    if entry['kind']=='locate':
        if (report(entry).parent/'frames'/(str(entry['job']['seed_frame'])+'.jpg')).is_file():return None
        return 'frame'
    return 'media'


class Dispatcher:
    """One coordinator; only admitted work gets a thread, never waiting submissions."""
    def __init__(self,run=None,poll_seconds=.1):
        self.run=run or _run;self.poll_seconds=poll_seconds;self.stop_event=threading.Event();self._lease=None;self._thread=None;self.workers={};self._launch_lock=threading.Lock()
    @property
    def active_workers(self):return sum(thread.is_alive() for thread in list(self.workers.values()))
    def start(self):
        if self._thread and self._thread.is_alive():return False
        directory().mkdir(parents=True,exist_ok=True);stream=(directory()/'dispatcher.lock').open('a')
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:stream.close();return False
        self._lease=stream;self.stop_event.clear()
        try:
            editor_resources.settings();self.recover()
            self._thread=threading.Thread(target=self.loop,daemon=True);self._thread.start();return True
        except BaseException:
            stream.close();self._lease=None;raise
    def stop(self,timeout=None):
        with self._launch_lock:self.stop_event.set()
        if self._thread:self._thread.join(timeout)
        # Only the coordinator's finally releases its lease, after ALL workers exit.
    def recover(self):
        interrupted=[]
        with locked():
            state=load();live=[]
            for entry in state['active']:
                try:lease=cutting.lock_file(report(entry).with_suffix('.worker.lock'))
                except cutting.CutError:live.append(entry);continue
                lease.close();job=read_json(report(entry)) if report(entry).is_file() else entry['job']
                if job.get('cancel_requested'):interrupted.append((entry,'cancelled'))
                elif job.get('status') in TERMINAL:continue
                elif entry['kind'] in {'clip','track','mosaic_prepare','cut_prepare','preview_oss','mosaic_frame','cut_frame'} or job.get('status')=='queued':state['pending'].append(entry);interrupted.append((entry,'queued'))
                else:interrupted.append((entry,'failed'))
            state['active']=live;state['pending'].sort(key=lambda row:row['order']);save(state)
        for entry,status in interrupted:
            _update(entry,status=status,progress='服务恢复后等待处理' if status=='queued' else '上次处理已中断，未重放可能发出的云端请求')
    def finish_cancellations(self):
        # HTTP order is report state -> queue; never invert it in the dispatcher.
        for entry in snapshot()['pending']:
            path=report(entry);job=read_json(path) if path.is_file() else entry['job']
            if not job.get('cancel_requested') or job.get('status') in TERMINAL:continue
            try:
                with cutting.lock_file(data_root()/entry['state_lock']):
                    current=read_json(path) if path.is_file() else dict(entry['job'])
                    if current.get('id')==entry['job']['id'] and current.get('cancel_requested') and current.get('status') not in TERMINAL:
                        current.update(status='cancelled',progress='任务已取消');write_json_atomic(path,current)
            except cutting.CutError as exc:
                if exc.status!=429:raise

    def claim(self):
        with locked():
            state=load();pending=state['pending'];ordered=[]
            roles=['scan','preview','preprocess','frame']
            if state.get('last_role') in roles:
                index=roles.index(state['last_role'])+1;roles=roles[index:]+roles[:index]
            for role in roles:
                group=[row for row in pending if row['role']==role];owners=list(dict.fromkeys(row['actor'] for row in group));cursor=state['cursor'].get(role)
                if cursor in owners:
                    index=owners.index(cursor)+1;owners=owners[index:]+owners[:index]
                while group:
                    for owner in owners:
                        item=next((row for row in group if row['actor']==owner),None)
                        if item:ordered.append(item);group.remove(item)
            for entry in ordered:
                path=report(entry)
                if not path.parent.is_dir():
                    pending.remove(entry);continue  # Never resurrect a deleted project/session.
                job=read_json(path) if path.is_file() else entry['job']
                if entry.get('not_before',0)>time.time():continue
                if job.get('status') in TERMINAL:
                    pending.remove(entry);continue
                if job.get('cancel_requested'):continue
                leases=[];initial=None
                try:
                    leases.append(editor_resources.acquire(entry['role'],entry['actor']))
                    if entry['role']=='frame':initial=('frame',leases[0])
                    resource=initial_resource(entry)
                    if resource:
                        lease=editor_resources.acquire(resource,entry['actor']);leases.append(lease);initial=(resource,lease)
                    leases.append(cutting.lock_file(path.with_suffix('.worker.lock')))
                    leases.append(cutting.lock_file(cutting.lock_root()/(entry['session']['batch_id']+'.lock'),shared=True))
                    if entry['kind'] in {'mosaic_scan','cut_scan'}:leases.append(editor_resources.acquire('preprocess',entry['actor']))
                except cutting.CutError as exc:
                    for lease in reversed(leases):lease.close()
                    if exc.status!=429:raise
                    continue
                if self.stop_event.is_set() or maintenance_file().exists():
                    for lease in reversed(leases):lease.close()
                    save(state);return None
                pending.remove(entry);state['active'].append(entry);state['cursor'][entry['role']]=entry['actor'];state['last_role']=entry['role'];save(state)
                return entry,leases,initial
            save(state)
        return None
    def execute(self,entry,leases,initial=None):
        deferred=None
        try:
            job=_update(entry,status='running',progress='正在处理',started_at=now_iso())
            if job.get('status') in TERMINAL:return
            if not job.get('cancel_requested'):
                with diagnostics.context(**entry.get('diagnostics',{})),editor_resources.reservation(initial),processing_cancel.scope(lambda:read_json(report(entry)).get('cancel_requested',False),leases=leases):self.run(entry,leases)
            else:_update(entry,status='cancelled',progress='任务已取消')
        except processing_cancel.Cancelled:_update(entry,status='cancelled',progress='任务已取消')
        except ResourceDeferred as exc:
            deferred=exc.not_before;_update(entry,status='queued',progress='等待所需资源，稍后继续')
        except Exception as exc:
            _update(entry,status='failed',error=str(exc) if isinstance(exc,cutting.CutError) else '处理未完成，请重试；原素材保留')
        finally:
            for lease in reversed(leases):lease.close()
            with locked():
                state=load();state['active']=[row for row in state['active'] if row['task_id']!=entry['task_id']]
                if deferred is not None:
                    job=read_json(report(entry))
                    if job.get('status')=='queued' and not job.get('cancel_requested'):
                        entry={**entry,'not_before':deferred};state['pending'].append(entry)
                save(state)
    def loop(self):
        recovered_at=time.monotonic()
        try:
            while not self.stop_event.is_set():
                self.workers={key:thread for key,thread in self.workers.items() if thread.is_alive()}
                claimed=None
                try:
                    if time.monotonic()-recovered_at>5:
                        self.recover();recovered_at=time.monotonic()
                    # Same short maintenance/admission order as the existing queues.
                    with mutation_lock():
                        if not self.stop_event.is_set() and not maintenance_file().exists():
                            self.finish_cancellations();claimed=self.claim()
                        if claimed:
                            entry,leases,initial=claimed
                            with self._launch_lock:
                                if self.stop_event.is_set() or maintenance_file().exists():
                                    for lease in reversed(leases):lease.close()
                                    with locked():
                                        state=load();state['active']=[row for row in state['active'] if row['task_id']!=entry['task_id']]
                                        state['pending'].append(entry);state['pending'].sort(key=lambda row:row['order']);save(state)
                                    claimed=None
                                else:
                                    thread=threading.Thread(target=self.execute,args=(entry,leases,initial),daemon=True)
                                    self.workers[entry['task_id']]=thread
                                    try:thread.start()
                                    except BaseException:
                                        for lease in reversed(leases):lease.close()
                                        self.workers.pop(entry['task_id'],None)
                                        _update(entry,status='failed',error='无法启动处理，请重试；任务输入已保留')
                                        with locked():
                                            state=load();state['active']=[row for row in state['active'] if row['task_id']!=entry['task_id']];save(state)
                except (OSError,ValueError,RuntimeError):claimed=None
                if claimed is None:self.stop_event.wait(self.poll_seconds)
        finally:
            # Finite stop(timeout) returns while draining, but cannot release ownership.
            for thread in list(self.workers.values()):thread.join()
            if self._lease:self._lease.close();self._lease=None
