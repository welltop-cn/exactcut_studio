"""Project-level drama title and logo burned onto the plot, never the ending card."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .errors import ValidationError
from .fonts import font_path, load_font

LOGOS = {'reelai': 'Reel.AI', 'loreker': 'Loreker'}
ASSETS = Path(__file__).parent / 'assets' / 'brand'


class BrandOverlay(BaseModel):
    title_enabled: bool = False
    logo_enabled: bool = False
    logo: Literal['', 'reelai', 'loreker'] = ''
    title: str = Field(default='', max_length=200)
    title_mode: Literal['text', 'cover'] = 'text'
    title_asset: str = ''
    title_drama_id: str = ''


def parse(draft: dict) -> tuple[BrandOverlay, list[str]]:
    raw = draft.get('brand_overlay') or {}
    if not isinstance(raw, dict):
        return BrandOverlay(), ['画面标识设置无效']
    title_on = bool(raw.get('title_enabled'))
    logo_on = bool(raw.get('logo_enabled'))
    logo = raw.get('logo') or ''
    if logo not in LOGOS:
        logo = ''
    title = str(draft.get('drama_title') or '').replace('\r', '').strip()
    errors = []
    mode = raw.get('title_mode', 'cover')
    asset = raw.get('title_asset', '')
    drama_id = str(draft.get('drama_id') or '').strip().strip('#')
    if mode not in {'text', 'cover'}:
        errors.append('剧名图片模式无效')
        mode = 'cover'
    if title_on and mode == 'cover' and asset:
        from .title_image.extraction import asset_file, AssetUnavailable
        try:
            asset_file(asset, drama_id)
        except AssetUnavailable:
            asset = ''
        except ValueError as exc:
            errors.append(str(exc))
    if title_on and not title:
        errors.append('勾选视频水印「剧名」后需要填写剧名')
    if logo_on and logo not in LOGOS:
        errors.append('勾选视频水印「LOGO」后需要选择 Reel.AI 或 Loreker')
    if len(title) > 200:
        errors.append('剧名不能超过 200 字')
        title = ''
        title_on = False
    return BrandOverlay(
        title_enabled=title_on,
        logo_enabled=logo_on,
        logo=logo if logo in LOGOS else '',
        title=title if title_on else '',
        title_mode=mode if title_on else 'text',
        title_asset=asset if title_on and mode == 'cover' and isinstance(asset, str) else '',
        title_drama_id=drama_id if title_on and mode == 'cover' else '',
    ), errors


def active(brand: BrandOverlay | None) -> bool:
    return bool(brand and (brand.title_enabled or brand.logo_enabled))


def _scale(width: int) -> float:
    return width / 1080


def _logo_image(logo: str, height: int, max_width: int) -> Image.Image:
    path = ASSETS / f'{logo}.png'
    if not path.is_file():
        raise ValidationError('logo 原图缺失，未生成成片')
    with Image.open(path) as source:
        image = source.convert('RGBA')
    width = max(1, round(image.width * height / image.height))
    if width > max_width:
        width = max_width
        height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _width(font, text: str, stroke: int) -> int:
    box = font.getbbox(text, stroke_width=stroke)
    return box[2] - box[0]


def _wrap_paragraph(text: str, font, max_width: int, stroke: int) -> list[str]:
    if _width(font, text, stroke) <= max_width:
        return [text]
    if ' ' in text:
        lines, current = [], ''
        for word in text.split(' '):
            trial = word if not current else f'{current} {word}'
            if current and _width(font, trial, stroke) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines
    lines, current = [], ''
    for char in text:
        trial = current + char
        if current and _width(font, trial, stroke) > max_width:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _title_lines(text: str, max_width: int, scale: float) -> tuple[object, list[str], int]:
    stroke = max(1, round(2 * scale))
    size = max(stroke + 1, round(48 * scale))
    minimum = max(stroke + 1, round(22 * scale))
    font = load_font(font_path('title_card'), size)
    lines: list[str] = []
    while size >= minimum:
        font = load_font(font_path('title_card'), size)
        lines = []
        for paragraph in text.split('\n'):
            lines.extend(_wrap_paragraph(paragraph, font, max_width, stroke) or [''])
        if len(lines) <= 2 and all(_width(font, line, stroke) <= max_width for line in lines):
            return font, lines[:2], stroke
        size -= max(1, round(2 * scale))
    font = load_font(font_path('title_card'), minimum)
    lines = []
    for paragraph in text.split('\n'):
        lines.extend(_wrap_paragraph(paragraph, font, max_width, stroke))
    if len(lines) > 2:
        lines = lines[:2]
        while lines[1] and _width(font, lines[1] + '…', stroke) > max_width:
            lines[1] = lines[1][:-1]
        lines[1] += '…'
    lines = [_clip(line, font, max_width, stroke) for line in lines]
    return font, lines, stroke


def _clip(line: str, font, max_width: int, stroke: int) -> str:
    if _width(font, line, stroke) <= max_width:
        return line
    while line and _width(font, line + '…', stroke) > max_width:
        line = line[:-1]
    return (line + '…') if line else '…'


def render(brand: BrandOverlay, width: int, height: int, dest: Path) -> tuple[Path | None, float]:
    """Full-frame transparent PNG and the fraction below which a top hook must sit."""
    if not active(brand):
        return None, 0
    if brand.title_enabled and not brand.title.strip():
        raise ValidationError('勾选视频水印「剧名」后需要填写剧名')
    if brand.logo_enabled and brand.logo not in LOGOS:
        raise ValidationError('勾选视频水印「LOGO」后需要选择 Reel.AI 或 Loreker')
    scale = _scale(width)
    margin_x = max(8, round(36 * scale))
    margin_top = max(8, round(48 * scale))
    margin_bottom = max(8, round(110 * scale))
    gap = max(8, round(24 * scale))
    canvas = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    title_bottom = 0
    if brand.logo_enabled:
        logo = _logo_image(brand.logo, max(16, round(64 * scale)), max(16, round(width * .46)))
        canvas.alpha_composite(logo, ((width - logo.width) // 2, height - margin_bottom - logo.height))
    if brand.title_enabled and brand.title_mode == 'cover':
        from .title_image.extraction import asset_file
        with Image.open(asset_file(brand.title_asset, brand.title_drama_id)) as original:
            title_image = original.convert('RGBA')
            title_image = title_image.crop(title_image.getchannel('A').getbbox())
            factor = min(300 * scale / title_image.width, 180 * scale / title_image.height)
            title_image = title_image.resize((max(1, round(title_image.width * factor)),
                                              max(1, round(title_image.height * factor))), Image.Resampling.LANCZOS)
        x, y = round(54 * scale), round(42 * scale)
        canvas.alpha_composite(title_image, (x, y))
        title_bottom = y + title_image.height
    elif brand.title_enabled:
        font, lines, stroke = _title_lines(brand.title, max(40, round(width * .62)), scale)
        draw = ImageDraw.Draw(canvas)
        line_gap = max(2, round(8 * scale))
        y = margin_top
        for line in lines:
            box = font.getbbox(line, stroke_width=stroke)
            draw.text((margin_x - box[0], y - box[1]), line, font=font, fill=(255, 255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            y += box[3] - box[1] + line_gap
        title_bottom = y - line_gap
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest)
    return dest, ((title_bottom + gap) / height) if title_bottom else 0
