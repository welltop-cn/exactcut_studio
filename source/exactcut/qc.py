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


def _sample_title_card(timeline, sample_ms=100):
    elapsed = 0
    for item in timeline or []:
        duration = (int(item['end_ms']) - int(item['start_ms']) if item['type'] == 'episode'
                    else int(item['duration_ms']))
        if elapsed <= sample_ms < elapsed + duration:
            return item if item['type'] == 'title_card' else None
        elapsed += duration
    return None


def inspect_output(video: Path, expected_duration_ms: int, config: dict[str, Any],
                   timeline: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
    card = _sample_title_card(timeline)
    card_coverage = None
    with tempfile.TemporaryDirectory(prefix="exactcut-qc-") as temporary:
        frame = Path(temporary) / "first.jpg"
        run_checked(["ffmpeg", "-y", "-v", "error", "-ss", "0.10", "-i", str(video), "-frames:v", "1", str(frame)], light=True)
        if not frame.exists():
            errors.append("cannot extract first frame")
            mean_luma = 0.0
        else:
            with Image.open(frame) as image:
                gray = image.convert("L")
            mean_luma = float(ImageStat.Stat(gray).mean[0])
            if card is not None:
                from .renderer import title_card_image
                # Check the expected white glyphs, not a Hook or bright pixels elsewhere.
                # Compression can soften edges; use glyph interiors and require 80% visible.
                reference = title_card_image(card['text'], int(render['width']), int(render['height']))
                mask = reference.convert('L').point(lambda value: 255 if value >= 192 else 0)
                if gray.size == mask.size and mask.getbbox():
                    visible = gray.point(lambda value: 255 if value >= 32 else 0)
                    card_coverage = ImageStat.Stat(visible, mask=mask).mean[0] / 255
                else:
                    card_coverage = 0.0
                if card_coverage < 0.8:
                    errors.append('title card text is missing or unreadable')
            if mean_luma < 2.0 and not (card_coverage is not None and card_coverage >= 0.8):
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
        "title_card_text_coverage": card_coverage,
    }
