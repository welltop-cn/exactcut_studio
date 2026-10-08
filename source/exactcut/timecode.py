from __future__ import annotations

import re
import math

from .errors import ValidationError


UNIT_RE = re.compile(r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?(?:(?P<s>\d+(?:\.\d+)?)s)?$", re.I)


def parse_timepoint(value: str) -> int:
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        raise ValidationError("时间码为空")
    if ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise ValidationError(f"时间码格式错误：{value}")
        try:
            numbers = [float(part) for part in parts]
        except ValueError as exc:
            raise ValidationError(f"时间码格式错误：{value}") from exc
        if any(not math.isfinite(n) or n < 0 for n in numbers) or numbers[-1] >= 60 or (len(numbers) == 3 and numbers[-2] >= 60):
            raise ValidationError(f'时间码数值错误：{value}，秒位须为 0～59')
        if any(not part.isdigit() for part in parts[:-1]):
            raise ValidationError(f'时间码格式错误：{value}')
        seconds = numbers[-1] + numbers[-2] * 60
        if len(numbers) == 3:
            seconds += numbers[0] * 3600
        return round(seconds * 1000)
    if text.replace(".", "", 1).isdigit():
        return round(float(text) * 1000)
    match = UNIT_RE.fullmatch(text)
    if not match or not any(match.groupdict().values()):
        raise ValidationError(f"时间码格式错误：{value}")
    seconds = float(match.group("h") or 0) * 3600 + float(match.group("m") or 0) * 60 + float(match.group("s") or 0)
    return round(seconds * 1000)


def parse_split_timepoint(value: str) -> int:
    """Explicit spreadsheet 分:秒:毫秒 notation, never hours or frame numbers."""
    match = re.fullmatch(r'(\d+):([0-5]\d):(\d{1,3})', str(value).strip())
    if not match:
        raise ValidationError(f'分:秒:毫秒格式错误：{value}，例如 01:22:008')
    minutes, seconds, millis = map(int, match.groups())
    return minutes * 60000 + seconds * 1000 + millis


def parse_range(value: str) -> tuple[int, int]:
    text = str(value).strip().replace("—", "-").replace("–", "-")
    parts = re.split(r"\s*-\s*", text, maxsplit=1)
    if len(parts) != 2:
        raise ValidationError(f"片段缺少起止时间：{value}")
    start_ms, end_ms = parse_timepoint(parts[0]), parse_timepoint(parts[1])
    if end_ms <= start_ms:
        raise ValidationError(f"片段结束时间必须大于开始时间：{value}")
    return start_ms, end_ms


def format_ms(value: int) -> str:
    total = value / 1000
    minutes = int(total // 60)
    seconds = total - minutes * 60
    return f"{minutes:02d}:{seconds:06.3f}"
