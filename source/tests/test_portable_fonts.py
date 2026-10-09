import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from exactcut import hook_visual, renderer


@pytest.mark.parametrize('style', ['default', *sorted(hook_visual.AUTO_STYLE_IDS)])
def test_hook_uses_packaged_font_even_when_host_fonts_are_present(style, tmp_path):
    decision = hook_visual.choose_hook_visual({'hook_text': 'Her secret'}, {}, forced_style=style)
    root = Path(hook_visual.__file__).with_name('fonts')
    path = Path(decision['font_path'])
    assert path.is_relative_to(root)
    assert path.is_file()
    font = hook_visual._font(str(path), 48)
    assert Path(font.path) == path
    png = hook_visual.render_personalized_hook_png('Her secret', tmp_path, decision)
    with Image.open(png) as image:
        assert image.getbbox() is not None


def test_title_card_uses_packaged_font_with_chinese_glyphs():
    font = renderer._font(64)
    assert Path(font.path).is_relative_to(Path(renderer.__file__).with_name('fonts'))
    # Missing glyphs would render the same tofu box for distinct characters.
    images = []
    for character in ('中', '文'):
        image = Image.new('RGB', (256, 256), 'black')
        renderer._draw_centered_text(image, character, y=128, font_size=64)
        images.append(image)
    assert ImageChops.difference(*images).getbbox() is not None


def test_packaged_font_manifest_matches_bytes_and_includes_licenses():
    root = Path(hook_visual.__file__).with_name('fonts')
    manifest = json.loads((root / 'manifest.json').read_text())
    for entry in manifest['files'].values():
        assert hashlib.sha256((root / entry['file']).read_bytes()).hexdigest() == entry['sha256']
        assert (root / entry['license']).is_file()


def test_missing_packaged_font_is_an_error_not_a_host_fallback():
    with pytest.raises(RuntimeError, match='字体'):
        hook_visual._font('/missing/font.ttf', 48)
