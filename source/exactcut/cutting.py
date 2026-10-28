"""Frame-addressed, non-destructive postprocessing of generated master videos."""
from __future__ import annotations

import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from .media import bounded_ffmpeg, ffprobe
from .util import load_config, file_sha256


class CutError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def settings():
    return load_config().get('cutting', {})


def enabled():
    return settings().get('enabled') is True


def run(command, timeout=600):
    try:
        from . import processing_cancel
        result = processing_cancel.run(bounded_ffmpeg(command), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CutError('视频处理超时，请重试；原成片保留') from exc
    if result.returncode:
        raise CutError('视频处理失败，请重试；原成片保留')
    return result.stdout


def normalize_ranges(ranges, count):
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 40:
        raise CutError('请添加 1～40 段要删除的画面')
    checked = []
    for item in ranges:
        if (not isinstance(item, list) or len(item) != 2
                or any(type(n) is not int for n in item) or not 0 <= item[0] < item[1] <= count):
            raise CutError('删除范围必须是视频内的整数帧，起点小于终点')
        checked.append(item.copy())
    merged = []
    for start, end in sorted(checked):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    if sum(b-a for a, b in merged) == count:
        raise CutError('不能删除全部视频，请至少保留一帧')
    return merged


def keep_ranges(ranges, count):
    result, cursor = [], 0
    for start, end in normalize_ranges(ranges, count):
        if start > cursor:
            result.append([cursor, start])
        cursor = end
    if cursor < count:
        result.append([cursor, count])
    return result


def normalize_mosaics(items, meta):
    if not isinstance(items, list) or len(items) > 40:
        raise CutError('打码区域最多 40 处')
    width, height, count = meta['width'], meta['height'], meta['frame_count']
    checked = []
    for item in items:
        if not isinstance(item, dict):
            raise CutError('打码区域格式不正确')
        try:
            start, end, x, y, w, h = (item[key] for key in ('start', 'end', 'x', 'y', 'w', 'h'))
        except KeyError as exc:
            raise CutError('打码区域格式不正确') from exc
        if any(type(n) is not int for n in (start, end, x, y, w, h)):
            raise CutError('打码区域必须是整数帧和整数像素')
        if not 0 <= start < end <= count:
            raise CutError('打码时间必须在视频内，起点小于终点')
        if any(n % 2 or n < 0 for n in (x, y, w, h)) or w < 16 or h < 16 or x + w > width or y + h > height:
            raise CutError('打码区域需为偶数像素、至少 16×16，且完整落在画面内')
        checked.append({'start': start, 'end': end, 'x': x, 'y': y, 'w': w, 'h': h})
    return checked


def probe_source(source):
    config = settings()
    if source.stat().st_size > config.get('max_source_bytes', 512*1024**2):
        raise CutError('本机剪切支持 512 MB 以内的成片')
    try:
        data = ffprobe(source)
        video = next(s for s in data['streams'] if s['codec_type'] == 'video')
        rate = Fraction(video['avg_frame_rate'])
        tick = Fraction(video['time_base'])
        duration = float(video['duration'])
        start = float(video['start_time'])
        audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
        if not 0 < rate <= 60 or not 0 < duration <= config.get('max_duration_seconds', 300):
            raise CutError('本机剪切支持 5 分钟以内、60 fps 以内的成片')
        if max(video['width'], video['height']) > 1920:
            raise CutError('本机剪切支持最长边 1920 像素以内的成片')
        if abs(start) > float(tick) or any(s.get('rotation', 0) % 360 for s in video.get('side_data_list', [])):
            raise CutError('暂不支持带时间偏移或旋转标记的视频，请使用项目生成的成片')
        raw = run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                   '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(source)], 60)
        frames = json.loads(raw)['frames']
        if not frames or any(abs(Fraction(f['best_effort_timestamp_time']) - Fraction(n, 1)/rate)
                             > max(tick*2, Fraction(2, 1000000)) for n, f in enumerate(frames)):
            raise CutError('暂不支持可变帧率视频，请使用项目生成的恒定帧率成片')
        count = len(frames)
        if abs(duration - float(count/rate)) > float(1/rate):
            raise CutError('视频时间戳不连续，无法可靠剪切')
        if audio and (abs(float(audio['start_time'])) > .05
                      or abs(float(audio['duration'])-float(count/rate)) > .1):
            raise CutError('音视频时间轴不对齐，暂不能可靠剪切')
        return {'fps_num': rate.numerator, 'fps_den': rate.denominator, 'frame_count': count,
                'width': video['width'], 'height': video['height'], 'has_audio': audio is not None}
    except (KeyError, StopIteration, ZeroDivisionError, RuntimeError) as exc:
        raise CutError('无法读取完整的成片帧信息') from exc


def render_video(source, output, ranges, meta, mosaics=None, *, video_graph=None, video_inputs=()):
    mosaics = normalize_mosaics([] if mosaics is None else mosaics, meta)
    if ranges:
        kept = keep_ranges(ranges, meta['frame_count'])
    elif mosaics:
        kept = [[0, meta['frame_count']]]
    else:
        raise CutError('请添加删除范围或打码区域')
    expected = sum(b-a for a, b in kept)
    num, den = meta['fps_num'], meta['fps_den']
    rate = Fraction(num, den)
    render = load_config()['render']
    audio_rate, channels = int(render['audio_rate']), int(render['audio_channels'])
    if audio_rate not in {44100, 48000} or channels not in {1, 2}:
        raise CutError('剪切音频配置需为 44100/48000 Hz、单声道或双声道')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cut-render-', dir=output.parent) as temporary:
        work = Path(temporary)
        video_only = work/'video.mp4'
        predicate = '+'.join(f'gte(n,{a})*lt(n,{b})' for a, b in kept)
        tail = f"select='{predicate}',setpts=N*{den}/({num}*TB)"
        if video_graph is not None:
            video_args = ['-filter_complex', video_graph+';[out]'+tail+'[vout]', '-map', '[vout]']
        elif mosaics:
            parts, last = [], '0:v'
            for index, box in enumerate(mosaics):
                enable = f"gte(n,{box['start']})*lt(n,{box['end']})"
                parts.append(f"[{last}]split[b{index}][s{index}]")
                parts.append(f"[s{index}]crop={box['w']}:{box['h']}:{box['x']}:{box['y']},pixelize=w=16:h=16:mode=avg[m{index}]")
                parts.append(f"[b{index}][m{index}]overlay={box['x']}:{box['y']}:enable='{enable}'[o{index}]")
                last = f'o{index}'
            parts.append(f'[{last}]{tail}[vout]')
            video_args = ['-filter_complex', ';'.join(parts), '-map', '[vout]']
        else:
            video_args = ['-vf', tail]
        run(['ffmpeg', '-y', '-v', 'error', '-i', str(source), *[arg for path in video_inputs for arg in ('-i',str(path))], '-an', *video_args,
             '-c:v', 'libx264', '-crf', str(render['crf']), '-preset', render['preset'],
             '-pix_fmt', 'yuv420p', '-r', f'{num}/{den}', '-movflags', '+faststart', str(video_only)])
        final = video_only
        if meta['has_audio']:
            total_samples = round(Fraction(meta['frame_count']*audio_rate, 1)/rate)
            raw, cut = work/'source.pcm', work/'cut.pcm'
            run(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vn', '-af',
                 f'aresample={audio_rate}:async=1:first_pts=0,apad=whole_len={total_samples},atrim=end_sample={total_samples}',
                 '-ar', str(audio_rate), '-ac', str(channels), '-f', 's16le', str(raw)])
            with raw.open('rb') as incoming, cut.open('wb') as outgoing:
                for a, b in kept:
                    start = round(Fraction(a*audio_rate, 1)/rate)
                    end = round(Fraction(b*audio_rate, 1)/rate)
                    remaining = (end-start)*channels*2
                    incoming.seek(start*channels*2)
                    while remaining:
                        chunk = incoming.read(min(1024*1024, remaining))
                        if not chunk:
                            raise CutError('音频采样不足，剪切未保存')
                        outgoing.write(chunk)
                        remaining -= len(chunk)
            final = work/'final.mp4'
            run(['ffmpeg', '-y', '-v', 'error', '-i', str(video_only), '-f', 's16le',
                 '-ar', str(audio_rate), '-ac', str(channels), '-i', str(cut), '-map', '0:v:0',
                 '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-movflags', '+faststart', str(final)])
        result = probe_source(final)
        if any(result[k] != meta[k] for k in ('fps_num', 'fps_den', 'width', 'height', 'has_audio')) or result['frame_count'] != expected:
            raise CutError('剪切帧数或格式校验失败，结果未保存')
        if meta['has_audio']:
            audio = next(s for s in ffprobe(final)['streams'] if s['codec_type'] == 'audio')
            if abs(float(audio['duration']) - float(expected/rate)) > max(float(1/rate), 2048/audio_rate):
                raise CutError('剪切音画时长校验失败，结果未保存')
        run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(final), '-f', 'null', '-'])
        result['sha256'] = file_sha256(final)
        final.replace(output)
        return result


# Locks live outside the batch so deleting/recreating a directory cannot split a lease.
import fcntl
import re
import shutil
import threading
import time
from contextlib import contextmanager
from .util import read_json, write_json_atomic, json_sha256, now_iso


def lock_file(path, *, shared=False, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a')
    try:
        fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError as exc:
        stream.close()
        raise CutError('后台正在处理其他视频，请稍候点击重试', 429) from exc
    except BaseException:
        stream.close()
        raise
    return stream


def lock_root():
    from . import workflow
    return workflow.BATCHES.parent/'cut-locks'


@contextmanager
def frame_lease(path, *, timeout=2.0):
    """Absorb short frame-window contention without unbounded HTTP waits."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            stream = lock_file(path)
            break
        except CutError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CutError('准确帧正在生成，请稍后重试', 429) from exc
            time.sleep(min(.05, remaining))
    try:
        yield
    finally:
        stream.close()


@contextmanager
def batch_lease(batch_id, *, exclusive=False):
    stream = lock_file(lock_root()/f'{batch_id}.lock', shared=not exclusive)
    try:
        yield
    finally:
        stream.close()


def session_path(batch_id, session_id):
    from . import workflow
    if not isinstance(session_id, str) or not re.fullmatch('[0-9a-f]{32}', session_id):
        raise CutError('剪切会话不存在', 404)
    return workflow.folder(batch_id)/'cuts'/session_id


def require_request(batch_id, request_id):
    from . import workflow
    try:
        workflow.request_for(workflow.folder(batch_id), request_id)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        raise CutError('素材不存在或已删除', 404) from exc


def read_session(batch_id, session_id):
    path = session_path(batch_id, session_id)
    if not (path/'session.json').is_file():
        raise CutError('剪切会话不存在', 404)
    data = read_json(path/'session.json')
    if data['batch_id'] != batch_id:
        raise CutError('剪切会话不存在', 404)
    require_request(batch_id, data['request_id'])
    return data


def recover(path, data):
    changed = False
    for job in data['jobs']:
        if job['status'] != 'running':
            continue
        try:
            lease = lock_file(path/(job['id']+'.lock'))
        except CutError:
            continue
        lease.close()
        job.update(status='failed', error='上次处理已中断，请重新生成预览')
        changed = True
    if changed:
        write_json_atomic(path/'session.json', data)
    return data


def get_session(batch_id, session_id):
    from .cutting_replace import recover as recover_replacement
    recover_replacement(batch_id, session_id)
    path = session_path(batch_id, session_id)
    # Check existence before lock_file's parent creation.
    read_session(batch_id, session_id)
    with lock_file(path/'state.lock', blocking=True):
        from . import editor_source
        return {**recover(path, read_session(batch_id, session_id)),
                "capabilities": {"oss_playback": editor_source.enabled()}}


def require_reset(session, reset_at):
    if not isinstance(reset_at, str) or reset_at != session.get('reset_at', ''):
        raise CutError('本次编辑已在其他窗口放弃，请关闭后重新打开', 409)


def discard(batch_id, session_id, *, reset_at=''):
    """Abandon editing state, retaining immutable files and current project media."""
    from . import workflow, cutting_access
    data = get_session(batch_id, session_id)
    batch = workflow.folder(batch_id)
    with cutting_access.gate(batch, data['request_id']):
        cutting_access.require_editable(batch, data['request_id'], source_sha=data['source_sha256'])
        path = session_path(batch_id, session_id)
        with lock_file(path/'state.lock', blocking=True):
            data = read_session(batch_id, session_id)
            require_reset(data, reset_at)
            if any(j['status'] == 'running' or j.get('replacement', {}).get('status') == 'running' for j in data['jobs']):
                raise CutError('视频仍在处理中，请完成后再放弃', 409)
            for job in data['jobs']:
                job['discarded'] = True
            data['reset_at'] = now_iso()
            write_json_atomic(path/'session.json', data)
            return data


def open_session(batch_id, request_id, source_sha256):
    from . import workflow, storage
    if not isinstance(request_id, str) or not isinstance(source_sha256, str) or not re.fullmatch('[0-9a-f]{64}', source_sha256):
        raise CutError('成片版本参数无效')
    from .cutting_access import require_editable
    require_editable(workflow.folder(batch_id), request_id, source_sha=source_sha256)
    source, record = workflow.preview_file(batch_id, request_id)
    if record.get('output_sha256') != source_sha256:
        raise CutError('成片版本已变化，请刷新页面后重新打开剪切', 409)
    sid = json_sha256([batch_id, request_id, source_sha256])[:32]
    path = session_path(batch_id, sid)
    if (path/'session.json').is_file():
        return get_session(batch_id, sid)
    with lock_file(lock_root()/'compute.lock'):
        if (path/'session.json').is_file():
            return get_session(batch_id, sid)
        if source.stat().st_size > settings().get('max_source_bytes', 512*1024**2):
            raise CutError('本机剪切支持 512 MB 以内的成片')
        storage.require_space(source.parent, source.stat().st_size*3)
        path.mkdir(parents=True, exist_ok=True)
        temporary = path/'source.partial.mp4'
        try:
            shutil.copyfile(source, temporary)
            if file_sha256(temporary) != source_sha256:
                raise CutError('原成片内容已变化，请重新打开剪切', 409)
            meta = probe_source(temporary)
            # Recheck after copying to reject a concurrent generation switch.
            _, current_record = workflow.preview_file(batch_id, request_id)
            if current_record.get('output_sha256') != source_sha256:
                raise CutError('成片版本已变化，请重新打开剪切', 409)
            temporary.replace(path/'source.mp4')
            data = {'schema_version': 1, 'id': sid, 'batch_id': batch_id, 'request_id': request_id,
                    'source_sha256': source_sha256, 'source_filename': source.name, 'metadata': meta,
                    'created_at': now_iso(), 'jobs': []}
            write_json_atomic(path/'session.json', data)
            from . import editor_source
            return {**data, 'capabilities': {'oss_playback': editor_source.enabled()}}
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise


def get_job(data, job_id):
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if job is None:
        raise CutError('剪切任务不存在', 404)
    return job


def start_render(batch_id, session_id, ranges, mosaics=None):
    from . import storage
    path = session_path(batch_id, session_id)
    read_session(batch_id, session_id)
    with lock_file(path/'state.lock', blocking=True):
        data = recover(path, read_session(batch_id, session_id))
        meta = data['metadata']
        mosaics = normalize_mosaics([] if mosaics is None else mosaics, meta)
        if ranges is None:
            ranges = []
        if ranges:
            ranges = normalize_ranges(ranges, meta['frame_count'])
        elif not mosaics:
            raise CutError('请添加删除范围或打码区域')
        recipe = json_sha256({'ranges': ranges, 'mosaics': mosaics, 'source': data['source_sha256'],
                             'render': load_config()['render'], 'engine': 2,
                             **({'reset_at': data['reset_at']} if data.get('reset_at') else {})})[:32]
        existing = next((j for j in data['jobs'] if j['id'] == recipe), None)
        if existing and existing['status'] in {'running', 'ready', 'saved'}:
            return existing
        if not existing and len(data['jobs']) >= 20:
            raise CutError('本次成片最多生成 20 个剪切方案')
        compute = lock_file(lock_root()/'compute.lock')
        worker_lease = batch_lock = None
        try:
            worker_lease = lock_file(path/(recipe+'.lock'))
            batch_lock = lock_file(lock_root()/f'{batch_id}.lock', shared=True)
            source = path/'source.mp4'
            storage.require_space(path, max(source.stat().st_size*4, 512*1024**2))
            job = {'id': recipe, 'ranges': ranges, 'mosaics': mosaics, 'status': 'running',
                   'created_at': now_iso(),
                   'expected_frames': meta['frame_count']-sum(b-a for a, b in ranges)}
            data['jobs'] = [j for j in data['jobs'] if j['id'] != recipe] + [job]
            write_json_atomic(path/'session.json', data)
            task = threading.Thread(target=_render_job, args=(path, data, job, compute, worker_lease, batch_lock), daemon=True)
            task.start()
            return job.copy()
        except Exception:
            compute.close()
            if worker_lease: worker_lease.close()
            if batch_lock: batch_lock.close()
            raise


def _render_job(path, data, job, compute, worker_lease, batch_lock):
    output = path/(job['id']+'.mp4')
    try:
        if file_sha256(path/'source.mp4') != data['source_sha256']:
            raise CutError('剪切原片校验失败，请重新打开成片')
        result = render_video(path/'source.mp4', output, job['ranges'], data['metadata'], job.get('mosaics') or [])
        outcome = {'status': 'ready', 'output_sha256': result['sha256'], 'completed_at': now_iso()}
    except Exception as exc:
        output.unlink(missing_ok=True)
        outcome = {'status': 'failed', 'error': str(exc) if isinstance(exc, CutError) else '剪切失败，请重试；原成片保留'}
    try:
        with lock_file(path/'state.lock', blocking=True):
            latest = read_json(path/'session.json')
            get_job(latest, job['id']).update(outcome)
            write_json_atomic(path/'session.json', latest)
    finally:
        worker_lease.close()
        compute.close()
        batch_lock.close()


def output_file(batch_id, session_id, job_id):
    data = get_session(batch_id, session_id)
    job = get_job(data, job_id)
    if job['status'] not in {'ready', 'saved'}:
        raise CutError('剪切视频尚未准备好')
    file = session_path(batch_id, session_id)/(job['id']+'.mp4')
    if not file.is_file():
        raise CutError('剪切文件不存在，请重新生成')
    return file


def save_job(batch_id, session_id, job_id):
    path = session_path(batch_id, session_id)
    read_session(batch_id, session_id)
    with lock_file(path/'state.lock', blocking=True):
        data = recover(path, read_session(batch_id, session_id))
        job = get_job(data, job_id)
        if job['status'] not in {'ready', 'saved'}:
            raise CutError('请先生成并检查剪切预览')
        file = path/(job['id']+'.mp4')
        if not file.is_file() or file_sha256(file) != job['output_sha256']:
            raise CutError('剪切文件校验失败，未保存')
        if job['status'] != 'saved':
            job.update(status='saved', saved_at=now_iso())
            write_json_atomic(path/'session.json', data)
        return job


def saved_versions(batch_id, request_id):
    from . import workflow
    require_request(batch_id, request_id)
    result = []
    for file in (workflow.folder(batch_id)/'cuts').glob('*/session.json'):
        data = read_json(file)
        if data['request_id'] == request_id:
            for job in data['jobs']:
                if job['status'] == 'saved':
                    result.append({'session_id': data['id'], 'source_sha256': data['source_sha256'],
                                   'metadata': data['metadata'], 'source_filename': data['source_filename'], 'job': job})
    return sorted(result, key=lambda v: v['job']['saved_at'], reverse=True)


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
