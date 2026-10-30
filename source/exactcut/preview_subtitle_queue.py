"""Durable bounded FIFO for preview subtitle recognition.

`queue.json` under the data root's `.web-state` is the scheduling authority.
Version-1 `active` keeps its original primary descriptor; optional `active_extra`
holds additional workers. Existing one-slot state remains readable.
Each task's `report.json` is the result authority. Queue writes precede report
creation; reports become final before slots are released. A single dispatcher
lease covers every live worker, including during shutdown. Recovery fails
stranded active tasks without resending paid requests.
"""
import fcntl
import threading
import time
from contextlib import contextmanager

from . import diagnostics, workflow
from .util import now_iso, read_json, write_json_atomic

SCHEMA_VERSION = 1
FIELDS = ('task_id', 'batch_id', 'request_id', 'source_sha256', 'version_id',
          'actor', 'settings_sha')


class QueueError(ValueError):
    """Application-authored, safe-to-display queue failures."""

    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def state_dir():
    return workflow.BATCHES.parent


def queue_path():
    return state_dir() / 'queue.json'


def wake_path():
    return state_dir() / 'queue.wake'


def accounts_path():
    return state_dir() / 'accounts.sqlite3'


@contextmanager
def _queue_lock(blocking=True):
    path = state_dir() / 'queue.lock'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = path.open('a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError as exc:
        stream.close()
        raise QueueError('字幕队列正在处理其他请求，请稍后重试', 429) from exc
    try:
        yield
    finally:
        stream.close()


def _load():
    try:
        data = read_json(queue_path())
    except FileNotFoundError:
        return {'schema_version': SCHEMA_VERSION, 'sequence': 0, 'active': None, 'pending': []}
    if (not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION
            or not isinstance(data.get('pending'), list)
            or (data.get('active') is not None and not isinstance(data['active'], dict))
            or not isinstance(data.get('sequence'), int)):
        raise QueueError('字幕排队数据无效，请检查本机数据目录', 503)
    if (not isinstance(data.get('active_extra',[]),list)
            or not all(isinstance(item, dict) and item.get('task_id')
                       for item in [*data['pending'],*data.get('active_extra',[])])):
        raise QueueError('字幕排队数据无效，请检查本机数据目录', 503)
    return data


def _active_items(data):
    return ([data['active']] if data.get('active') else []) + data.get('active_extra',[])


def _remove_active(data, task_id):
    remaining = [item for item in _active_items(data) if item['task_id'] != task_id]
    data['active'] = remaining[0] if remaining else None
    data['active_extra'] = remaining[1:]


def _save(data):
    write_json_atomic(queue_path(), data)


def snapshot():
    """Read-only view for dialogs and status reads; never mutates or enqueues."""
    try:
        data = _load()
    except (OSError, ValueError):
        # Cannot judge: report the task as still queued instead of failing it wrongly.
        return {'readable': False, 'active': None, 'active_tasks': [], 'pending': []}
    return {'readable': True, 'active': data['active'], 'active_tasks': _active_items(data), 'pending': data['pending']}


def membership(task_id):
    """Position of a task without taking a lock; 0 means active, 1..n means queued."""
    view = snapshot()
    pending = view['pending']
    item = next((item for item in view['active_tasks'] if item.get('task_id') == task_id),None)
    if item is not None:
        return {'readable': True, 'active': True, 'position': 0, 'batch_id': item.get('batch_id')}
    for index, entry in enumerate(pending):
        if entry.get('task_id') == task_id:
            return {'readable': True, 'active': False, 'position': index + 1,
                    'batch_id': entry.get('batch_id')}
    return {'readable': view['readable'], 'active': False, 'position': None}


def wake():
    """Ask the dispatcher to look again soon. Best effort; never a control path."""
    _wake_event.set()
    try:
        wake_path().write_text(f'{time.time()}\n', encoding='utf-8')
    except OSError:
        pass


def enqueue(descriptor, max_queued, on_enqueued=None):
    """Admit one task. `on_enqueued` writes the queued report inside the lock.

    Lock order is queue.lock -> report state lock. Callers must not hold a report
    state lock while calling this.
    """
    with _queue_lock():
        data = _load()
        if any(item.get('task_id') == descriptor['task_id'] for item in _active_items(data)):
            return {'status': 'active', 'position': 0}
        for index, item in enumerate(data['pending']):
            if item.get('task_id') == descriptor['task_id']:
                return {'status': 'queued', 'position': index + 1}
        if len(data['pending']) >= max_queued:
            raise QueueError(f'字幕生成排队已满（{max_queued} 条），请等待现有任务完成后再开启', 429)
        data['sequence'] += 1
        entry = {**{key: descriptor[key] for key in FIELDS},
                 'order': data['sequence'], 'enqueued_at': now_iso()}
        data['pending'].append(entry)
        _save(data)
        if on_enqueued:
            on_enqueued(entry)
        return {'status': 'queued', 'position': len(data['pending'])}


def claim(max_active=1):
    """Move the oldest pending task into an available durable slot, atomically."""
    if type(max_active) is not int or not 1 <= max_active <= 2:
        raise QueueError('字幕并发配置无效',503)
    with _queue_lock():
        data = _load()
        if len(_active_items(data)) >= max_active or not data['pending']:
            return None
        item = data['pending'].pop(0)
        if data['active'] is None:
            data['active'] = item
        else:
            data.setdefault('active_extra',[]).append(item)
        _save(data)
        return item


def finish(task_id):
    """Release the active slot after its report is final."""
    with _queue_lock():
        data = _load()
        if not any(item.get('task_id') == task_id for item in _active_items(data)):
            return False
        _remove_active(data,task_id)
        _save(data)
        return True


def _interrupt(item):
    """Fail a stranded task; never resend a paid request."""
    from . import preview_subtitles
    preview_subtitles.interrupt(item.get('batch_id'), item.get('task_id'))


def recover():
    """Fail stranded active tasks under the dispatcher lease, saving each removal."""
    with _queue_lock():
        data = _load()
        items = _active_items(data)
        for item in items:
            _interrupt(item)
            _remove_active(data,item['task_id'])
            _save(data)
        return items[0] if len(items) == 1 else items or None


def _enabled():
    from . import preview_subtitles
    return preview_subtitles.available()


class Dispatcher:
    """Cross-process single-owner loop supervising up to two recognition workers."""

    def __init__(self, run=None, poll_seconds=0.5):
        self.stop_event = threading.Event()
        self.poll_seconds = poll_seconds
        self._run = run
        self._lease = None
        self._thread = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        """Become the dispatching process. Returns False when another one owns it."""
        if self.running:
            return False
        path = state_dir() / 'dispatcher.lock'
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stream = path.open('a')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return False
        self._lease = stream
        try:
            recover()
        except (OSError, ValueError, RuntimeError) as exc:
            diagnostics.event('preview_subtitles.recovery_failed', level='warning',
                              error_type=type(exc).__name__)
            self._lease.close()
            self._lease = None
            return False
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name='preview-subtitle-dispatcher',
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=10):
        """Request shutdown; an in-flight call retains its lease until it exits."""
        self.stop_event.set()
        _wake_event.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self):
        try:
            self._dispatch()
        finally:
            # join(timeout) cannot cancel a paid provider call. Releasing this in
            # stop() would let another owner recover and overwrite its live task.
            if self._lease:
                self._lease.close()
                self._lease = None

    def _dispatch(self):
        workers = []
        try:
            while not self.stop_event.is_set():
                _wake_event.clear()
                workers = [thread for thread in workers if thread.is_alive()]
                item = None
                try:
                    if _enabled():
                        from . import preview_subtitles
                        limit = preview_subtitles.settings()['max_active_tasks']
                        if len(workers) < limit:
                            item = claim(limit)
                except (OSError, ValueError, RuntimeError) as exc:
                    diagnostics.event('preview_subtitles.queue_unavailable', level='warning',
                                      error_type=type(exc).__name__)
                if item is None:
                    _wake_event.wait(self.poll_seconds)
                    continue
                if self.stop_event.is_set():
                    _requeue(item)
                    break
                thread = threading.Thread(target=self._execute,args=(item,),
                                          name='preview-subtitle-worker',daemon=True)
                try:
                    thread.start()
                except RuntimeError:
                    _requeue(item)
                    raise
                workers.append(thread)
        finally:
            # The dispatcher lease covers ALL workers; timeout cannot release it early.
            for thread in workers:
                thread.join()

    def _execute(self,item):
        try:
            if self.stop_event.is_set():
                _requeue(item)
                return
            (self._run or _default_run)(item, self.stop_event)
        except BaseException as exc:  # A worker crash must never wedge the queue.
            diagnostics.event('preview_subtitles.worker_crashed', level='error',
                              error_type=type(exc).__name__)
            try:
                from . import preview_subtitles
                preview_subtitles.fail_task(item.get('batch_id'), item.get('task_id'),
                                            '字幕识别未完成，请点击重试')
            except (OSError, ValueError, RuntimeError):
                pass
        finally:
            try:
                finish(item['task_id'])
            except (OSError, ValueError, RuntimeError) as exc:
                diagnostics.event('preview_subtitles.slot_release_failed', level='error',
                                  error_type=type(exc).__name__)
            _wake_event.set()


def _requeue(item):
    with _queue_lock():
        data = _load()
        if any(entry.get('task_id') == item['task_id'] for entry in _active_items(data)):
            _remove_active(data,item['task_id'])
            data['pending'].insert(0, item)
            _save(data)


def _default_run(item, stop_event):
    from . import preview_subtitles
    preview_subtitles.execute(item)


_wake_event = threading.Event()
