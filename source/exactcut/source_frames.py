"""Source frame indexing and conversion to the existing millisecond timeline."""
import json
from bisect import bisect_left
import math
import re
from fractions import Fraction

from . import media, source_prefetch, workflow, resource_locks
from .util import json_sha256, read_json, write_json_atomic

def _video(drama, episode):
    try:
        return media.resolve_episode_video(workflow.ROOT / 'projects' / drama, episode)
    except (OSError, ValueError, RuntimeError):
        raise ValueError('本集正片尚未就绪，请先准备正片，等待帧索引准备完成') from None


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
        return _public(_load(path,key)[0])
    with resource_locks.hold('source-index',key):
        if target.is_file():return _public(_load(path,key)[0])
        # Projects retain their existing index.json layout. Reuse a validated
        # same-video index already published by another project under this key.
        for cached in workflow.BATCHES.glob('*/source-picker/'+key+'/index.json'):
            if any(part.is_symlink() for part in (cached,cached.parent,cached.parent.parent)):continue
            try:
                doc,_,_=_load(cached.parents[2],key)
                if doc.get('key')!=key or doc.get('signature')!=signature:continue
            except (OSError,ValueError,KeyError,TypeError):continue
            source_prefetch.ensure_editable(path)
            if _fingerprint(video)!=signature:raise ValueError('正片已变化，请重新准备帧索引')
            write_json_atomic(target,doc);return _public(doc)
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
        raise ValueError('无效帧索引标识')
    target = path / 'source-picker' / key / 'index.json'
    if not target.is_file():
        raise ValueError('帧索引已失效，请重新准备正片')
    doc = read_json(target)
    video = _video(doc['drama'], doc['episode'])
    if _fingerprint(video) != doc['signature']:
        raise ValueError('正片已变化，请重新准备帧索引')
    return doc, video, target.parent


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


def _clock(minutes, seconds, frame):
    return f'{minutes:02d}:{seconds:02d}:{frame:03d}'


class ClipValidationError(ValueError):
    def __init__(self, message, *, fields=()):
        super().__init__(message)
        self.fields = list(fields)


def _reject_frame(episode, side, numbers, ordinal, count, times):
    """Explain a frame the editor typed, using the same 分:秒:帧 fields."""
    label = '开始' if side == 'start' else '结束'
    clock = _clock(numbers[side + '_m'], numbers[side + '_s'], ordinal)
    if count == 0:
        last_ms = times[-1]
        second = numbers[side + '_m'] * 60 + numbers[side + '_s']
        if second * 1000 > last_ms:
            last_second = last_ms // 1000
            last_ordinal = len(times) - bisect_left(times, last_second * 1000) - 1
            minutes, seconds = divmod(last_second, 60)
            last = _clock(minutes, seconds, last_ordinal)
            raise ClipValidationError(f'{episode} {label}时间 {clock} 超出这一集的长度，请改到 {last} 或更早', fields=[side + '_' + key for key in ('m', 's', 'f')])
        fields = [side + '_' + key for key in ('m', 's', 'f')]
        raise ClipValidationError(f'{episode} {label}时间 {clock} 这一秒没有画面，请改到有画面的时间', fields=fields)
    raise ClipValidationError(f'{episode} {label}时间 {clock} 超出这一秒的帧数，请填 0 到 {count - 1}', fields=[side + '_f'])


def resolve_frame_clips(row, drama, path):
    """Compile explicit frame drafts from a current index; never probe under save locks.

    Frame numbers are zero-based within the actual source presentation second.
    The end frame is inclusive, mapped to the next frame's exclusive boundary.
    """
    from copy import deepcopy
    from .intake import _clip_fields, compile_clips, _segment_fields, _clip_idle
    result = deepcopy(row)
    indexes = {}
    for clip_index, clip in enumerate(result['clips']):
        try:
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
                    _reject_frame(episode, side, numbers, ordinal, count, doc['times_ms'])
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
        except ValueError as exc:
            error = ClipValidationError(str(exc), fields=getattr(exc, 'fields', ()))
            error.clip_index = clip_index
            raise error from exc
    compile_clips(result)
    return result
