"""Validate local episode files and copy them without hard links or macOS clone."""
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import signal
import time
import uuid
from fractions import Fraction
from pathlib import Path
from .runtime import data_root
from .media import ffprobe, run_checked
from .util import file_sha256


def validate_downloaded_video(video):
    """A fast-start MP4 may advertise its full duration before download finishes."""
    try:
        payload = ffprobe(video)
        stream = next((s for s in payload.get('streams', []) if s.get('codec_type') == 'video'), None)
        if stream is None:
            raise ValueError('缺少视频轨道')
        # Container duration may include an audio-only tail. Preserve source PTS
        # and decode a window, rather than seeking past the last video frame.
        start = float(stream.get('start_time') or 0)
        length = float(stream['duration']) if stream.get('duration') not in (None, 'N/A') else None
        if not math.isfinite(start) or length is not None and (not math.isfinite(length) or length <= 0):
            raise ValueError('无效视频时长')
        container_start = float(payload.get('format', {}).get('start_time') or 0)
        seek = (['-ss', f'{max(0, start - container_start + length - 2):.6f}']
                if length is not None else [])
        frames = run_checked(['ffmpeg', '-v', 'error', '-copyts', *seek,
                              '-i', str(video), '-map', '0:v:0', '-fps_mode', 'passthrough',
                              '-enc_time_base', '-1', '-f', 'framemd5', '-'], timeout=30,
                             light=length is not None)
        timebase = None
        end = None
        for line in frames.stdout.splitlines():
            if line.startswith('#tb 0:'):
                timebase = float(Fraction(line.split(':', 1)[1].strip()))
            elif line.strip() and not line.startswith('#'):
                fields = line.split(',')
                if timebase is None:
                    raise ValueError('缺少解码时间基准')
                end = (int(fields[2]) + int(fields[3])) * timebase
        if end is None:
            raise ValueError('视频尾部无法完整解码')
        if length is not None and end < start + length - max(timebase, .001):
            raise ValueError('视频尾部短于声明时长')
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(f'{video.name} 正片尚不完整或无法读取，未复制；请重试下载') from exc


def stop_download(process):
    """Reap the entire dedicated Hub process group before releasing the runner."""
    exited = process.poll() is not None
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        if not exited:
            process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The Hub parent may exit while a subordinate ignores SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait(timeout=5)


def run_download(command, source, log, progress=None, *, total_timeout=1800, idle_timeout=300):
    """Bound Hub work by active elapsed time and actual media-file progress."""
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    elapsed = idle = 0.0
    previous_time = time.monotonic()
    previous_files = None
    try:
        while process.poll() is None:
            now = time.monotonic()
            delta = now - previous_time
            previous_time = now
            # SIGSTOP suspends this parent and its children; don't charge that gap.
            delta = delta if delta < 10 else 2.0
            elapsed += delta
            files = []
            for path in source.glob('EP*'):
                try:
                    stat = path.stat()
                    files.append((path.name, stat.st_size, stat.st_mtime_ns))
                except FileNotFoundError:
                    continue
            signature = sorted(files)
            idle = 0.0 if signature != previous_files else idle + delta
            previous_files = signature
            if progress:
                progress(files)
            if elapsed >= total_timeout or idle >= idle_timeout:
                reason = '下载总时长超限' if elapsed >= total_timeout else '下载长时间无文件进展'
                raise TimeoutError(reason + '，已停止本次下载，可重试；已有正片保留')
            time.sleep(2)
        return process.poll()
    finally:
        stop_download(process)


def place_file(source: Path, destination: Path) -> str:
    """Place a complete file at destination. Returns 'reflink' or 'copy'."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.part')
    mode = 'copy'
    if hasattr(fcntl, 'FICLONE'):
        src = os.open(source, os.O_RDONLY)
        try:
            dst = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            try:
                fcntl.ioctl(dst, fcntl.FICLONE, src)
                mode = 'reflink'
            except OSError:
                mode = 'copy'
            finally:
                os.close(dst)
        finally:
            os.close(src)
    if mode == 'copy':
        shutil.copy2(source, temporary)
    if temporary.stat().st_size != source.stat().st_size:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f'{source.name} 复制后大小不一致，未替换目标文件')
    os.replace(temporary, destination)
    return mode


def mirror_videos(project, source, *, episodes=None):
    target = project / 'source/videos'
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for video in sorted(source.glob('EP*.mp4')):
        if not re.fullmatch(r'EP\d+\.mp4', video.name) or video.stat().st_size == 0:
            continue
        if episodes is not None and video.stem not in episodes:
            continue
        destination = target / video.name
        validate_downloaded_video(video)
        damaged = False
        if destination.exists():
            if destination.stat().st_size == video.stat().st_size and file_sha256(destination) == file_sha256(video):
                continue
            try:
                validate_downloaded_video(destination)
            except RuntimeError:
                damaged = True
            else:
                raise RuntimeError(f'{video.name} 已存在但内容不同，请人工核对，未覆盖')
        if damaged:
            quarantine = target / 'quarantine'
            quarantine.mkdir(exist_ok=True)
            destination.replace(quarantine / f'{video.stem}-{uuid.uuid4().hex}.mp4')
        place_file(video, destination)
        copied.append(video.name)
    return copied


def main():
    from .source_cache import cached_names, check_drama, ensure
    if len(sys.argv) < 2:
        raise SystemExit(2)
    drama_id = sys.argv[1]
    episodes = sys.argv[2:]
    check_drama(drama_id)
    project = data_root() / 'projects' / drama_id
    if not (project / 'project.json').is_file():
        raise ValueError('请先创建项目')
    names = sorted(ensure(drama_id, episodes)) if episodes else cached_names(drama_id)
    print(json.dumps({'cached': names, 'local_video_count': len(names)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    from . import editor_resources
    editor_resources.production_process()
    raise SystemExit(main())
