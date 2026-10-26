"""Offline inspection of built-in image editor results; no inference/API calls.

Usage: python report.py /absolute/path/to/manifest.json
The manifest lists id, title, source, result, prompt for each image.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw


def build_report(manifest: Path) -> Path:
    folder = manifest.resolve().parent
    jobs = json.loads(manifest.read_text(encoding="utf-8"))
    seen = set()
    cards = []
    checks = []
    for index, job in enumerate(jobs, 1):
        source, result = Path(job["source"]), Path(job["result"])
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        dest = folder / f"{index:02d}"
        dest.mkdir(exist_ok=True)
        shutil.copy2(source, dest / "cover.png")
        shutil.copy2(result, dest / "title.png")
        (dest / "prompt.txt").write_text(job["prompt"], encoding="utf-8")
        with Image.open(result) as original:
            has_alpha = "A" in original.getbands() or "transparency" in original.info
            image = original.convert("RGBA")
        alpha = image.getchannel("A")
        histogram = alpha.histogram()
        valid = has_alpha and histogram[0] > 0 and sum(histogram[1:]) > 0
        check = {
            "id": job["id"], "source_sha256": digest,
            "size": list(image.size), "has_alpha": has_alpha,
            "alpha_extrema": list(alpha.getextrema()),
            "fully_transparent_fraction": histogram[0] / (image.width * image.height),
            "nonempty_transparent_image": valid,
            "visual_approval": "pending",
        }
        checks.append(check)
        # Inspection previews only. The delivered title.png remains byte-identical.
        preview = image.copy()
        preview.thumbnail((1000, 700))
        sheet = Image.new("RGB", (preview.width, preview.height * 2))
        for row, color in enumerate(((45, 58, 72), (235, 235, 235))):
            bg = Image.new("RGBA", preview.size, (*color, 255))
            bg.alpha_composite(preview)
            sheet.paste(bg.convert("RGB"), (0, row * preview.height))
        sheet.save(dest / "preview.jpg", quality=95)
        escaped = html.escape(job["title"])
        cards.append(f'<section><h2>{escaped}</h2><p>Alpha check: {valid}; visual approval pending</p>'
                     f'<div><img class="cover" src="{index:02d}/cover.png">'
                     f'<a href="{index:02d}/title.png"><img class="result" src="{index:02d}/preview.jpg"></a></div></section>')
    (folder / "checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    page = '<!doctype html><meta charset="utf-8"><title>Title extraction review</title>'
    page += '<style>body{background:#161b22;color:#eee;font:16px sans-serif;margin:30px}section{margin-bottom:40px}section div{display:flex;gap:24px;align-items:flex-start}.cover{width:258px}.result{max-width:900px;width:65vw}a{color:inherit}</style>'
    page += '<h1>Cover title extraction — AI edit experiment</h1><p>Original cover / output on dark and light backgrounds. AI may reconstruct details. Click output to open original transparent PNG.</p>'
    page += "".join(cards)
    (folder / "index.html").write_text(page, encoding="utf-8")
    return folder / "index.html"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    print(build_report(parser.parse_args().manifest))
