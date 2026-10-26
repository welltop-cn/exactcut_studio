import json
from pathlib import Path

from PIL import Image

from exactcut.brand_overlay import BrandOverlay, render
from exactcut.intake import validate_draft
from exactcut.media import run_checked
from exactcut.models import CutRequest
from exactcut.orchestrator import _request_fingerprint
from exactcut.renderer import _apply_hook, _concat_units
from exactcut.util import load_config


def draft():
    return {
        'drama_id': '101500', 'drama_title': 'The Forbidden Ride', 'business_date': '20261008',
        'author': '马涛', 'material_type': 'typezxraw', 'time_notation': 'seconds',
        'rows': [{
            'request_id': 'R1', 'seq': 1, 'title_cn': '', 'hook_text': '', 'structure': '顺叙', 'notes': '',
            'timeline_text': 'EP1 00:05.000-00:08.000', 'source': '网页填写',
            'hook_options': {'mode': 'none', 'style': 'default', 'position': 'top', 'duration_ms': 2000, 'manual_ranges': []},
        }],
    }


def test_switches_default_off_and_do_not_change_an_existing_fingerprint(tmp_path, monkeypatch):
    manifest, errors = validate_draft(draft(), '')
    assert errors == []
    assert manifest.brand_overlay == BrandOverlay()
    assert manifest.requests[0].brand_overlay == BrandOverlay()
    episode = tmp_path / 'EP1.mp4'
    episode.write_bytes(b'episode')
    monkeypatch.setattr('exactcut.orchestrator.resolve_episode_video', lambda project, name: episode)
    monkeypatch.setattr('exactcut.orchestrator.file_sha256', lambda path: 'abc')
    request = manifest.requests[0]
    plain = _request_fingerprint(tmp_path, request, {'render': {'width': 1080}})
    stored = json.loads(request.model_dump_json())
    stored.pop('brand_overlay')
    bare = CutRequest.model_validate(stored)
    assert _request_fingerprint(tmp_path, bare, {'render': {'width': 1080}}) == plain


def test_title_and_logo_switches_are_independent(tmp_path):
    missing = draft()
    missing['drama_title'] = ''
    missing['brand_overlay'] = {'title_enabled': True, 'logo_enabled': False, 'logo': ''}
    manifest, errors = validate_draft(missing, '')
    assert manifest is None
    assert errors == ['勾选视频水印「剧名」后需要填写剧名']
    logo = draft()
    logo['brand_overlay'] = {'title_enabled': False, 'logo_enabled': True, 'logo': ''}
    manifest, errors = validate_draft(logo, '')
    assert manifest is None
    assert errors == ['勾选视频水印「LOGO」后需要选择 Reel.AI 或 Loreker']
    both = draft()
    both['brand_overlay'] = {'title_enabled': True, 'logo_enabled': True, 'logo': 'loreker'}
    manifest, errors = validate_draft(both, '')
    assert errors == []
    assert manifest.brand_overlay.title == 'The Forbidden Ride'
    assert manifest.brand_overlay.logo == 'loreker'


def test_enabled_brand_changes_the_fingerprint(tmp_path, monkeypatch):
    episode = tmp_path / 'EP1.mp4'
    episode.write_bytes(b'episode')
    monkeypatch.setattr('exactcut.orchestrator.resolve_episode_video', lambda project, name: episode)
    monkeypatch.setattr('exactcut.orchestrator.file_sha256', lambda path: 'abc')
    base, _ = validate_draft(draft(), '')
    marked = draft()
    marked['brand_overlay'] = {'title_enabled': True, 'logo_enabled': True, 'logo': 'reelai'}
    changed, errors = validate_draft(marked, '')
    assert errors == []
    config = {'render': {'width': 1080}}
    assert _request_fingerprint(tmp_path, base.requests[0], config) != _request_fingerprint(tmp_path, changed.requests[0], config)


def test_png_puts_the_title_at_the_top_left_and_the_logo_at_the_bottom(tmp_path):
    brand = BrandOverlay(title_enabled=True, logo_enabled=True, logo='reelai', title='Give Me Back My Name')
    path, floor = render(brand, 1080, 1920, tmp_path / 'brand.png')
    with Image.open(path) as image:
        assert image.mode == 'RGBA'
        assert image.getpixel((0, 0))[3] == 0
        assert image.crop((0, 0, 640, 180)).getbbox()
        assert image.crop((860, 0, 1080, 180)).getbbox() is None
        logo_box = image.crop((0, 1600, 1080, 1920)).getbbox()
        assert logo_box and logo_box[0] > 200
        assert abs((logo_box[0] + logo_box[2]) / 2 - 540) <= 2
        assert abs((1600 + logo_box[3]) - (1920 - 110)) <= 2
    assert floor > 48 / 1920
    long = BrandOverlay(title_enabled=True, logo_enabled=False, logo='', title='这是一个很长的中文剧名用来确认最多只排两行并且仍然留在左上角')
    long_path, _ = render(long, 1080, 1920, tmp_path / 'long.png')
    with Image.open(long_path) as image:
        box = image.getbbox()
        assert box[0] < 50 and box[1] < 60 and box[3] < 220 and box[2] < 800


def test_brand_stays_on_the_plot_and_off_the_ending(tmp_path):
    render_config = {**load_config()['render'], 'width': 108, 'height': 192, 'preset': 'ultrafast', 'crf': 28}
    source, plot, ending, output = (tmp_path / name for name in ('source.mp4', 'plot.mp4', 'ending.mp4', 'joined.mp4'))
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=108x192:r=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)])
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=108x192:r=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '0.5',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(ending)])
    brand = BrandOverlay(title_enabled=False, logo_enabled=True, logo='loreker', title='')
    brand_png, _ = render(brand, 108, 192, tmp_path / 'brand.png')
    _apply_hook(source, '', plot, render_config, tmp_path, brand_png=brand_png, normalized_input=True)
    _concat_units([plot, ending], output, render_config, 1500)
    def corner(video, second):
        frame = tmp_path / f'{second}.png'
        run_checked(['ffmpeg', '-y', '-v', 'error', '-ss', str(second), '-i', str(video), '-frames:v', '1', str(frame)])
        with Image.open(frame) as image:
            rgb = image.convert('RGB')
            return [rgb.getpixel((x, y)) for y in range(160, 192) for x in range(48)]
    plot_colors = {pixel for pixel in corner(plot, 0.2)}
    assert any(pixel[2] < 200 or pixel[0] > 40 for pixel in plot_colors)
    ending_colors = {pixel for pixel in corner(output, 1.2)}
    assert all(pixel[1] > pixel[0] and pixel[1] > pixel[2] for pixel in ending_colors)


def test_editor_shows_two_independent_switches():
    source = Path(__file__).parents[1].joinpath('exactcut/web/app.js').read_text()
    assert '视频水印' in source
    assert '>剧名</label>' in source
    assert '>LOGO</label>' in source
    assert '剧名放在左上角' not in source
    assert 'value="reelai"' in source and 'value="loreker"' in source
    assert '默认关闭' not in source


def test_hook_covers_brand_without_moving_and_brand_remains_after_hook(tmp_path, monkeypatch):
    from exactcut import renderer
    from exactcut.hook_options import HookOptions
    config = {**load_config()['render'], 'width': 108, 'height': 192,
              'preset': 'ultrafast', 'crf': 18, 'hook_fade_out_ms': 0}
    source = tmp_path / 'source.mp4'
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'color=c=blue:s=108x192:r=30', '-t', '2', '-c:v', 'libx264', str(source)])
    brand, hook = tmp_path / 'red.png', tmp_path / 'green.png'
    Image.new('RGBA', (108, 192), 'red').save(brand)
    Image.new('RGBA', (108, 192), (0, 255, 0, 255)).save(hook)
    monkeypatch.setattr(renderer, 'render_personalized_hook_png', lambda *args, **kwargs: hook)
    for effects in (False, True):
        output = tmp_path / f'out-{effects}.mp4'
        plan = {'total_frames': 60, 'captions': [], 'flashes': []} if effects else None
        decision = _apply_hook(source, 'Test', output, config, tmp_path,
            options=HookOptions(position='top', duration_ms=1000),
            preview_decision={'placement': {'position': 'top', 'y_ratio': .07}},
            brand_png=brand, hook_floor=.3, normalized_input=True, segment_plan=plan)
        assert decision['placement']['y_ratio'] == .07
        for second, channel in ((.2, 1), (1.5, 0)):
            frame = tmp_path / f'frame-{effects}-{second}.png'
            run_checked(['ffmpeg', '-y', '-v', 'error', '-ss', str(second), '-i', str(output),
                         '-frames:v', '1', str(frame)])
            with Image.open(frame) as image:
                pixel = image.convert('RGB').getpixel((50, 50))
            assert pixel[channel] > 200 and all(v < 30 for i, v in enumerate(pixel) if i != channel)
