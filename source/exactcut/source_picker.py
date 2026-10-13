"""Read-only source picking. Durable boundaries use renderer's millisecond clock."""
import json
from bisect import bisect_left
import math
import re
import subprocess
import threading
import tempfile
from pathlib import Path
from contextlib import contextmanager
from fractions import Fraction

from . import media, source_prefetch, workflow
from .util import json_sha256, read_json, write_json_atomic

# Separate bounded interactive work; never occupies the durable mutation lock.
_SLOTS = threading.BoundedSemaphore(2)
_CACHE_LOCK = threading.Lock()


@contextmanager
def _slot():
    if not _SLOTS.acquire(blocking=False):
        raise ValueError('准确帧资源繁忙，请稍后重试')
    try:
        yield
    finally:
        _SLOTS.release()


def _video(drama, episode):
    try:
        return media.resolve_episode_video(workflow.ROOT / 'projects' / drama, episode)
    except (OSError, ValueError, RuntimeError):
        raise ValueError('本集正片尚未就绪，请先准备正片，完成后重新打开选帧') from None


def _fingerprint(video):
    stat = video.stat()
    return [str(video), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _public(doc):
    return {k: doc[k] for k in ('key', 'episode', 'fps', 'times_ms', 'duration_ms')}


def prepare(path, draft, episode):
    source_prefetch.ensure_editable(path)
    drama, episodes, _ = source_prefetch.snapshot(draft)
    if episode not in episodes:
        raise ValueError('请选择当前草稿中有效片段的集数')
    video = _video(drama, episode)
    signature = _fingerprint(video)
    key = json_sha256({'version': 1, 'drama': drama, 'episode': episode, 'signature': signature})
    target = path / 'source-picker' / key / 'index.json'
    if target.is_file():
        return _public(read_json(target))
    with _slot():
        try:
            result = media.run_checked(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_frames', '-show_streams', '-show_format', '-show_entries',
                'frame=pts,key_frame:stream=time_base,avg_frame_rate,duration,start_time:format=start_time,duration',
                '-of', 'json', str(video)], timeout=180)
            probe = json.loads(result.stdout)
            stream = probe['streams'][0]
            frames = probe['frames']
            clock = Fraction(stream['time_base'])
            origin = Fraction(probe['format']['start_time'])
            pts = [int(f['pts']) for f in frames]
            times = [math.floor((p * clock - origin) * 1000) for p in pts]
            end = (Fraction(stream['start_time']) + Fraction(stream['duration']) - origin) * 1000
            duration = math.ceil(end)
            if not times or len(times) > 1_000_000 or times[0] < 0 or any(a >= b for a, b in zip(times, times[1:])) or duration <= times[-1]:
                raise ValueError('unsupported timestamps')
            doc = {'key': key, 'drama': drama, 'episode': episode,
                   'signature': signature, 'fps': stream['avg_frame_rate'],
                   'times_ms': times, 'duration_ms': duration, 'pts': pts,
                   'keyframes': [i for i, f in enumerate(frames) if f['key_frame']],
                   'time_base': str(clock), 'origin': str(origin)}
            if _fingerprint(video) != signature:
                raise ValueError('source changed')
            write_json_atomic(target, doc)
        except (RuntimeError, OSError, KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            raise ValueError('正片帧索引暂不可用，请重新准备正片后重试') from None
    return _public(doc)


def _load(path, key):
    if not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('无效选帧标识')
    target = path / 'source-picker' / key / 'index.json'
    if not target.is_file():
        raise ValueError('选帧已失效，请重新打开')
    doc = read_json(target)
    video = _video(doc['drama'], doc['episode'])
    if _fingerprint(video) != doc['signature']:
        raise ValueError('正片已变化，请重新打开选帧')
    return doc, video, target.parent


def video(path, key):
    return _load(path, key)[1]


def frame(path, key, index):
    doc, source, directory = _load(path, key)
    if not 0 <= index < len(doc['pts']):
        raise ValueError('帧序号超出范围')
    target = directory / f'{index}.png'
    with _CACHE_LOCK:
        if target.is_file():
            return target.read_bytes()
    with _slot():
        preceding = max((i for i in doc['keyframes'] if i <= index), default=0)
        seek = max(0, doc['times_ms'][preceding])
        # Preserve source PTS through seek; select the indexed presentation timestamp.
        command = ['ffmpeg', '-v', 'error', '-copyts', '-ss', f'{seek / 1000:.3f}',
                   '-i', str(source), '-vf', f"select=eq(pts\\,{doc['pts'][index]})",
                   '-frames:v', '1', '-fps_mode', 'passthrough', '-f', 'image2pipe', '-c:v', 'png', '-']
        try:
            result = subprocess.run(media.bounded_ffmpeg(command), capture_output=True, timeout=30)
            if result.returncode or not result.stdout.startswith(b'\x89PNG'):
                raise ValueError('frame unavailable')
            _load(path, key)
            with _CACHE_LOCK:
                # Publish a complete inode; concurrent responses never see truncation.
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(result.stdout)
                    temporary.replace(target)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                # Bound image storage for this batch across all source revisions.
                images = sorted((path / 'source-picker').glob('*/*.png'), key=lambda p: p.stat().st_mtime_ns)
                for old in images[:-256]:
                    if old != target:
                        old.unlink(missing_ok=True)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise ValueError('准确帧暂不可用，请重新打开选帧后重试') from None
    return result.stdout


def copy_frame_indexes(source, target, draft):
    """Copy only current, referenced frame indexes; never probe under clone locks."""
    drama = str(draft.get('drama_id', '')).strip().strip('#')
    episodes = set()
    for row in draft.get('rows', []):
        for clip in row.get('clips') or []:
            if not isinstance(clip, dict) or clip.get('time_format') != 'source_frames_v1':
                continue
            episode = str(clip.get('episode', '')).strip().upper().removeprefix('EP')
            if episode.isdigit() and 1 <= int(episode) <= 500:
                episodes.add('EP' + str(int(episode)))
    for episode in sorted(episodes):
        try:
            signature = _fingerprint(_video(drama, episode))
            key = json_sha256({'version': 1, 'drama': drama, 'episode': episode, 'signature': signature})
            directory = source / 'source-picker' / key
            index = directory / 'index.json'
            if any(p.is_symlink() for p in (source / 'source-picker', directory, index)) or not index.is_file():
                continue
            doc, _, _ = _load(source, key)
            if (doc.get('key'), doc.get('drama'), doc.get('episode'), doc.get('signature')) != (key, drama, episode, signature):
                continue
            # Recheck immediately before atomic publication. Later validation
            # checks again, including when the source is replaced after copying.
            if _fingerprint(_video(drama, episode)) != signature:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Missing/stale cache is reported by normal draft validation.
        write_json_atomic(target / 'source-picker' / key / 'index.json', doc)


def resolve_frame_clips(row, drama, path):
    """Compile explicit frame drafts from a current index; never probe under save locks.

    Frame numbers are zero-based within the actual source presentation second.
    The end frame is inclusive, mapped to the next frame's exclusive boundary.
    """
    from copy import deepcopy
    from .intake import _clip_fields, compile_clips, _segment_fields, _clip_idle
    result = deepcopy(row)
    indexes = {}
    for clip in result['clips']:
        if not isinstance(clip, dict) or not clip.get('time_format'):
            continue
        if clip['time_format'] != 'source_frames_v1':
            raise ValueError('未知片段时间格式，请核对后重新保存')
        stored, numbers = _clip_fields(clip)
        episode = stored['episode']
        if _clip_idle(stored, numbers):
            clip.clear()
            continue
        if any(stored[key] == '' for key in numbers):
            raise ValueError('原片帧时间的分、秒、帧均为必填，请明确填写0或具体数字')
        if not (episode.isdigit() and 1 <= int(episode) <= 500 and all(n is not None for n in numbers.values())):
            raise ValueError('原片帧时间须填写有效集号和分:秒:帧，秒为0～59')
        episode = 'EP' + str(int(episode))
        if episode not in indexes:
            if path is None:
                raise ValueError('原片帧索引尚未就绪，请等待正片及帧索引准备完成后重试')
            source = _video(drama, episode)
            signature = _fingerprint(source)
            key = json_sha256({'version': 1, 'drama': drama, 'episode': episode, 'signature': signature})
            target = path / 'source-picker' / key / 'index.json'
            if not target.is_file():
                raise ValueError('原片帧索引尚未就绪或正片已变化，请等待正片及帧索引准备完成后重试')
            indexes[episode] = _load(path, key)[0]
        doc = indexes[episode]
        chosen = []
        for side in ('start', 'end'):
            second = numbers[side + '_m'] * 60 + numbers[side + '_s']
            first_in_second = bisect_left(doc['times_ms'], second * 1000)
            next_second = bisect_left(doc['times_ms'], (second + 1) * 1000)
            count = next_second - first_in_second
            ordinal = numbers[side + '_f']
            if ordinal >= count:
                raise ValueError(f'{episode} 第{second}秒原片帧序号超出范围（本秒{count}帧，从0开始）')
            chosen.append(first_in_second + ordinal)
        first, last = chosen
        if last < first:
            raise ValueError('结束帧不能早于开始帧')
        boundaries = [doc['times_ms'][first], doc['times_ms'][last + 1] if last + 1 < len(doc['times_ms']) else doc['duration_ms']]
        effects = _segment_fields(clip)
        clip.clear()
        clip.update(effects)
        clip['episode'] = episode
        for side, boundary in zip(('start', 'end'), boundaries):
            minutes, rest = divmod(boundary, 60000)
            seconds, ms = divmod(rest, 1000)
            clip.update({side + '_m': str(minutes), side + '_s': str(seconds), side + '_ms': str(ms)})
    compile_clips(result)
    return result
