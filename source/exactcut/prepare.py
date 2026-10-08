from __future__ import annotations

import json
from pathlib import Path

from .errors import ValidationError
from .media import duration_ms
from .util import write_json_atomic


def prepare_project(project_dir: Path, *, threshold: float = 27.0, episodes: set[str] | None = None, skip_existing: bool = False, progress=None) -> list[Path]:
    """Create minimal physical-shot indexes. OCR/ASR enrichment may be added upstream."""
    try:
        from scenedetect import AdaptiveDetector, detect
    except ImportError as exc:
        raise ValidationError("prepare 模式需要安装：pip install -e '.[prepare]'") from exc
    videos = sorted((project_dir / "source" / "videos").glob("EP*.mp4"))
    if not videos:
        raise ValidationError("source/videos 下没有 EP*.mp4")
    outputs: list[Path] = []
    target = project_dir / "data" / "episodes"
    target.mkdir(parents=True, exist_ok=True)
    for video in videos:
        if episodes is not None and video.stem not in episodes:
            continue
        output = target / f"{video.stem}.video.json"
        if skip_existing and output.exists():
            continue
        if progress:
            progress(video.stem)
        scenes = detect(str(video), AdaptiveDetector(adaptive_threshold=threshold))
        total = duration_ms(video)
        shots = []
        for index, (start, end) in enumerate(scenes):
            start_ms = round(start.get_seconds() * 1000)
            end_ms = min(total, round(end.get_seconds() * 1000))
            if end_ms > start_ms:
                shots.append({
                    "shot_id": f"{video.stem}-S{index + 1:05d}",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "ocr_text": "",
                    "asr_text": "",
                    "action_tags": [],
                    "emotion_tags": [],
                })
        if not shots:
            shots = [{"shot_id": f"{video.stem}-S00001", "start_ms": 0, "end_ms": total}]
        output = target / f"{video.stem}.video.json"
        write_json_atomic(output, {"episode_id": video.stem, "source_video": str(video.resolve()), "shots": shots})
        outputs.append(output)
    return outputs
