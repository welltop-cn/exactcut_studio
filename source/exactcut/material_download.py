"""Bridge the existing Material Hub CLI to this project's media directory."""
import json
import os
import re
import shutil
import subprocess
import sys
import signal
import time
import uuid
from pathlib import Path
from .catalog import HUB_PYTHON, HUB_SCRIPT, ROOT
from .runtime import require_external
from .media import ffprobe, run_checked
from .util import file_sha256


def validate_downloaded_video(video):
    """A fast-start MP4 may advertise its full duration before download finishes."""
    try:
        payload = ffprobe(video)
        length = float(payload.get('format', {}).get('duration') or 0)
        if length <= 0 or not any(s.get('codec_type') == 'video' for s in payload.get('streams', [])):
            raise ValueError('缺少视频轨道或有效时长')
        frame = run_checked(['ffmpeg', '-v', 'error', '-ss', f'{max(0, length - .2):.3f}',
                             '-i', str(video), '-map', '0:v:0', '-frames:v', '1', '-f', 'framemd5', '-'], timeout=30)
        if not any(line.strip() and not line.startswith('#') for line in frame.stdout.splitlines()):
            raise ValueError('片尾尚未下载完整')
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
        temporary = destination.with_suffix('.part')
        try:
            subprocess.run(['/bin/cp', '-c', '-p', str(video), str(temporary)], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            shutil.copy2(video, temporary)
        if damaged:
            quarantine = target / 'quarantine'
            quarantine.mkdir(exist_ok=True)
            destination.replace(quarantine / f'{video.stem}-{uuid.uuid4().hex}.mp4')
        temporary.replace(destination)
        copied.append(video.name)
    return copied


def main():
    require_external('物料下载与同步')
    drama_id = sys.argv[1]
    if not re.fullmatch(r'\d{6}', drama_id) or not 101400 <= int(drama_id) <= 999999:
        raise ValueError('无效剧 ID')
    project = ROOT / 'projects' / drama_id
    if not (project / 'project.json').is_file():
        raise ValueError('请先创建项目')
    # Keep Hub's deduplication and source fallback logic, and its real partial status.
    source = Path.home() / 'Downloads/auto-cut/projects' / f'#{drama_id}#' / 'source/videos'
    with (project / 'download.log').open('ab') as log:
        code = run_download([str(HUB_PYTHON), str(HUB_SCRIPT), 'sync', '--drama-id', drama_id,
                             '--restore-missing', '--only', 'videos'], source, log)
    copied = mirror_videos(project, source)
    print(json.dumps({'mirrored': copied, 'local_video_count': len(list((project / 'source/videos').glob('EP*.mp4'))),
                      'hub_exit_code': code}, ensure_ascii=False), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
