"""Static ending contract, immutable image inputs and deterministic still layout."""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, Field, field_validator, model_validator

from .fonts import font_path, load_font
from .media import ffprobe, run_checked
from .runtime import protect_path
from .util import file_sha256


def thumbnail(path):
    """Small browser preview; the immutable rendering input is never rewritten."""
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((480, 480), Image.Resampling.LANCZOS)
            image = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
            output = io.BytesIO()
            image.save(output, 'WEBP', quality=75, method=4)
            return output.getvalue()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('结尾图片无法生成预览') from exc


class EndingOptions(BaseModel):
    mode: Literal['none', 'last_frame', 'image'] = 'none'
    image: str = ''
    guide: str = Field(default='', max_length=200)
    duration_ms: int = Field(default=3000, ge=1000, le=15000)

    @field_validator('image')
    @classmethod
    def safe_name(cls, value):
        if value and not re.fullmatch(r'[\w.-]{1,80}\.(png|jpg|webp)', value):
            raise ValueError('结尾图片路径无效')
        return value

    @field_validator('guide', mode='before')
    @classmethod
    def clean_guide(cls, value):
        return value.replace('\r', '').strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def required_image(self):
        if self.mode == 'image' and not self.image:
            raise ValueError('请上传结尾图片')
        return self


def row_ending(row):
    return row.get('ending') or (row.get('composition') or {}).get('ending') or {}


def decode_image(raw):
    if not raw or len(raw) > 4_000_000:
        raise ValueError('结尾图片需在 4 MB 以内')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 32_000_000:
                raise ValueError('结尾图片只接受 PNG、JPEG 或 WebP，且不能超过 3200 万像素')
            return ImageOps.exif_transpose(image).convert('RGBA')
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('结尾图片无法解码，请重新上传有效图片') from exc


def checked_path(directory, name):
    EndingOptions(image=name)
    target = directory / name
    protect_path(target)
    if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError('结尾图片不存在或路径无效')
    if target.stat().st_size > 4_000_000:
        raise ValueError('结尾图片需在 4 MB 以内')
    return target


def image_file(project, options):
    target = checked_path(project / 'assets/endings', options.image)
    if not re.fullmatch(r'[a-f0-9]{64}', target.stem) or file_sha256(target) != target.stem:
        raise ValueError('结尾图片校验失败，请重新确认制作')
    return target


def snapshot_images(batch, project, manifest):
    """Bind confirmed requests to content-addressed bytes, independent of editable uploads."""
    for request in manifest.requests:
        options = request.ending
        if options is None or options.mode != 'image':
            continue
        source = checked_path(batch / 'endings', options.image)
        raw = source.read_bytes()
        decode_image(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if re.fullmatch(r'[a-f0-9]{64}', source.stem) and source.stem != digest:
            raise ValueError('结尾图片校验失败')
        destination = project / 'assets/endings'
        protect_path(destination)
        created = []
        parent = destination
        while not parent.exists():
            created.append(parent)
            parent = parent.parent
        destination.mkdir(parents=True, exist_ok=True)
        name = digest + source.suffix
        target = destination / name
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            try: os.link(temporary, target)
            except FileExistsError: pass
        finally:
            temporary.unlink(missing_ok=True)
        # Publish directory entries before a durable manifest can reference them.
        for directory in dict.fromkeys([destination, *(p.parent for p in created)]):
            fd = os.open(directory, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        options.image = name
        image_file(project, options)


def draw_guide(image, text):
    """Fit all text within the lower safe area, preserving user line breaks."""
    if not text:
        return image
    draw = ImageDraw.Draw(image)
    max_width, max_height = int(image.width * .86), int(image.height * .32)
    for size in range(max(10, round(image.width * 64 / 1080)), 5, -1):
        font = load_font(font_path('title_card'), size)
        stroke, spacing = max(1, round(size / 16)), max(2, round(size * .18))
        lines = []
        for paragraph in text.split('\n'):
            line = ''
            # Prefer word wrapping, but split long words/CJK by measured glyph width.
            for word in re.findall(r'\S+\s*', paragraph):
                if line and draw.textlength(line + word.rstrip(), font=font) > max_width - stroke * 2:
                    lines.append(line.rstrip()); line = ''
                for char in word:
                    if line and draw.textlength(line + char, font=font) > max_width - stroke * 2:
                        lines.append(line.rstrip()); line = ''
                    line += char
            lines.append(line.rstrip())
        wrapped = '\n'.join(lines)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing, stroke_width=stroke, align='center')
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            x = (image.width - (box[2] - box[0])) / 2 - box[0]
            y = image.height * .82 - (box[3] - box[1]) - box[1]
            draw.multiline_text((x, y), wrapped, font=font, spacing=spacing, align='center',
                                fill='white', stroke_width=stroke, stroke_fill='black')
            return image
    raise ValueError('结尾引导文案无法完整排版，请减少文字或换行')


def still_image(project, base, options, render, work):
    width, height = int(render['width']), int(render['height'])
    if options.mode == 'image':
        source = decode_image(image_file(project, options).read_bytes())
        fitted = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
        image = Image.new('RGB', (width, height), 'black')
        image.paste(fitted, ((width-fitted.width)//2, (height-fitted.height)//2), fitted)
    else:
        video = next(s for s in ffprobe(base)['streams'] if s['codec_type'] == 'video')
        count = int(video['nb_frames'])
        # Seek close to the tail, then keep the last decoded frame. This avoids
        # rounding a timestamp to the frame immediately before the actual end.
        png = work / 'last-frame.png'
        run_checked(['ffmpeg', '-y', '-v', 'error', '-sseof', '-1', '-i', str(base),
                     '-map', '0:v:0', '-update', '1', str(png)], light=True)
        if count < 1 or not png.exists():
            raise ValueError('无法提取素材最后一帧')
        with Image.open(png) as source:
            image = source.convert('RGB')
    draw_guide(image, options.guide)
    png = work / 'ending.png'
    image.save(png)
    return png
