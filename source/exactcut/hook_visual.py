"""Content- and first-frame-aware Hook typography routing.

The router is deliberately deterministic and local: it reuses story metadata
already present in the clip plan, samples the rendered first frame with ffmpeg,
and chooses one of six controlled visual styles.  It never changes Hook copy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat
from .hook_emphasis import manual_emphasis
from .fonts import font_path as bundled_font_path, load_font


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).with_name("hook_visual_styles.json")
HOOK_LAYOUT_VERSION = 8
HOOK_MAX_LINES = 3
HOOK_WIDTH_RATIO = 0.86
HOOK_HEIGHT_RATIO = 0.18
AUTO_STYLE_IDS = {
    "neon_reversal",
    "danger_warning",
    "ceo_luxury",
    "emotional_diary",
    "cold_mystery",
    "raw_conflict",
}

_STYLE_PALETTES: dict[str, dict[str, dict[str, str]]] = {
    "neon_reversal": {
        "warm": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FF42A6", "secondary": "#844EFF", "stroke": "#5B32CB"},
        "cool": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FF557A", "secondary": "#D34CFF", "stroke": "#6230A8"},
        "neutral": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FF42A6", "secondary": "#844EFF", "stroke": "#5B32CB"},
    },
    "danger_warning": {
        "warm": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FFE600", "secondary": "#FF3B30", "stroke": "#050505"},
        "cool": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FF2727", "secondary": "#FF7A00", "stroke": "#050505"},
        "neutral": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FF2727", "secondary": "#FF7A00", "stroke": "#050505"},
    },
    "ceo_luxury": {
        "warm": {"base_l1": "#FFF8E8", "base_l2": "#FFF8E8", "accent": "#FFE08A", "secondary": "#C58A42", "stroke": "#302116"},
        "cool": {"base_l1": "#FFF9ED", "base_l2": "#FFF9ED", "accent": "#F4C15D", "secondary": "#D99B3D", "stroke": "#211B17"},
        "neutral": {"base_l1": "#FFF9ED", "base_l2": "#FFF9ED", "accent": "#F4C15D", "secondary": "#D99B3D", "stroke": "#211B17"},
    },
    "emotional_diary": {
        "warm": {"base_l1": "#FFF0DA", "base_l2": "#FF8F9B", "accent": "#FF8F9B", "secondary": "#FFC5AF", "stroke": "#4B302D"},
        "cool": {"base_l1": "#FFF4E7", "base_l2": "#FF9B7D", "accent": "#FF9B7D", "secondary": "#EFA7C8", "stroke": "#3C2D3E"},
        "neutral": {"base_l1": "#FFF0DA", "base_l2": "#EF8FA8", "accent": "#EF8FA8", "secondary": "#FFC5AF", "stroke": "#423036"},
    },
    "cold_mystery": {
        "warm": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#4EF2FF", "secondary": "#3579FF", "stroke": "#123A75"},
        "cool": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#D76CFF", "secondary": "#5B65FF", "stroke": "#282867"},
        "neutral": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#4EF2FF", "secondary": "#3579FF", "stroke": "#123A75"},
    },
    "raw_conflict": {
        "warm": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FF332E", "secondary": "#FFE600", "stroke": "#050505"},
        "cool": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FFE600", "secondary": "#FF3B30", "stroke": "#050505"},
        "neutral": {"base_l1": "#FFFFFF", "base_l2": "#FFFFFF", "accent": "#FFE600", "secondary": "#FF3B30", "stroke": "#050505"},
    },
}


def _load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("enabled_by_default") is not True:
        raise RuntimeError("Hook视觉自动路由不得默认关闭")
    styles = payload.get("styles") or {}
    if set(styles) != AUTO_STYLE_IDS:
        raise RuntimeError("Hook视觉风格配置不完整")
    return payload


def _hex_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    clean = value.lstrip("#")
    if len(clean) != 6:
        raise ValueError(f"invalid color: {value}")
    return tuple(int(clean[i : i + 2], 16) for i in (0, 2, 4)) + (alpha,)


def analyze_frame_image(image: Image.Image) -> dict[str, Any]:
    """Measure the top Hook zone rather than the whole first frame."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    crop = rgb.crop((0, 0, width, max(1, int(height * 0.36)))).resize((96, 96))
    mean_r, mean_g, mean_b = ImageStat.Stat(crop).mean
    gray = crop.convert("L")
    luma = ImageStat.Stat(gray).mean[0] / 255.0
    contrast = ImageStat.Stat(gray).stddev[0] / 255.0
    hsv = crop.convert("HSV")
    saturation = ImageStat.Stat(hsv.getchannel("S")).mean[0] / 255.0
    hue_bins = [0] * 12
    pixels = hsv.get_flattened_data() if hasattr(hsv, "get_flattened_data") else hsv.getdata()
    for hue, sat, value in pixels:
        if sat >= 36 and value >= 28:
            hue_bins[min(11, int(hue / 256 * 12))] += 1
    dominant_bin = max(range(12), key=hue_bins.__getitem__) if any(hue_bins) else -1
    hue_names = [
        "red", "orange", "yellow", "green", "green", "cyan",
        "cyan", "blue", "blue", "purple", "magenta", "red",
    ]
    dominant_hue = hue_names[dominant_bin] if dominant_bin >= 0 else "neutral"
    if dominant_hue in {"red", "orange", "yellow"} or mean_r - mean_b >= 16:
        temperature = "warm"
    elif dominant_hue in {"cyan", "blue", "purple"} or mean_b - mean_r >= 16:
        temperature = "cool"
    else:
        temperature = "neutral"
    scrim_alpha = 112 if luma >= 0.62 else 82 if contrast >= 0.24 else 56
    stroke_width = 5 if luma >= 0.68 or contrast >= 0.28 else 4
    return {
        "mean_rgb": [round(mean_r), round(mean_g), round(mean_b)],
        "luminance": round(luma, 4),
        "contrast": round(contrast, 4),
        "saturation": round(saturation, 4),
        "dominant_hue": dominant_hue,
        "temperature": temperature,
        "scrim_alpha": scrim_alpha,
        "stroke_width": stroke_width,
    }


def extract_first_frame_profile(video_path: Path, temp_dir: Path) -> dict[str, Any]:
    from .media import bounded_ffmpeg
    frame_path = temp_dir / "hook_visual_first_frame.jpg"
    subprocess.run(
        bounded_ffmpeg([
            "ffmpeg", "-y", "-v", "error", "-ss", "0.050", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale=270:480", "-q:v", "2", str(frame_path),
        ]),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    with Image.open(frame_path) as image:
        return analyze_frame_image(image)


def _story_corpora(plan: dict[str, Any]) -> tuple[str, str]:
    hook_parts = [
        str(plan.get("hook_text") or ""),
        str(plan.get("angle") or ""),
        str(plan.get("mood") or ""),
    ]
    story_parts = [
        str(plan.get("variant_name") or ""),
        str(plan.get("narrative_arc") or ""),
        str(plan.get("core_question") or ""),
    ]
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        story_parts.extend(str(item) for item in (segment.get("event_tags") or []))
        story_parts.extend(str(item) for item in (segment.get("emotion_tags") or []))
        story_parts.extend([
            str(segment.get("narrative_intent") or ""),
            str(segment.get("script_text") or ""),
            str(segment.get("subtitle_text") or ""),
        ])
    return " ".join(hook_parts).casefold(), " ".join(story_parts).casefold()


def _story_scores(plan: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, float], dict[str, list[str]]]:
    hook_corpus, story_corpus = _story_corpora(plan)
    scores = {style_id: 0.0 for style_id in AUTO_STYLE_IDS}
    signals = {style_id: [] for style_id in AUTO_STYLE_IDS}
    for style_id, spec in config["styles"].items():
        for raw_keyword in spec.get("keywords") or []:
            keyword = str(raw_keyword).casefold()
            if keyword and keyword in hook_corpus:
                scores[style_id] += 2.0
                signals[style_id].append(keyword)
            elif keyword and keyword in story_corpus:
                scores[style_id] += 0.75
                signals[style_id].append(keyword)
    angle = str(plan.get("angle") or "").casefold()
    mood = str(plan.get("mood") or "").casefold()
    for style_id, weight in (config.get("angle_bias", {}).get(angle) or {}).items():
        scores[style_id] += float(weight)
        signals[style_id].append(f"angle:{angle}")
    for style_id, weight in (config.get("mood_bias", {}).get(mood) or {}).items():
        scores[style_id] += float(weight)
        signals[style_id].append(f"mood:{mood}")
    return scores, signals


def _accent_phrase(hook_text: str, style_id: str, signals: list[str]) -> str:
    marked = re.search(r"\*\*(.+?)\*\*", hook_text or "")
    if marked:
        return marked.group(1).strip()
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", hook_text or "")
    preferred = [signal for signal in signals if not signal.startswith(("angle:", "mood:"))]
    for keyword in sorted(set(preferred), key=len, reverse=True):
        match = re.search(re.escape(keyword), clean, flags=re.IGNORECASE)
        if match:
            return clean[match.start() : match.end()]
    if style_id in {"ceo_luxury", "raw_conflict", "danger_warning"}:
        last_line = next((line.strip() for line in reversed(clean.splitlines()) if line.strip()), clean)
        words = last_line.split()
        return " ".join(words[-2:]) if words else ""
    return ""


def choose_hook_visual(
    plan: dict[str, Any],
    frame_profile: dict[str, Any],
    *,
    forced_style: str | None = None,
    config: dict[str, Any] | None = None,
    emphasis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = config or _load_config()
    scores, signals = _story_scores(plan, payload)
    if emphasis is not None:
        style_id = forced_style or emphasis.get('style') or 'default'
        if style_id not in AUTO_STYLE_IDS | {'default'}:
            raise ValueError('unsupported semantic Hook style')
        selection_source = emphasis['source']
        scores = {style: 0.0 for style in AUTO_STYLE_IDS}
        signals = {style: [] for style in AUTO_STYLE_IDS}
    elif forced_style:
        if forced_style not in AUTO_STYLE_IDS | {"default"}:
            raise ValueError(f"unsupported personalized Hook style: {forced_style}")
        style_id = forced_style
        selection_source = "explicit_style"
    else:
        ranked = sorted(scores, key=lambda item: (-scores[item], item))
        if scores[ranked[0]] <= 0:
            style_id = str(payload.get("fallback_style") or "default")
            selection_source = "deterministic_fallback"
        else:
            top_score = scores[ranked[0]]
            margin = float(payload.get("near_tie_score_margin", 0.75))
            eligible = [item for item in ranked if top_score - scores[item] <= margin]
            identity = "|".join([
                str(plan.get("variant_id") or ""),
                str(plan.get("hook_text") or ""),
                str(plan.get("angle") or ""),
            ])
            pick = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16)
            style_id = eligible[pick % len(eligible)]
            selection_source = "story_score"
    temperature = str(frame_profile.get("temperature") or "neutral")
    if style_id == "default":
        palette = {
            "base_l1": "#FFFFFF",
            "base_l2": "#FFFFFF",
            "accent": "#FFA500",
            "secondary": "#FFD700",
            "stroke": "#000000",
        }
        label = "默认清晰"
        matched: list[str] = []
        font_path = bundled_font_path('default')
    else:
        palette = dict(
            _STYLE_PALETTES[style_id].get(temperature)
            or _STYLE_PALETTES[style_id]["neutral"]
        )
        label = payload["styles"][style_id]["label_zh"]
        matched = sorted(set(signals[style_id]))
        font_path = bundled_font_path(style_id)
    hook_text = str(plan.get("hook_text") or "")
    reason_bits = [f"剧情匹配 {', '.join(matched[:4])}" if matched else "剧情信号较弱"]
    reason_bits.append(
        f"首帧顶部为{temperature}/{frame_profile.get('dominant_hue', 'neutral')}，"
        f"亮度 {float(frame_profile.get('luminance', 0.0)):.2f}"
    )
    accents = emphasis or manual_emphasis(hook_text)
    if accents is not None:
        reason_bits[0] = accents['reason']
    return {
        "version": 1,
        "mode": "semantic_frame_auto" if accents is not None and accents['source'] == 'ai_semantic' else "story_frame_auto",
        "style_id": style_id,
        "style_label_zh": label,
        "selection_source": selection_source,
        "story_scores": {key: round(value, 3) for key, value in sorted(scores.items())},
        "matched_signals": matched,
        "frame_profile": frame_profile,
        "palette": palette,
        "accent_phrase": (next((span['phrase'] for span in accents['highlights']), '') if accents is not None
                          else _accent_phrase(hook_text, style_id, matched)),
        "emphasis": accents,
        "font_path": font_path,
        "reason_zh": "；".join(reason_bits),
    }


def resolve_hook_visual(
    plan: dict[str, Any],
    video_path: Path,
    temp_dir: Path,
    *,
    forced_style: str | None = None,
    emphasis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return choose_hook_visual(
        plan,
        extract_first_frame_profile(video_path, temp_dir),
        forced_style=forced_style,
        emphasis=emphasis,
    )


def _font(path: str, size: int):
    return load_font(path, size)


def _wrap_lines(
    hook_text: str,
    font_path: str,
    video_w: int,
    video_h: int = 1920,
) -> tuple[Any, int, list[str]]:
    unmarked = re.sub(r"\*\*(.+?)\*\*", r"\1", hook_text or "", flags=re.S)
    clean_lines = [
        line.strip()
        for line in unmarked.splitlines()
        if line.strip()
    ]
    clean = " ".join(clean_lines)
    probe = ImageDraw.Draw(Image.new("RGBA", (video_w, 10)))
    # Leave room for italic overhang, stroke, shadow and glow inside the safe zone.
    effect_margin = 16
    max_width = int(video_w * HOOK_WIDTH_RATIO) - effect_margin * 2
    max_height = int(video_h * HOOK_HEIGHT_RATIO)

    def wrap(text: str, font: Any) -> list[str]:
        words, result, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if probe.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                if current:
                    result.append(current)
                current = word
        if current:
            result.append(current)
        return result

    def fits(lines: list[str], font: Any, size: int, max_lines: int) -> bool:
        if len(lines) > max_lines:
            return False
        gap = max(10, int(size * 0.22))
        for index, line in enumerate(lines):
            left, top, right, bottom = probe.textbbox((0, 0), line, font=font)
            advance = probe.textlength(line, font=font)
            if max(advance, right - left) > max_width:
                return False
            if index * (size + gap) + bottom + effect_margin > max_height:
                return False
        return True

    normal_sizes = sorted({max(1, round(size * video_w / 1080)) for size in range(78, 47, -2)}, reverse=True)
    compact_sizes = sorted({max(1, round(size * video_w / 1080)) for size in range(46, 31, -2)}, reverse=True)
    # Prefer two readable lines; allow three before using the compact font range.
    for max_lines, preserve_breaks, sizes in (
        (2, True, normal_sizes), (2, False, normal_sizes),
        (3, True, normal_sizes), (3, False, normal_sizes),
        (3, False, compact_sizes),
    ):
        if preserve_breaks and len(clean_lines) > max_lines:
            continue
        if max_lines == 2 and len(clean_lines) == 3:
            # An explicit three-line Hook is already an accepted author layout.
            continue
        for size in sizes:
            font = _font(font_path, size)
            candidate = ([part for line in clean_lines for part in wrap(line, font)]
                         if preserve_breaks else wrap(clean, font))
            if fits(candidate, font, size, max_lines):
                return font, size, candidate
    from .errors import ValidationError
    raise ValidationError("Hook 文案过长，无法在最多三行的安全区域内完整显示；请缩短 Excel 文案或调整换行。")


@lru_cache(maxsize=512)
def validate_hook_layout(hook_text: str, video_w: int = 1080, video_h: int = 1920) -> None:
    if not hook_text.strip():
        return
    # Frame-dependent auto routing can select any supported font at render time.
    for style in sorted(AUTO_STYLE_IDS | {'default'}):
        decision = choose_hook_visual({'hook_text': hook_text}, {}, forced_style=style)
        _wrap_lines(hook_text, decision['font_path'], video_w, video_h)


def _accent_split(line: str, phrase: str) -> tuple[str, str, str] | None:
    if not phrase:
        return None
    match = re.search(re.escape(phrase.strip()), line, flags=re.IGNORECASE)
    if not match:
        return None
    return line[: match.start()], line[match.start() : match.end()], line[match.end() :]


def _accent_runs(line: str, line_start: int, spans: list[dict]) -> list[tuple[str, bool]]:
    """Intersect source spans with this line, including phrases that wrap."""
    boundaries = {0, len(line)}
    for span in spans:
        boundaries.add(max(0, min(len(line), span['start'] - line_start)))
        boundaries.add(max(0, min(len(line), span['end'] - line_start)))
    points = sorted(boundaries)
    return [(line[start:end], any(span['start'] <= line_start + start < span['end'] for span in spans))
            for start, end in zip(points, points[1:]) if end > start]


def render_personalized_hook_png(
    hook_text: str,
    temp_dir: Path,
    decision: dict[str, Any],
    *,
    video_w: int = 1080,
    video_h: int = 1920,
) -> Path:
    style_id = str(decision["style_id"])
    palette = decision["palette"]
    font, size, lines = _wrap_lines(hook_text, str(decision["font_path"]), video_w, video_h)
    decision["layout"] = {"font_size": size, "lines": lines, "max_lines": HOOK_MAX_LINES,
                          "version": HOOK_LAYOUT_VERSION, "max_width_ratio": HOOK_WIDTH_RATIO,
                          "max_height_ratio": HOOK_HEIGHT_RATIO}
    output = temp_dir / f"hook_visual_{style_id}.png"
    if not lines:
        return output
    canvas = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    line_gap = max(10, int(size * 0.22))
    block_h = len(lines) * size + (len(lines) - 1) * line_gap
    y_start = int(video_h * (decision.get('placement') or {}).get('y_ratio', .07))
    scrim_alpha = int((decision.get("frame_profile") or {}).get("scrim_alpha", 64))
    scrim_top = max(0, y_start - int(size * 0.70))
    scrim_bottom = min(video_h, y_start + int(video_h * HOOK_HEIGHT_RATIO), y_start + block_h + int(size * 0.75))
    scrim = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    height = max(1, scrim_bottom - scrim_top)
    for y in range(scrim_top, scrim_bottom):
        phase = (y - scrim_top) / height
        alpha = int(scrim_alpha * math.sin(math.pi * phase))
        if alpha:
            scrim_draw.line((0, y, video_w, y), fill=(0, 0, 0, alpha))
    # Keep the text on a separate layer to verify the actual decorated pixels.

    probe = ImageDraw.Draw(Image.new("RGBA", (video_w, 10)))
    accent_phrase = str(decision.get("accent_phrase") or "")
    emphasis = decision.get('emphasis')
    spans = emphasis['highlights'] if emphasis is not None else None
    if emphasis is not None and emphasis['text'] != ' '.join(lines):
        from .errors import ValidationError
        raise ValidationError('Hook 强调结果与当前文案不一致，请重新制作')
    stroke_width = max(1, round(size / 60))
    if style_id in {"danger_warning", "raw_conflict"}:
        stroke_width += 1
    elif style_id in {"neon_reversal", "cold_mystery", "ceo_luxury"}:
        stroke_width = max(2, stroke_width)
    base_l1 = _hex_rgba(palette["base_l1"])
    base_l2 = _hex_rgba(palette["base_l2"])
    accent = _hex_rgba(palette["accent"])
    secondary = _hex_rgba(palette["secondary"])
    stroke = _hex_rgba(palette["stroke"], 245)
    glow_radius = 4 if style_id == "neon_reversal" else 2 if style_id in {"cold_mystery", "ceo_luxury"} else 0
    y = y_start
    line_start = 0
    for index, line in enumerate(lines):
        line_base = base_l2 if index else base_l1
        split = _accent_split(line, accent_phrase)
        if spans is not None:
            if style_id in {'neon_reversal', 'emotional_diary'}:
                line_base = base_l1
            outline = secondary if style_id == 'neon_reversal' and index == 0 else stroke
            parts = [(text, accent if highlighted else line_base, outline)
                     for text, highlighted in _accent_runs(line, line_start, spans)]
        elif style_id == "neon_reversal":
            parts = [(line, (255, 255, 255, 255), secondary if index == 0 else accent)]
        elif style_id == "emotional_diary":
            parts = [(line, line_base, stroke)]
        elif split:
            before, highlighted, after = split
            parts = []
            if before:
                parts.append((before, line_base, stroke))
            parts.append((highlighted, accent, stroke))
            if after:
                parts.append((after, line_base, stroke))
        else:
            parts = [(line, line_base, stroke)]
        total_w = sum(int(probe.textlength(text, font=font)) for text, _, _ in parts)
        x = (video_w - total_w) // 2
        if glow_radius:
            glow = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow)
            gx = x
            for text, fill, outline in parts:
                glow_color = outline if style_id == "neon_reversal" else fill
                glow_draw.text(
                    (gx, y), text, font=font, fill=glow_color,
                    stroke_width=stroke_width + 1, stroke_fill=glow_color,
                )
                gx += int(probe.textlength(text, font=font))
            canvas = Image.alpha_composite(
                canvas, glow.filter(ImageFilter.GaussianBlur(glow_radius))
            )
        draw = ImageDraw.Draw(canvas)
        px = x
        for text, fill, outline in parts:
            draw.text(
                (px + 1, y + 2), text, font=font, fill=(0, 0, 0, 155),
                stroke_width=stroke_width, stroke_fill=(0, 0, 0, 155),
            )
            draw.text(
                (px, y), text, font=font, fill=fill,
                stroke_width=stroke_width, stroke_fill=outline,
            )
            px += int(probe.textlength(text, font=font))
        y += size + line_gap
        line_start += len(line) + 1
    bbox = canvas.getbbox()
    horizontal_margin = int(video_w * (1 - HOOK_WIDTH_RATIO) / 2)
    if bbox and (bbox[0] < horizontal_margin or bbox[2] > video_w - horizontal_margin
                 or bbox[1] < 0 or bbox[3] > y_start + int(video_h * HOOK_HEIGHT_RATIO)
                 or bbox[3] - bbox[1] > int(video_h * HOOK_HEIGHT_RATIO)):
        from .errors import ValidationError
        raise ValidationError("Hook 排版超出安全区域，请调整文案后重新制作。")
    decision["layout"]["text_bbox"] = list(bbox) if bbox else None
    Image.alpha_composite(scrim, canvas).save(output, "PNG")
    return output
