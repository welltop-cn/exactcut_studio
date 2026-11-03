"""Independent durable queue for final cut/mosaic renders (schema version 1).

Queue admission is authoritative and contains the complete frozen job. Render JSON
is the execution/result authority. Admission precedes projection; recovery repairs
missing projections. Terminal report precedes slot release. Lock order: mutation
barrier -> asset gate -> queue lock -> render-state lock. Never acquire an asset
gate or wait for compute under the queue lock. Dispatcher lease covers all workers.
"""
import threading
from . import cutting, workflow, diagnostics
from .runtime import maintenance_file, mutation_lock
from .util import read_json, write_json_atomic, load_config


def queue_path():
    return workflow.BATCHES.parent/'mosaic-queue.json'


def settings():
    cfg=load_config().get('mosaic_queue',{})
    limit=cfg.get('max_active_tasks',1)
    if type(limit) is not int or not 1<=limit<=8:raise cutting.CutError('剪切打码并发配置无效',503)
    return {'max_active_tasks':limit}


def locked():
    return cutting.lock_file(queue_path().with_suffix('.lock'),blocking=True)


def load():
    if not queue_path().exists():return {'schema_version':1,'sequence':0,'last_actor':None,'tasks':[]}
    data=read_json(queue_path())
    if data.get('schema_version')!=1 or not isinstance(data.get('tasks'),list):raise cutting.CutError('剪切打码队列数据无效',503)
    return data


def job_path(item):
    return workflow.BATCHES/item['batch_id']/'mosaics'/item['session_id']/f"render-{item['id']}.json"


def report(item):
    path=job_path(item)
    return read_json(path) if path.exists() else item['job']


def blocked(bid,rid=None,allow_job=None):
    for item in load()['tasks']:
        if item['batch_id']!=bid or rid is not None and item['request_id']!=rid or item['id']==allow_job:continue
        job=report(item)
        if job['status'] in {'queued','running','cancelling'} or (job['status']=='completed' and not job.get('discarded_at') and job.get('replacement',{}).get('status')!='completed'):
            return item
    return None


def enqueue(bid,sid,rid,actor,job):
    with locked():
        data=load();data['sequence']+=1
        job={**job,'queue_order':data['sequence']}
        item={'id':job['id'],'batch_id':bid,'session_id':sid,'request_id':rid,'actor':actor,
              'order':data['sequence'],'state':'pending','job':job,'recoveries':0}
        data['tasks'].append(item);write_json_atomic(queue_path(),data)
        _project(item)
        return item


def _project(item):
    path=job_path(item)
    with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
        if not path.exists():write_json_atomic(path,item['job'])
        latest=path.parent/'render-latest.json'
        old_order=-1
        if latest.exists():
            previous=path.parent/f"render-{read_json(latest)['id']}.json"
            if previous.exists():old_order=read_json(previous).get('queue_order',-1)
        if old_order<=item['order']:write_json_atomic(latest,{'id':item['id']})


def claim(max_active=1):
    with locked():
        data=load();active=[i for i in data['tasks'] if i['state']=='active']
        if len(active)>=max_active:return None
        busy={i['actor'] for i in active}
        pending=[i for i in data['tasks'] if i['state']=='pending' and i['actor'] not in busy]
        for item in pending[:]:
            if report(item)['status'] in {'cancelled','failed','completed'}:
                item['state']='done';pending.remove(item)
        if not pending:
            write_json_atomic(queue_path(),data);return None
        # Keep each user's FIFO; rotate away from the last served user.
        turns=data.setdefault('actor_turns',{})
        item=min(pending,key=lambda i:(turns.get(i['actor'],0),i['order']))
        turns[item['actor']]=max(turns.values(),default=0)+1
        item['state']='active';data['last_actor']=item['actor']
        write_json_atomic(queue_path(),data)
        return item


def finish(jid,*,pending=False):
    with locked():
        data=load()
        for item in data['tasks']:
            if item['id']==jid:item['state']='pending' if pending else 'done'
        write_json_atomic(queue_path(),data)


def recover(skip_ids=()):
    """Only called under dispatcher lease; live legacy workers keep their lease."""
    with locked():
        data=load()
        for item in data['tasks']:
            if item['state']=='done' or item['id'] in skip_ids:continue
            path=job_path(item);_project(item)
            if item['state']!='active':continue
            try:lease=cutting.lock_file(path.with_suffix('.worker.lock'))
            except cutting.CutError:continue
            try:
                with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
                    job=read_json(path)
                    if job.get('cancel_requested'):
                        job.update(status='cancelled',progress='已停止生成，参数已保留');item['state']='done'
                    elif job['status'] in {'completed','failed','cancelled'}:item['state']='done'
                    else:
                        item['recoveries']+=1
                        if item['recoveries']>2:
                            job.update(status='failed',error='任务多次中断，请检查资源后重新提交');item['state']='done'
                        else:
                            job.update(status='queued',stage='queued',progress='服务恢复，等待重新处理');item['state']='pending'
                    write_json_atomic(path,job)
            finally:lease.close()
        write_json_atomic(queue_path(),data)


class Dispatcher:
    def __init__(self,poll_seconds=.25):
        self.stop_event=threading.Event();self.poll_seconds=poll_seconds;self._thread=None;self._lease=None

    def start(self):
        if self._thread and self._thread.is_alive():return False
        try:self._lease=cutting.lock_file(queue_path().with_name('mosaic-dispatcher.lock'))
        except cutting.CutError:return False
        try:
            recover()
            self.stop_event.clear()
            self._thread=threading.Thread(target=self._loop,name='mosaic-dispatcher',daemon=True)
            self._thread.start()
        except Exception:
            self._thread=None;self._lease.close();self._lease=None;raise
        return True

    def stop(self,timeout=10):
        self.stop_event.set()
        if self._thread:self._thread.join(timeout)

    def _loop(self):
        workers={}
        try:
            while not self.stop_event.is_set():
                workers={jid:t for jid,t in workers.items() if t.is_alive()}
                item=None
                try:
                    recover(skip_ids=workers)
                    with mutation_lock():
                        if not maintenance_file().exists() and not self.stop_event.is_set():item=claim(settings()['max_active_tasks'])
                    if item:
                        thread=threading.Thread(target=self._execute,args=(item,),daemon=True)
                        thread.start();workers[item['id']]=thread
                except Exception as exc:
                    diagnostics.event('mosaic.dispatch_failed',level='error',error_type=type(exc).__name__)
                self.stop_event.wait(self.poll_seconds)
        finally:
            for thread in workers.values():thread.join()
            self._lease.close();self._lease=None

    def _execute(self,item):
        from . import mosaic_render
        deferred=False
        try:deferred=mosaic_render.execute(item,self.stop_event)=='deferred'
        except BaseException as exc:
            path=job_path(item)
            with cutting.lock_file(path.parent/'render-state.lock',blocking=True):
                job=read_json(path)
                if job['status'] not in {'completed','failed','cancelled'}:
                    job.update(status='failed',error='后台处理未完成，请重新提交');write_json_atomic(path,job)
            diagnostics.event('mosaic.worker_failed',level='error',error_type=type(exc).__name__)
        finally:
            # A failed terminal write must stay recoverable rather than become an
            # unscheduled, permanently frozen task. Recovery retries it under lease.
            try:
                if deferred or report(item)['status'] in {'completed','failed','cancelled'}:
                    finish(item['id'],pending=deferred)
            except (OSError,ValueError) as exc:
                diagnostics.event('mosaic.slot_release_failed',level='error',error_type=type(exc).__name__)
