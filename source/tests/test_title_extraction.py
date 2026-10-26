import base64
import io

import pytest
from PIL import Image

from exactcut.title_image import extraction
from exactcut.brand_overlay import BrandOverlay, render, parse


def png(mode='RGBA'):
    image = Image.new(mode, (100, 70), (0, 0, 0, 0) if mode == 'RGBA' else 'white')
    if mode == 'RGBA':
        for x in range(10, 90):
            for y in range(10, 60):
                image.putpixel((x, y), (240, 180, 90, 255))
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def test_response_requires_real_nonempty_transparency():
    with pytest.raises(ValueError, match='透明'):
        extraction.decode_response({'data': [{'b64_json': base64.b64encode(png('RGB')).decode()}]})
    assert extraction.decode_response({'data': [{'b64_json': base64.b64encode(png()).decode()}]}) == png()


def test_asset_is_bound_to_drama_and_detects_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, 'data_root', lambda: tmp_path)
    asset = extraction.store_asset('101607', png(), {'cover_sha256': 'a' * 64})
    assert extraction.asset_file(asset, '101607').is_file()
    with pytest.raises(ValueError):
        extraction.asset_file(asset, '101608')
    extraction.asset_file(asset, '101607').write_bytes(b'changed')
    with pytest.raises(ValueError):
        extraction.asset_file(asset, '101607')


def test_cover_title_render_has_reference_position_and_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, 'data_root', lambda: tmp_path)
    asset = extraction.store_asset('101607', png(), {})
    brand = BrandOverlay(title_enabled=True, title='Example', title_mode='cover',
                         title_asset=asset, title_drama_id='101607')
    path, floor = render(brand, 1080, 1920, tmp_path / 'out.png')
    with Image.open(path) as im:
        box = im.getchannel('A').getbbox()
    assert box[:2] == (54, 42)
    assert box[2] <= 354 and box[3] <= 222
    assert floor == (box[3] + 24) / 1920


def test_cover_mode_never_silently_falls_back_to_text():
    brand, errors = parse({'drama_id': '101607', 'drama_title': 'Example',
                      'brand_overlay': {'title_enabled': True, 'title_mode': 'cover'}})
    assert not errors
    assert brand.title_mode == 'cover' and not brand.title_asset


def test_preparation_pins_asset_across_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, 'data_root', lambda: tmp_path)
    asset = extraction.store_asset('101607', png(), {})
    calls = []
    def extract(drama_id, force=False):
        calls.append(drama_id)
        return asset
    monkeypatch.setattr(extraction, 'extract', extract)
    brand = BrandOverlay(title_enabled=True, title='Example', title_mode='cover', title_drama_id='101607')
    from test_brand_overlay import draft
    from exactcut.intake import validate_draft
    data = draft()
    data['drama_id'] = '101607'
    data['brand_overlay'] = brand.model_dump()
    manifest, errors = validate_draft(data, '')
    assert not errors
    first = extraction.prepare_manifest(manifest, tmp_path / 'output')
    second = extraction.prepare_manifest(manifest, tmp_path / 'output')
    assert len(calls) == 1
    assert first.requests[0].brand_overlay.title_asset == second.requests[0].brand_overlay.title_asset == asset


def test_poster_rejects_untrusted_url():
    with pytest.raises(ValueError):
        extraction.poster_url('https://example.com/cover.png', 'https://aigc-us.oss-accelerate.aliyuncs.com')
    assert extraction.poster_url('/tb/example/image/cover.png', 'https://aigc-us.oss-accelerate.aliyuncs.com').endswith('/tb/example/image/cover.png')


def test_cache_tracks_title_and_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, 'data_root', lambda: tmp_path)
    cfg = {'enabled': True, 'allow_live_requests': True, 'timeout_seconds': 1,
           'model': 'test-model', 'quality': 'high', 'kes_base_url': 'https://example.test'}
    monkeypatch.setattr(extraction, 'settings', lambda: cfg)
    monkeypatch.setattr(extraction, 'environment', lambda: 'development')
    monkeypatch.setattr(extraction, 'credentials', lambda cfg: {'OPENAI_API_KEY': 'test'})
    title = ['Original title']
    monkeypatch.setattr(extraction, 'fetch_cover', lambda *args: (title[0], 'https://example.test/cover', png()))
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(kwargs['json'])
            return extraction.httpx.Response(200, request=extraction.httpx.Request('POST', url),
                                            json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})

    monkeypatch.setattr(extraction.httpx, 'Client', Client)
    extraction.extract('101607')
    extraction.extract('101607')
    assert len(calls) == 1
    title[0] = 'Corrected title'
    extraction.extract('101607')
    assert len(calls) == 2 and 'Corrected title' in calls[-1]['prompt']
    cfg['quality'] = 'medium'
    extraction.extract('101607')
    extraction.extract('101607')
    assert len(calls) == 3 and calls[-1]['quality'] == 'medium'


def test_cover_connection_override_preserves_tls_identity():
    import httpx
    route = extraction.cover_connection({'kes_base_url': 'https://pre-api.welltop.tech',
                                        'kes_connect_ip': '47.117.169.192'})
    request = httpx.Request('POST', 'https://pre-api.welltop.tech/api/v1/user/session')
    route(request)
    assert request.url.host == '47.117.169.192'
    assert request.headers['host'] == 'pre-api.welltop.tech'
    assert request.extensions['sni_hostname'] == 'pre-api.welltop.tech'
    other = httpx.Request('GET', 'https://example.com/image.png')
    route(other)
    assert other.url.host == 'example.com' and 'sni_hostname' not in other.extensions


def test_cover_query_retries_transport_errors_only(monkeypatch):
    import httpx
    calls = []
    def fetch(*args):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError('TLS EOF')
        return 'Title', 'url', b'image'
    monkeypatch.setattr(extraction, 'fetch_cover', fetch)
    monkeypatch.setattr(extraction.time, 'sleep', lambda seconds: None)
    assert extraction.fetch_cover_with_retry(None, '101606', {}, {}) == ('Title', 'url', b'image')
    assert len(calls) == 3
    def invalid(*args):
        raise ValueError('invalid cover')
    monkeypatch.setattr(extraction, 'fetch_cover', invalid)
    with pytest.raises(ValueError, match='invalid cover'):
        extraction.fetch_cover_with_retry(None, '101606', {}, {})


@pytest.fixture
def live_extractor(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(extraction, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(extraction, 'environment', lambda: 'development')
    monkeypatch.setattr(extraction, 'settings', lambda: {
        'enabled': True, 'allow_live_requests': True, 'timeout_seconds': 1,
        'model': 'test', 'quality': 'high', 'kes_base_url': 'https://example.test'})
    monkeypatch.setattr(extraction, 'credentials', lambda cfg: {'OPENAI_API_KEY': 'test'})
    monkeypatch.setattr(extraction, 'fetch_cover', lambda *args: ('Title', 'url', png()))

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(kwargs['json'])
            return extraction.httpx.Response(200, request=extraction.httpx.Request('POST', url),
                json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})

    monkeypatch.setattr(extraction.httpx, 'Client', Client)
    return calls


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'metadata', 'binary_metadata', 'pointer'])
def test_damaged_cache_is_regenerated(live_extractor, damage):
    asset = extraction.extract('101607')
    path = extraction.asset_file(asset, '101607')
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'broken')
    elif damage == 'binary_metadata':
        path.with_name('asset.json').write_bytes(b'\xff\xfe')
    elif damage == 'metadata':
        path.with_name('asset.json').write_text('{}')
    else:
        (extraction.root() / '101607.json').write_text('{broken')
    repaired = extraction.extract('101607')
    assert extraction.asset_file(repaired, '101607').read_bytes() == png()
    assert len(live_extractor) == 2
    assert extraction.extract('101607') == repaired
    assert len(live_extractor) == 2


def test_batch_pin_recovers_missing_asset(live_extractor, tmp_path):
    from test_brand_overlay import draft
    from exactcut.intake import validate_draft
    data = draft()
    data['drama_id'] = '101607'
    data['brand_overlay'] = {'title_enabled': True, 'title_mode': 'cover'}
    manifest, errors = validate_draft(data, '')
    assert not errors
    first = extraction.prepare_manifest(manifest, tmp_path / 'batch')
    extraction.asset_file(first.requests[0].brand_overlay.title_asset, '101607').unlink()
    second = extraction.prepare_manifest(manifest, tmp_path / 'batch')
    assert extraction.asset_file(second.requests[0].brand_overlay.title_asset, '101607').is_file()
    extraction.prepare_manifest(manifest, tmp_path / 'batch')
    assert len(live_extractor) == 2


def test_different_dramas_generate_concurrently(live_extractor, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    entered = Barrier(2, timeout=5)
    def fetch(client, drama, cfg, secrets):
        entered.wait()
        return 'Title', 'url', png()
    monkeypatch.setattr(extraction, 'fetch_cover', fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(extraction.extract, ['101606', '101607']))
    assert len(set(results)) == 2 and len(live_extractor) == 2


def test_same_drama_concurrency_reuses_generation(live_extractor):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(extraction.extract, ['101607', '101607']))
    assert results[0] == results[1] and len(live_extractor) == 1


def test_corrupt_cache_does_not_bypass_disabled_live_requests(live_extractor, monkeypatch):
    asset = extraction.extract('101607')
    extraction.asset_file(asset, '101607').unlink()
    cfg = extraction.settings()
    cfg['allow_live_requests'] = False
    monkeypatch.setattr(extraction, 'settings', lambda: cfg)
    with pytest.raises(ValueError, match='实时提取未启用'):
        extraction.extract('101607')
    assert len(live_extractor) == 1


def test_explicit_asset_recovers_but_wrong_drama_is_rejected(live_extractor, tmp_path):
    from test_brand_overlay import draft
    from exactcut.intake import validate_draft
    asset = extraction.extract('101607')
    data = draft()
    data['drama_id'] = '101607'
    data['brand_overlay'] = {'title_enabled': True, 'title_mode': 'cover', 'title_asset': asset}
    manifest, errors = validate_draft(data, '')
    assert not errors
    extraction.asset_file(asset, '101607').unlink()
    parsed, errors = parse(data)
    assert not errors and not parsed.title_asset
    restored = extraction.prepare_manifest(manifest, tmp_path / 'batch')
    assert extraction.asset_file(restored.requests[0].brand_overlay.title_asset, '101607').is_file()
    data['drama_id'] = '101606'
    _, errors = parse(data)
    assert any('剧 ID 不符' in error for error in errors)
