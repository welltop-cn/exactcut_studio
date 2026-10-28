"""Independent immutable source snapshots for region recognition."""
import re
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path
from . import cutting, cutting_scan, cutting_access, workflow, storage
from .cutting import CutError, lock_file, lock_root, run, frame_lease
from .util import file_sha256, json_sha256, read_json, write_json_atomic, now_iso


def session_path(bid,sid):
    if not isinstance(sid,str) or not re.fullmatch('[0-9a-f]{32}',sid):raise CutError('打码会话不存在',404)
    return workflow.folder(bid)/'mosaics'/sid


def read_session(bid,sid):
    path=session_path(bid,sid)/'session.json'
    if not path.is_file():raise CutError('打码会话不存在',404)
    data=read_json(path)
    if data['batch_id']!=bid:raise CutError('打码会话不存在',404)
    cutting.require_request(bid,data['request_id'])
    return data


def get_session(bid,sid):
    from . import mosaic_tracking
    return {**read_session(bid,sid),'capabilities':{'region_recognition':cutting_scan.enabled(),'tracking':mosaic_tracking.enabled(),'render_stop':True}}


def open_session(bid,rid,sha):
    if not isinstance(rid,str) or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):raise CutError('成片版本参数无效')
    with cutting_access.gate(workflow.folder(bid),rid):
        cutting_access.require_editable(workflow.folder(bid),rid,source_sha=sha)
        sid=json_sha256([bid,rid,sha])[:32];path=session_path(bid,sid)
        if (path/'session.json').is_file():return get_session(bid,sid)
        with lock_file(lock_root()/'compute.lock'):
            source,_=workflow.preview_file(bid,rid)
            if source.stat().st_size>cutting.settings().get('max_source_bytes',512*1024**2):raise CutError('本机支持 512 MB 以内的成片')
            storage.require_space(source.parent,source.stat().st_size*3)
            path.mkdir(parents=True,exist_ok=True)
            try:
                temporary=path/'source.partial.mp4';shutil.copyfile(source,temporary)
                if file_sha256(temporary)!=sha:raise CutError('原成片内容已变化，请重新打开',409)
                meta=cutting.probe_source(temporary)
                cutting_access.require_editable(workflow.folder(bid),rid,source_sha=sha)
                temporary.replace(path/'source.mp4')
                write_json_atomic(path/'session.json',{'schema_version':1,'id':sid,'batch_id':bid,'request_id':rid,
                    'source_sha256':sha,'source_filename':source.name,'metadata':meta,'created_at':now_iso(),'reset_at':''})
            except Exception:
                shutil.rmtree(path,ignore_errors=True);raise
        return get_session(bid,sid)


def frame_file(batch_id, session_id, index):
    data = read_session(batch_id, session_id)
    if type(index) is not int or not 0 <= index < data['metadata']['frame_count']:
        raise CutError('帧号超出视频范围')
    path = session_path(batch_id, session_id)
    cache = path/'frames'
    output = cache/f'{index}.jpg'
    if output.is_file():
        output.touch()
        return output
    with frame_lease(lock_root()/'frame.lock'):
        cache.mkdir(exist_ok=True)
        if output.is_file():
            output.touch()
            return output
        meta = data['metadata']
        start = max(0, index-8)
        size = min(32, meta['frame_count']-start)
        rate = Fraction(meta['fps_num'], meta['fps_den'])
        # For verified zero-origin CFR, a half-frame margin is strictly between
        # adjacent presentation timestamps, including fractional frame rates.
        seek = Fraction(2*start-1, 2)/rate if start else 0
        with tempfile.TemporaryDirectory(prefix='frame-window-', dir=cache) as directory:
            command = ['ffmpeg', '-y', '-v', 'error']
            if start:
                command += ['-ss', f'{float(seek):.9f}']
            run(command + ['-i', str(path/'source.mp4'), '-an', '-vf', "scale=w='min(540,iw)':h=-2",
                 '-frames:v', str(size), '-fps_mode', 'passthrough', '-q:v', '2',
                 '-start_number', str(start), str(Path(directory)/'%d.jpg')], 60)
            files = [Path(directory)/f'{n}.jpg' for n in range(start, start+size)]
            if not all(f.is_file() and f.stat().st_size for f in files):
                raise CutError('准确帧窗口不完整，请重试')
            for file in files:
                file.replace(cache/file.name)
            output.touch()
            for old in sorted(cache.glob('*.jpg'), key=lambda p: p.stat().st_mtime, reverse=True)[128:]:
                old.unlink(missing_ok=True)
            return output


def discard(bid,sid,reset_at):
    """Fence old drafts/results while retaining completed source discovery."""
    from . import mosaic_scan, cutting_scan
    session=read_session(bid,sid);root=session_path(bid,sid)
    with cutting_access.gate(workflow.folder(bid),session['request_id']):
        cutting_access.require_editable(workflow.folder(bid),session['request_id'],source_sha=session['source_sha256'])
        with lock_file(root/'render-state.lock',blocking=True), lock_file(root/'scan-state.lock',blocking=True):
            session=read_session(bid,sid);cutting.require_reset(session,reset_at)
            scan=mosaic_scan.recover(root/'scan.json')
            if scan and scan['status']=='running':raise CutError('正在识别，请识别完成后再放弃',409)
            previous_identity=mosaic_scan.identity(session,cutting_scan.settings())
            for path in root.glob('render-*.json'):
                job=read_json(path)
                if job.get('status')=='running' and job.get('reset_at','')==reset_at and job.get('source_sha256')==session['source_sha256']:
                    job.update(cancel_requested=True,progress='正在停止生成…')
                    write_json_atomic(path,job)
            session['reset_at']=now_iso()
            write_json_atomic(root/'session.json',session)
            if scan:
                scan['reset_at']=session['reset_at']
                if scan.get('identity')==previous_identity:scan['identity']=mosaic_scan.identity(session,cutting_scan.settings())
                write_json_atomic(root/'scan.json',scan)
    return get_session(bid,sid)
