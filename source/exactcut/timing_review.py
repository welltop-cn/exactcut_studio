"""Read source endpoints without blocking status polling or changing the user's draft."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
from pathlib import Path
import threading
import time
from .media import resolve_episode_video, run_checked
from .runtime import protect_path

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='source-duration')
_PENDING = OrderedDict()
_LOCK = threading.Lock()


@lru_cache(maxsize=256)
def _duration(path, device, inode, size, modified):
    protect_path(Path(path))
    payload = json.loads(run_checked(['ffprobe','-v','error','-show_streams','-show_format',
                                     '-of','json',path], timeout=5).stdout)
    value = round(float(payload.get('format',{}).get('duration') or 0)*1000)
    if value <= 0 or not any(s.get('codec_type')=='video' for s in payload.get('streams',[])):
        raise ValueError('正片的视频轨道或时长无效')
    stat = Path(path).stat()
    if (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)!=(device,inode,size,modified):
        raise ValueError('正片正在变化，请稍后重新核对')
    return value


def source_duration(path, *, wait=True):
    protect_path(path)
    stat = path.stat()
    key = (str(path.resolve()),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
    if wait:
        return _duration(*key)
    with _LOCK:
        item = _PENDING.get(key)
        if item and item[0].done() and item[0].exception() and time.monotonic()-item[1]>10:
            _PENDING.pop(key);item=None
        if item is None:
            while len(_PENDING)>=128:
                finished = next((k for k,v in _PENDING.items() if v[0].done()),None)
                if finished is None:return None
                _PENDING.pop(finished)
            item = (_POOL.submit(_duration,*key),time.monotonic())
            _PENDING[key] = item
        _PENDING.move_to_end(key)
    return item[0].result() if item[0].done() else None


def local_timeline(project, timeline, *, wait=True):
    resolved, states, durations = [], [], {}
    for index, original in enumerate(timeline,1):
        item = dict(original);state={'source_status':'ready','error':''}
        if item['type']=='episode':
            episode=item['episode']
            try:
                if episode not in durations:
                    path=resolve_episode_video(project,episode)
                    durations[episode]=source_duration(path,wait=wait)
                end=durations[episode]
                state['source_status']='checking' if end is None else 'ready'
                if end is not None:
                    state['source_duration_ms']=end
                    if item.get('full_episode') or item.get('to_end'):
                        item.update(start_ms=0 if item.get('full_episode') else item['start_ms'],
                                    end_ms=end,full_episode=False,to_end=False)
                        item['duration_ms']=end-item['start_ms']
                    if item['start_ms']>=end or item.get('end_ms',0) is not None and item['end_ms']>end+50:
                        raise ValueError(f'{episode} 选段超出正片时长，请核对起止时间')
            except FileNotFoundError:
                state['source_status']='missing'
            except (ValueError,RuntimeError,OSError) as exc:
                from .errors import ValidationError
                if isinstance(exc,ValidationError) and '缺少正片' in str(exc):
                    state['source_status']='missing'
                else:
                    state.update(source_status='invalid',error=f'片段 {index}：{str(exc)[:200]}')
        resolved.append(item);states.append(state)
    return resolved,states
