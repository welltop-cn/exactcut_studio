"""Per-key raw SAM cache: immutable completed directories, private production work."""
import shutil
import time
import uuid
from contextlib import contextmanager
from PIL import Image
from . import cutting,mosaic_tracking as tracking,processing_cancel
from .util import read_json,write_json_atomic


def _verified(directory,target):
    if not (directory/'result.json').is_file():return None
    try:
        result=read_json(directory/'result.json')
        tracking.checked_frames(target,result.get('frames'),result['width'],result['height'])
        for index in range(target['end']-target['start']):
            file=directory/'masks'/f'{index:06d}.png'
            if not file.is_file():return None
            with Image.open(file) as image:
                if image.size!=(result['width'],result['height']):return None
                image.verify()
        return result
    except (ValueError,KeyError,TypeError,cutting.CutError,FileNotFoundError):return None
    except OSError as exc:
        if exc.errno is None:return None  # Unreadable image data; actual filesystem errors fail.
        raise


@contextmanager
def _key_lease(root,key,validate):
    deadline=time.monotonic()+910
    while True:
        processing_cancel.check();validate()
        try:
            lease=cutting.lock_file(root/f'inference-{key}.lock');break
        except cutting.CutError as exc:
            if exc.status!=429:raise
            if time.monotonic()>=deadline:raise cutting.CutError('等待同一区域推理超时，请稍后重试',503) from exc
            time.sleep(.05)
    try:
        with processing_cancel.retain([lease]):yield
    finally:lease.close()


def _legacy(root,session,target,cfg,directory):
    geometry={k:target[k] for k in ('start','end','seed_frame','x','y','w','h')}
    for key in tracking.legacy_inference_identities(session,geometry,cfg):yield root/f'inference-{key}'
    jid=tracking.identity(session,target,cfg);report=root/f'track-{jid}.json'
    if report.is_file() and read_json(report).get('status')=='completed':yield root/f'track-{jid}'
    # This directory is bound to the immutable owning job's target index.
    yield directory
    for job_path in root.glob('render-*.json'):
        previous=read_json(job_path)
        if previous.get('source_sha256')!=session['source_sha256'] or previous.get('reset_at','')!=session.get('reset_at',''):continue
        for request_path in (root/job_path.stem).glob('target-*/request.json'):
            request=read_json(request_path)
            if request.get('config')!=cfg or request.get('metadata')!=session['metadata']:continue
            if request.get('source')!=str(root/'source.mp4'):continue
            if {k:request.get('target',{}).get(k) for k in geometry}==geometry:yield request_path.parent


def prepare(root,session,target,cfg,directory,*,on_tracking=None,validate=lambda:None):
    geometry={k:target[k] for k in ('start','end','seed_frame','x','y','w','h')}
    key=tracking.inference_identity(session,geometry,cfg);canonical=root/f'inference-{key}'
    manifest={'schema_version':1,'identity':key,'source_sha256':session['source_sha256'],'reset_at':session.get('reset_at',''),
              'metadata':session['metadata'],'target':geometry,'config':cfg}
    def track(path,result):return {'target':target,'directory':path,'result':result}
    with _key_lease(root,key,validate):
        result=_verified(canonical,target)
        if result is not None:return track(canonical,result)
        # The key lease proves older work has no live writer, including inherited children.
        for stale in root.glob(f'inference-{key}-work-*'):
            saved=stale/'cache-manifest.json'
            result=_verified(stale,target) if saved.is_file() and read_json(saved)==manifest else None
            if result is not None:
                validate();processing_cancel.check()
                if canonical.exists():shutil.rmtree(canonical)
                stale.replace(canonical)
                return track(canonical,result)
            shutil.rmtree(stale)
        for legacy in _legacy(root,session,target,cfg,directory):
            result=_verified(legacy,target)
            if result is not None:return track(legacy,result)
        work=root/f'inference-{key}-work-{uuid.uuid4().hex}';work.mkdir()
        trash=None
        try:
            write_json_atomic(work/'cache-manifest.json',manifest)
            if on_tracking:on_tracking(work.name)
            tracking.run_tracker(root/'source.mp4',work,session['metadata'],geometry,{**cfg,'masks_only':True})
            processing_cancel.check();validate()
            result=_verified(work,target)
            if result is None:raise cutting.CutError('SAM推理缓存不完整，未生成处理结果',503)
            if canonical.exists():
                trash=root/f'.inference-{key}-invalid-{uuid.uuid4().hex}'
                canonical.replace(trash)
            work.replace(canonical)
            return track(canonical,result)
        finally:
            if work.exists():shutil.rmtree(work)
            if trash and trash.exists():shutil.rmtree(trash)
