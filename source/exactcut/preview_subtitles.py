"""Read-only, source-bound preview subtitle tasks; never change a media record.

Admission only enqueues a durable descriptor. The cross-process dispatcher in
`preview_subtitle_queue` executes up to the configured number of tasks concurrently.
"""
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import accounts, cutting, diagnostics, preview_subtitle_queue as queue
from . import preview_subtitle_model as model
from . import storage, workflow
from .gemini_credentials import read_key
from .media import ffprobe
from .util import file_sha256, json_sha256, load_config, now_iso, read_json, write_json_atomic


class SubtitleError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def settings():
    cfg = load_config().get('preview_subtitles', {})
    if not isinstance(cfg,dict):
        raise SubtitleError('预览字幕配置无效，请检查本机配置',503)
    if cfg.get('enabled') is True:
        limits = {'sample_fps':(1,5), 'proxy_long_edge':(720,1920), 'proxy_crf':(16,24),
                  'max_duration_seconds':(1,300), 'min_duration_seconds':(1,5), 'max_source_bytes':(1024,512*1024**2),
                  'max_inline_request_bytes':(100000,19_000_000), 'chunk_seconds':(1,60),
                  'request_timeout_ms':(1000,120000), 'prepare_timeout_seconds':(1,120),
                  'total_timeout_seconds':(120,1800), 'max_cues':(1,1000), 'max_active_tasks':(1,2),
                  'max_queued_tasks':(1,1000)}
        if (not isinstance(cfg.get('model'),str) or not cfg['model'].strip()
                or any(type(cfg.get(k)) is not int or not a <= cfg[k] <= b for k,(a,b) in limits.items())):
            raise SubtitleError('预览字幕配置无效，请检查本机配置',503)
    return cfg


def enabled():
    return settings().get('enabled') is True


def available():
    """A broken optional feature must not break login or the main workspace."""
    try:
        return enabled()
    except (ValueError,TypeError,OSError) as exc:
        diagnostics.event('preview_subtitles.configuration_invalid',level='warning',error_type=type(exc).__name__)
        return False


def lock_root():
    return workflow.BATCHES.parent/'preview-subtitle-locks'


def _lock(path, *, blocking=False):
    try:
        return cutting.lock_file(path, blocking=blocking)
    except cutting.CutError as exc:
        raise SubtitleError('其他素材正在生成中文字幕，请稍后点击重试',429) from exc


def binding_for(bid, rid, expected_sha, version_id):
    if (not isinstance(rid,str) or not re.fullmatch(r'[\w-]{1,80}',rid)
            or not isinstance(expected_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_sha)
            or (version_id is not None and (not isinstance(version_id,str) or not re.fullmatch(r'[0-9a-f]{32}',version_id)))):
        raise SubtitleError('字幕素材版本参数无效')
    try:
        source, record = workflow.preview_file(bid,rid,lightweight=False)
        batch = workflow.folder(bid)
        if not source.resolve().is_relative_to(batch.resolve()) or any(p.is_symlink() for p in (source,*source.parents) if p == batch or p.is_relative_to(batch)):
            raise ValueError('invalid path')
    except (ValueError,OSError,KeyError) as exc:
        raise SubtitleError('素材不存在或没有可预览的当前成片',404) from exc
    if record.get('output_sha256') != expected_sha or record.get('media_version_id') != version_id:
        raise SubtitleError('当前成片已变化，请刷新后重新开启字幕',409)
    return source, {'batch_id':bid,'request_id':rid,'source_sha256':expected_sha,'version_id':version_id}


def identity(binding, cfg):
    return json_sha256({'binding':binding, **model.fingerprint(cfg)})


def report_path(binding, cfg):
    return report_dir(binding['batch_id'], identity(binding,cfg))/'report.json'


def report_dir(bid, task_id):
    return workflow.folder(bid)/'preview-subtitles'/task_id


def _public(report):
    return {k:v for k,v in report.items() if not k.startswith('_')}


def _fail(report, message):
    report.update(status='failed',progress=message,error=message,cues=[])
    return report


def _recover(path, identifier):
    """Read a report, repairing a task that lost its queue entry or its worker."""
    if not path.exists():
        return {'id':identifier,'status':'idle','progress':'未生成 · 点击开启识别','cues':[]}
    report = read_json(path)
    status = report.get('status')
    if status == 'queued':
        # queue.json keeps pending and active in one atomic write, so "in neither" means
        # the task was dropped by a crash, not that a dequeue is in flight.
        known = queue.membership(identifier)
        if known['readable'] and not known['active'] and known['position'] is None:
            _fail(report,'字幕排队已中断，请点击重试')
            write_json_atomic(path,report)
    elif status == 'running':
        try:
            lease = _lock(lock_root()/(identifier+'.worker.lock'))
        except SubtitleError:
            return report
        lease.close()
        _fail(report,'上次字幕识别已中断，请点击重试')
        write_json_atomic(path,report)
    return report


def _queue_position(identifier):
    known = queue.membership(identifier)
    return known.get('position')


def state(bid,rid,sha,version_id):
    source, binding = binding_for(bid,rid,sha,version_id)
    cfg = settings(); identifier = identity(binding,cfg); path = report_path(binding,cfg)
    with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
        report = _recover(path,identifier)
        info = source.stat()
        actual_identity = [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]
        if report.get('_source_identity') and actual_identity != report['_source_identity'] and file_sha256(source) != sha:
            raise SubtitleError('正式成片内容已变化，请刷新后重试',409)
        result = _public(report)
        if report['status'] == 'queued':
            result['queue_position'] = _queue_position(identifier)
        return {'enabled':enabled(),'binding':binding,'report':result}


def _write_queued(path, identifier, binding, entry):
    with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
        previous = read_json(path) if path.exists() else {}
        if previous.get('status') == 'completed':
            return  # a cache hit must never be masked by a fresh queued state
        write_json_atomic(path, {'schema_version':1,'id':identifier,'status':'queued',
                                 'progress':'排队中','cues':[],'model':settings()['model'],
                                 'created_at':previous.get('created_at') or now_iso(),
                                 'enqueued_at':entry.get('enqueued_at'),'order':entry.get('order'),
                                 'binding':binding})


def start(bid,rid,sha,version_id,actor):
    if not enabled():
        raise SubtitleError('预览中文字幕未启用',404)
    if not isinstance(actor,str) or not re.fullmatch(r'[\w-]{1,64}',actor):
        raise SubtitleError('字幕请求缺少有效账号',401)
    source,binding = binding_for(bid,rid,sha,version_id)
    cfg = settings(); identifier = identity(binding,cfg); path = report_path(binding,cfg)
    with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
        previous = _recover(path,identifier)
        if previous['status'] == 'completed':
            if file_sha256(source) != sha:
                raise SubtitleError('正式成片内容已变化，请刷新后重试',409)
            return {'enabled':True,'binding':binding,'report':_public(previous)}
        if previous['status'] in {'running','queued'}:
            result = _public(previous)
            result['queue_position'] = _queue_position(identifier)
            return {'enabled':True,'binding':binding,'report':result}
    if source.stat().st_size > cfg['max_source_bytes']:
        raise SubtitleError('正式成片大小超出字幕识别上限')
    try:
        read_key(Path(os.environ.get('EXACTCUT_PREVIEW_SUBTITLE_ENV','')))
    except ValueError as exc:
        raise SubtitleError('字幕识别凭据不可用，请检查本机开发启动配置',503) from exc
    descriptor = {'task_id':identifier,'batch_id':bid,'request_id':rid,
                  'source_sha256':sha,'version_id':version_id,'actor':actor,
                  'settings_sha':model.SCHEMA_SHA}
    result = queue.enqueue(descriptor,cfg['max_queued_tasks'],
                           lambda entry:_write_queued(path,identifier,binding,entry))
    queue.wake()
    with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
        report = _recover(path,identifier)
        body = _public(report)
        if report['status'] == 'queued':
            body['queue_position'] = result.get('position') or _queue_position(identifier)
        return {'enabled':True,'binding':binding,'report':body}


def _actor_user(actor):
    """Recheck the initiating account; unavailable account data never grants access."""
    path = queue.accounts_path()
    if not path.is_file():
        return None
    try:
        return accounts.get(path,actor)
    except (OSError,ValueError,sqlite3.Error):
        return None


def _invalid(descriptor):
    """Return a safe reason when this task must not run, or None when it may."""
    bid,rid = descriptor['batch_id'],descriptor['request_id']
    user = _actor_user(descriptor.get('actor'))
    if not user or not user.get('enabled'):
        return '发起账号已停用，字幕任务未执行，请重新开启'
    try:
        folder = workflow.folder(bid)
    except (ValueError,KeyError):
        return '项目已删除，字幕任务未执行'
    if not (folder/'draft.json').exists() or workflow.material_versions.internal(folder):
        return '项目已删除，字幕任务未执行'
    if not accounts.allowed(folder,user):
        return '已无权访问该项目，字幕任务未执行'
    try:
        cfg = settings()
        source,binding = binding_for(bid,rid,descriptor['source_sha256'],descriptor['version_id'])
    except SubtitleError as exc:
        return str(exc)
    if identity(binding,cfg) != descriptor['task_id']:
        return '字幕识别配置已变化，请重新开启'
    if source.stat().st_size > cfg['max_source_bytes']:
        return '正式成片大小超出字幕识别上限'
    try:
        read_key(Path(os.environ.get('EXACTCUT_PREVIEW_SUBTITLE_ENV','')))
    except ValueError:
        return '字幕识别凭据不可用，请检查本机开发启动配置'
    return None


def fail_task(bid, task_id, message):
    """Record a final safe failure for an already created task directory."""
    if not isinstance(bid,str) or not re.fullmatch(r'[0-9a-f]{32}',bid):
        return False
    if not isinstance(task_id,str) or not re.fullmatch(r'[0-9a-f]{64}',task_id):
        return False
    path = report_dir(bid,task_id)/'report.json'
    if not path.parent.is_dir():
        return False
    with _lock(lock_root()/(task_id+'.state.lock'),blocking=True):
        if not path.exists():
            return False
        report = read_json(path)
        if report.get('status') in {'completed','failed'}:
            return False
        _fail(report,message)
        write_json_atomic(path,report)
        return True


def interrupt(bid, task_id):
    """A stranded task (no dispatcher, no worker) fails; it is never resent."""
    return fail_task(bid,task_id,'上次字幕识别已中断，请点击重试')


def execute(descriptor):
    """Run one claimed task. The dispatcher releases the active slot afterwards."""
    task_id = descriptor['task_id']
    if not isinstance(task_id,str) or not re.fullmatch(r'[0-9a-f]{64}',task_id):
        return
    path = report_dir(descriptor['batch_id'],task_id)/'report.json'
    bid,rid = descriptor['batch_id'],descriptor['request_id']
    sha,version = descriptor['source_sha256'],descriptor['version_id']
    identifier = task_id
    leases = []
    try:
        # Dispatcher slots bound global concurrency; this lease prevents duplicate
        # execution of one task without rejecting other admitted materials.
        leases.append(_lock(lock_root()/(identifier+'.worker.lock')))
        leases.append(cutting.lock_file(cutting.lock_root()/(bid+'.lock'),shared=True))
        # queue.json is committed before its report. Reconstruct a missing queued
        # report under the batch lease, without recreating a deleted project.
        workflow.folder(bid)
        binding = {'batch_id':bid,'request_id':rid,'source_sha256':sha,'version_id':version}
        with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
            if not path.exists():
                write_json_atomic(path, {'schema_version':1,'id':identifier,'status':'queued',
                                         'progress':'排队中','cues':[], 'binding':binding,
                                         'created_at':descriptor.get('enqueued_at') or now_iso()})
        # Recheck after admission, before the paid request leaves this process.
        reason = _invalid(descriptor)
        if reason:
            fail_task(bid,identifier,reason)
            return
        cfg = settings(); source,binding = binding_for(bid,rid,sha,version)
        key = read_key(Path(os.environ.get('EXACTCUT_PREVIEW_SUBTITLE_ENV','')))
        storage.require_space(source.parent,source.stat().st_size+128*1024**2)
        report = {'schema_version':1,'id':identifier,'status':'running','progress':'正在准备字幕识别',
                  'cues':[],'model':cfg['model'],'created_at':now_iso(),'started_at':time.time(),
                  'completed_chunks':0,'total_chunks':None,'binding':binding}
        with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
            if read_json(path).get('status') == 'completed':
                return  # cached result already final; never pay twice
            write_json_atomic(path,report)
        _work(source,binding,path,report,cfg,key)
    except SubtitleError as exc:
        fail_task(bid,identifier,str(exc))
    except (OSError,RuntimeError) as exc:
        diagnostics.event('preview_subtitles.admission_failed',level='warning',
                          batch_id=bid,error_type=type(exc).__name__)
        fail_task(bid,identifier,'字幕处理资源暂不可用，请稍后重试')
    finally:
        for lease in reversed(leases):
            lease.close()


def _work(source,binding,path,report,cfg,key):
    started = time.monotonic(); identifier = report['id']
    def update(**fields):
        with _lock(lock_root()/(identifier+'.state.lock'),blocking=True):
            if not path.parent.is_dir():
                raise SubtitleError('字幕任务所属项目已删除',404)
            latest = read_json(path); latest.update(fields); write_json_atomic(path,latest)
    try:
        # Prefer the existing lightweight video, but keep authority bound to the master.
        recognition_source, record = workflow.preview_file(binding['batch_id'],binding['request_id'],lightweight=True)
        batch = workflow.folder(binding['batch_id'])
        if (not recognition_source.resolve().is_relative_to(batch.resolve())
                or any(p.is_symlink() for p in (recognition_source,*recognition_source.parents)
                       if p == batch or p.is_relative_to(batch))):
            raise SubtitleError('预览视频路径校验失败',409)
        expected_sha = binding['source_sha256']
        source_kind = 'master'
        if recognition_source != source:
            preview = record.get('preview') or {}
            expected_sha = preview.get('sha256')
            if (not isinstance(expected_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_sha)
                    or preview.get('master_sha256') != binding['source_sha256']
                    or record.get('media_version_id') != binding['version_id']):
                raise SubtitleError('预览视频版本校验失败，请刷新后重试',409)
            source_kind = 'preview'
        # Copy the verified recognition input; never change or hardlink either video.
        with tempfile.TemporaryDirectory(prefix='source-',dir=path.parent) as temporary:
            snapshot = Path(temporary)/'source.mp4'
            shutil.copyfile(recognition_source,snapshot)
            if file_sha256(snapshot) != expected_sha:
                raise SubtitleError('识别视频校验失败，请刷新后重试',409)
            update(recognition_source=source_kind,recognition_bytes=snapshot.stat().st_size,
                   recognition_sha256=expected_sha)
            binding_for(binding['batch_id'],binding['request_id'],binding['source_sha256'],binding['version_id'])
            meta = ffprobe(snapshot)
            duration = float(meta['format']['duration'])
            if not math.isfinite(duration) or not 0 < duration <= cfg['max_duration_seconds']:
                raise SubtitleError(f'预览字幕支持最长 {cfg["max_duration_seconds"]} 秒以内的完整视频')
            if duration < cfg['min_duration_seconds']:
                raise SubtitleError(f'视频过短，本机字幕识别至少需要 {cfg["min_duration_seconds"]} 秒视频')
            duration_ms = math.ceil(duration*1000)
            update(duration_ms=duration_ms)
            cues = model.recognize(snapshot,duration_ms,cfg,key,update,Path(temporary),path.parent/'segments')
            current,_ = binding_for(binding['batch_id'],binding['request_id'],binding['source_sha256'],binding['version_id'])
            if file_sha256(current) != binding['source_sha256']:
                raise SubtitleError('当前成片内容已变化，旧字幕未接入',409)
            info = current.stat()
            update(status='completed',cues=cues,completed_at=now_iso(),elapsed_seconds=round(time.monotonic()-started,3),
                   progress='字幕已生成' if cues else '未识别到可见对白字幕',
                   _source_identity=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns])
    except Exception as exc:
        # Provider errors may contain signed URLs or credentials; never surface/log their text.
        diagnostics.event('preview_subtitles.failed',level='warning',batch_id=binding['batch_id'],error_type=type(exc).__name__)
        error = str(exc) if isinstance(exc,(SubtitleError,model.RecognitionError)) else '字幕识别未完成，请点击重试；完整视频结果未保存'
        if path.is_file():
            update(status='failed',error=error,progress=error,cues=[],elapsed_seconds=round(time.monotonic()-started,3))
