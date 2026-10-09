"""Font files shipped with the application; host font discovery is never used."""
import json
from pathlib import Path

from PIL import ImageFont

ROOT = Path(__file__).with_name('fonts')


def font_path(profile: str = 'default') -> str:
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    filename = manifest['profiles'][profile]
    path = ROOT / manifest['files'][filename]['file']
    if not path.is_file() or path.resolve().parent != ROOT.resolve():
        raise RuntimeError(f'应用字体缺失或路径无效: {filename}')
    return str(path)


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    candidate = Path(path)
    if candidate.resolve().parent != ROOT.resolve():
        raise RuntimeError('只允许使用应用内置字体，请重新生成字幕预览')
    try:
        return ImageFont.truetype(str(candidate), size=size)
    except OSError as exc:
        raise RuntimeError(f'应用字体无法加载: {candidate.name}') from exc
