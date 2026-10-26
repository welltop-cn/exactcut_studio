"""Legacy experiment: Gemini paints a new title, then removes the flat key color.

Production uses title_image.extraction; this API is retained for experiment scripts only.

The image script is copied from novel-auto `v13-assets/scripts/generate_gemini_image.mjs`.
Model, retries, and timeout follow that project's `v13-assets/config/runtime-defaults.json`,
except the image model stays the copied script's own default, `gemini-3.1-flash-image`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from ..util import load_config

SCRIPT = Path(__file__).with_name("generate_gemini_image.mjs")
KEY_RGB = (0, 255, 0)


def settings(config: dict | None = None) -> dict:
    config = load_config() if config is None else config
    values = {
        "image_model": "gemini-3.1-flash-image",
        "retries": 5,
        "timeout_ms": 240000,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "key_color": list(KEY_RGB),
    }
    values.update(config.get("title_image") or {})
    return values


def build_prompt(title: str) -> str:
    text = title.strip()
    if not text:
        raise ValueError("剧名是空的")
    return (
        "Create one title lockup and nothing else.\n\n"
        "Exact title text, spelling unchanged. Break it into at most two centered lines:\n"
        f"{text}\n\n"
        "Style: simple stacked drama title, like a classic gold serif title card. "
        "Bold high-contrast serif letters, centered, metallic champagne gold, beveled, with a thin dark-brown edge. "
        "Keep the lockup plain. No script, no swashes, no vines, no flowers, no stars, no sparkles, no ornaments. "
        "Letters, highlights, and shadows may use only gold, cream, white, and dark brown.\n\n"
        "Background: one perfectly flat solid color filling the entire canvas, exactly RGB(0, 255, 0). "
        "Every background pixel is that same color. No gradient, no texture, no noise, no checkerboard, "
        "no vignette, no scene, no people, no props, no frame, no border.\n\n"
        "Do not use green, pink, purple, or magenta in any letter, glow, or shadow. "
        "No colored outer stroke other than thin dark brown.\n"
    )


def _allowed(r: int, g: int, b: int) -> bool:
    if r >= 186 and g >= 170 and b >= 140 and abs(r - g) <= 50:
        return True
    if r >= 140 and g >= 90 and b <= 150 and r + g >= b + 180 and r + 20 >= g:
        return True
    return 25 <= r <= 145 and g < r and b + 5 < g and r >= b + 15 and r + g + b <= 300


def _flat_background(r: int, g: int, b: int, key: tuple[int, int, int]) -> bool:
    kr, kg, kb = key
    distance = ((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2) ** 0.5
    if distance <= 28:
        return True
    return r >= 245 and g >= 245 and b >= 245 and max(r, g, b) - min(r, g, b) <= 12


def cut_title(src: Path, dest: Path, key: tuple[int, int, int] = KEY_RGB) -> Path:
    """Drop the flat key color, an edge-connected white field, and colors outside the title palette."""
    image = Image.open(src).convert("RGB")
    width, height = image.size
    pixels = list(image.get_flattened_data())
    background = bytearray(width * height)
    stack = []
    for y in range(height):
        for x in (0, width - 1):
            index = y * width + x
            r, g, b = pixels[index]
            if _flat_background(r, g, b, key):
                stack.append(index)
    for x in range(width):
        for y in (0, height - 1):
            index = y * width + x
            r, g, b = pixels[index]
            if _flat_background(r, g, b, key):
                stack.append(index)
    while stack:
        index = stack.pop()
        if background[index]:
            continue
        r, g, b = pixels[index]
        if not _flat_background(r, g, b, key):
            continue
        background[index] = 1
        y, x = divmod(index, width)
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                stack.append(ny * width + nx)
    kr, kg, kb = key
    plate = []
    for index, (r, g, b) in enumerate(pixels):
        distance = ((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2) ** 0.5
        if background[index] or not _allowed(r, g, b):
            plate.append((0, 0, 0, 0))
            continue
        alpha = 255 if distance >= 60 else round((distance - 18) / 42 * 255)
        plate.append((r, g, b, alpha))
    out = Image.new("RGBA", image.size)
    out.putdata(plate)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest)
    return dest


def _saved_image(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("saved image: "):
            return Path(line.split(": ", 1)[1].strip())
    raise RuntimeError("生图脚本没有返回图片路径")


def generate_title_image(title: str, dest: Path, *, config: dict | None = None) -> Path:
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError("缺少 GEMINI_API_KEY")
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("缺少 node，无法运行生图脚本")
    options = settings(config)
    key = tuple(int(channel) for channel in options["key_color"])
    timeout = int(options["timeout_ms"]) * int(options["retries"]) / 1000 + 30
    with tempfile.TemporaryDirectory(prefix="title-image-") as folder:
        folder = Path(folder)
        prompt = folder / "prompt.txt"
        prompt.write_text(build_prompt(title), encoding="utf-8")
        raw = folder / "raw.png"
        completed = subprocess.run(
            [
                node, str(SCRIPT),
                "--prompt-file", str(prompt),
                "--model", str(options["image_model"]),
                "--aspect-ratio", str(options["aspect_ratio"]),
                "--image-size", str(options["image_size"]),
                "--retries", str(options["retries"]),
                "--timeout-ms", str(options["timeout_ms"]),
                "--output", str(raw),
            ],
            cwd=folder,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or "生图失败")
        return cut_title(_saved_image(completed.stdout), Path(dest), key)
