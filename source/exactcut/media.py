from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import ValidationError


def run_checked(command: list[str], *, capture: bool = True, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, text=True, capture_output=capture, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('媒体处理超时，已停止，可查看日志后重试') from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-4000:]
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command[:5])}\n{detail}")
    return result


def ffprobe(path: Path) -> dict[str, Any]:
    result = run_checked([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], timeout=30)
    return json.loads(result.stdout)


def duration_ms(path: Path) -> int:
    payload = ffprobe(path)
    return round(float(payload.get("format", {}).get("duration") or 0) * 1000)


def resolve_episode_video(project_dir: Path, episode: str) -> Path:
    directory = project_dir / "source" / "videos"
    candidates = [
        directory / f"{episode}.mp4",
        directory / f"{episode.lower()}.mp4",
        directory / f"{episode.upper()}.mp4",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise ValidationError(f"缺少正片：{directory / f'{episode}.mp4'}")


def _shot_bounds(shot: dict[str, Any]) -> tuple[int, int]:
    start = shot.get("start_ms", shot.get("start", shot.get("start_time_ms")))
    end = shot.get("end_ms", shot.get("end", shot.get("end_time_ms")))
    if isinstance(start, float) and start < 10000:
        start = round(start * 1000)
    if isinstance(end, float) and end < 10000:
        end = round(end * 1000)
    return int(start or 0), int(end or 0)


def load_shots(project_dir: Path, episode: str) -> list[dict[str, Any]]:
    path = project_dir / "data" / "episodes" / f"{episode}.video.json"
    if not path.exists():
        raise ValidationError(f"缺少物理镜头索引：{path}；请先运行 exactcut prepare")
    payload = json.loads(path.read_text(encoding="utf-8"))
    shots = payload.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValidationError(f"物理镜头索引为空：{path}")
    normalized = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        start, end = _shot_bounds(shot)
        if end <= start:
            continue
        normalized.append({
            **shot,
            "shot_id": str(shot.get("shot_id") or f"{episode}-S{index + 1:05d}"),
            "start_ms": start,
            "end_ms": end,
        })
    if not normalized:
        raise ValidationError(f"物理镜头索引没有有效区间：{path}")
    return normalized


def resolve_full_episodes(manifest: Any, project_dir: Path):
    """Resolve whole-episode intent against the actual downloaded source duration."""
    from .models import TimelineItem
    if not any(getattr(item, 'full_episode', False) or getattr(item, 'to_end', False) for request in manifest.requests for item in request.timeline):
        return manifest
    resolved = manifest.model_copy(deep=True)
    durations = {}
    for request in resolved.requests:
        for index, item in enumerate(request.timeline):
            if not item.full_episode and not item.to_end:
                continue
            if item.episode not in durations:
                durations[item.episode] = duration_ms(resolve_episode_video(project_dir, item.episode))
            start = 0 if item.full_episode else item.start_ms
            if durations[item.episode] <= start:
                raise ValidationError(f'{item.episode} 实际时长未超过片段起点，不能剪到结尾')
            request.timeline[index] = TimelineItem(type='episode', episode=item.episode, start_ms=start, end_ms=durations[item.episode])
    return resolved


def validate_manifest_sources(manifest: Any, project_dir: Path) -> dict[str, Any]:
    manifest = resolve_full_episodes(manifest, project_dir)
    project_cfg = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    expected = str(project_cfg.get("project_id") or "").strip().strip("#")
    if expected != manifest.drama_id:
        raise ValidationError(f"表格剧ID {manifest.drama_id} 与 project.json {expected} 不一致")
    authorized = {str(item).upper() for item in project_cfg.get("authorized_episodes") or []}
    excluded = {str(item).upper() for item in project_cfg.get("excluded_episodes") or []}
    checked: dict[str, Any] = {}
    for request in manifest.requests:
        for item in request.timeline:
            if item.type != "episode":
                continue
            episode = str(item.episode).upper()
            if authorized and episode not in authorized:
                raise ValidationError(f"{request.request_id} 使用了未授权剧集：{episode}")
            if episode in excluded and episode not in request.notes.upper():
                raise ValidationError(f"{request.request_id} 使用排除剧集 {episode}，备注中没有明确授权")
            source = resolve_episode_video(project_dir, episode)
            if episode in checked:
                length = checked[episode]['duration_ms']
            else:
                payload = ffprobe(source)
                if not any(stream.get('codec_type') == 'video' for stream in payload.get('streams', [])):
                    raise ValidationError(f'{episode} 文件没有可读取的视频轨道，请补齐正确正片')
                length = round(float(payload.get('format', {}).get('duration') or 0) * 1000)
            if length <= 0 or int(item.start_ms or 0) >= length:
                raise ValidationError(f'{request.request_id} {episode} 片段起点超出实际素材时长')
            if int(item.end_ms or 0) > length + 50:
                raise ValidationError(f"{request.request_id} {episode} 时间超过素材时长：{item.end_ms}>{length}")
            checked[episode] = {"source": str(source), "duration_ms": length}
    return checked
