"""Shared, versioned timeline sprites from the immutable local mosaic source."""
import math
import re
import tempfile
import threading
import time
from pathlib import Path
from . import cutting, diagnostics, mosaic_session as sessions, runtime
from .util import read_json, write_json_atomic, now_iso

VERSION='v1'
TILE_WIDTH=112
TILE_HEIGHT=116
COLUMNS=20
RETRY_SECONDS=600
ERROR='缩略图生成失败，可继续编辑'
_guard=threading.Lock()
_running=set()


def cache_path(sha):
    if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):raise cutting.CutError('成片版本参数无效')
    return runtime.data_root()/'.web-state/filmstrips'/sha


def _ready(root):
    if (root/(VERSION+'.jpg')).is_file() and (root/(VERSION+'.json')).is_file():
        return read_json(root/(VERSION+'.json'))


def _failed(root):
    path=root/(VERSION+'.failed.json')
    return path.is_file() and time.time()-read_json(path)['failed_at']<RETRY_SECONDS


def _generate(source,session,root):
    # Blocking only in the daemon: another process may already own this SHA.
    with cutting.lock_file(root/(VERSION+'.lock'),blocking=True):
        try:
            if _ready(root) or _failed(root):return
            if not source.is_file():raise FileNotFoundError('local mosaic source missing')
            meta=session['metadata'];total=int(meta['frame_count']);count=min(total,240)
            if count<1:raise ValueError('empty source')
            duration=total*meta['fps_den']/meta['fps_num'];rows=math.ceil(count/COLUMNS)
            frames=[(i*total//count+(i+1)*total//count)//2 for i in range(count)]
            manifest={'schema_version':1,'version':VERSION,'source_sha256':session['source_sha256'],
                'count':count,'columns':COLUMNS,'rows':rows,'tile_width':TILE_WIDTH,'tile_height':TILE_HEIGHT,
                'frames':frames,'frame_count':total,'created_at':now_iso()}
            with tempfile.TemporaryDirectory(prefix=VERSION+'-',dir=root) as directory:
                output=Path(directory)/'sprite.jpg'
                # Decode keyframes only. Extend the last keyframe so even a one-keyframe
                # clip supplies exactly count samples; trim before tile to bound the sheet.
                filters=(f'tpad=stop_mode=clone:stop_duration={duration:.9f},'
                    f'fps={count}/{duration:.9f}:start_time=0,trim=end_frame={count},'
                    f'scale={TILE_WIDTH}:{TILE_HEIGHT}:force_original_aspect_ratio=increase,'
                    f'crop={TILE_WIDTH}:{TILE_HEIGHT},tile={COLUMNS}x{rows}:nb_frames={count}')
                cutting.run(['ffmpeg','-y','-v','error','-skip_frame','nokey','-i',str(source),
                    '-an','-vf',filters,'-frames:v','1','-q:v','3',str(output)],90,light=True)
                if not output.is_file() or not output.stat().st_size:raise RuntimeError('empty filmstrip')
                output.replace(root/(VERSION+'.jpg'))
                write_json_atomic(root/(VERSION+'.json'),manifest)
            (root/(VERSION+'.failed.json')).unlink(missing_ok=True)
        except Exception as exc:
            diagnostics.event('editor.filmstrip_failed',level='warning',source_sha256=session['source_sha256'],
                              error_type=type(exc).__name__,reason=str(exc)[:500])
            write_json_atomic(root/(VERSION+'.failed.json'),{'schema_version':1,'failed_at':time.time(),'created_at':now_iso()})


def _worker(source,session,root):
    try:_generate(source,session,root)
    except Exception as exc:
        diagnostics.event('editor.filmstrip_failed',level='warning',error_type=type(exc).__name__,reason=str(exc)[:500])
    finally:
        with _guard:_running.discard(root)


def state(bid,sid):
    session=sessions.read_session(bid,sid);root=cache_path(session['source_sha256'])
    manifest=_ready(root)
    if manifest:return {'status':'ready',**manifest,'url':f'/api/batches/{bid}/mosaics/{sid}/filmstrip/image?v={VERSION}'}
    if _failed(root):return {'status':'failed','error':ERROR}
    with _guard:
        if root not in _running:
            root.mkdir(parents=True,exist_ok=True);_running.add(root)
            try:threading.Thread(target=_worker,args=(sessions.session_path(bid,sid)/'source.mp4',session,root),daemon=True).start()
            except Exception:
                _running.discard(root);raise
    return {'status':'running'}


def image_file(bid,sid):
    root=cache_path(sessions.read_session(bid,sid)['source_sha256'])
    if not _ready(root):raise cutting.CutError('缩略图尚未就绪',404)
    return root/(VERSION+'.jpg')
