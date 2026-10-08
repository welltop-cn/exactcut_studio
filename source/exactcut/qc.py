from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from .media import ffprobe, run_checked
from .util import file_sha256, now_iso


def _fps(value: str) -> float:
    numerator, _, denominator = value.partition("/")
    return float(numerator) / float(denominator or 1)


def inspect_output(video: Path, expected_duration_ms: int, config: dict[str, Any]) -> dict[str, Any]:
    render = config["render"]
    payload = ffprobe(video)
    streams = payload.get("streams") or []
    vstream = next((item for item in streams if item.get("codec_type") == "video"), None)
    astream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    errors = []
    actual_ms = round(float(payload.get("format", {}).get("duration") or 0) * 1000)
    if not vstream:
        errors.append("missing video stream")
    else:
        if int(vstream.get("width") or 0) != int(render["width"]) or int(vstream.get("height") or 0) != int(render["height"]):
            errors.append(f"resolution={vstream.get('width')}x{vstream.get('height')}")
        actual_fps = _fps(str(vstream.get("avg_frame_rate") or "0/1"))
        if not math.isclose(actual_fps, float(render["fps"]), abs_tol=0.05):
            errors.append(f"fps={actual_fps:.3f}")
    if not astream:
        errors.append("missing audio stream")
    else:
        if int(astream.get("channels") or 0) != int(render["audio_channels"]):
            errors.append(f"audio_channels={astream.get('channels')}")
    if abs(actual_ms - expected_duration_ms) > int(render["duration_tolerance_ms"]):
        errors.append(f"duration={actual_ms} expected={expected_duration_ms}")
    with tempfile.TemporaryDirectory(prefix="exactcut-qc-") as temporary:
        frame = Path(temporary) / "first.jpg"
        run_checked(["ffmpeg", "-y", "-v", "error", "-ss", "0.10", "-i", str(video), "-frames:v", "1", str(frame)])
        if not frame.exists():
            errors.append("cannot extract first frame")
            mean_luma = 0.0
        else:
            mean_luma = float(ImageStat.Stat(Image.open(frame).convert("L")).mean[0])
            if mean_luma < 2.0:
                errors.append("first frame is black")
    return {
        "checked_at": now_iso(),
        "status": "passed" if not errors else "rejected",
        "allowed": not errors,
        "errors": errors,
        "video_sha256": file_sha256(video),
        "actual_duration_ms": actual_ms,
        "expected_duration_ms": expected_duration_ms,
        "first_frame_mean_luma": mean_luma,
    }

