"""List and sign private original-episode objects.

open_original streams one object to the caller. Nothing is copied or stored."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from . import diagnostics as diag
from .runtime import CODE_ROOT, data_root, environment, protect_path, require_material_fetch, source_root
from .source_cache import check_drama, check_episode

CONTENT_ROOT = 'assets'
SOURCE_ROOT = 'tb/tbl5wK7ykMP9ZNKb'
SOURCE_BUCKET_DEFAULT = 'aigc-us02'
LINK_SECONDS = 900
FILE_NAME = re.compile(r'^EP0*([1-9]\d{0,2})\.mp4$', re.IGNORECASE)
EPISODE_LABEL = re.compile(r'EP\s*0*([1-9]\d{0,2})', re.IGNORECASE)
_CREDENTIALS = (
    'ALIYUN_OSS_ACCESS_KEY_ID', 'ALIYUN_OSS_ACCESS_KEY_SECRET', 'ALIYUN_SECURITY_TOKEN',
    'ALIYUN_OSS_ENDPOINT', 'ALIYUN_OSS_BUCKET', 'ALIYUN_OSS_SOURCE_BUCKET',
    'METABASE_HOST', 'METABASE_USER', 'METABASE_PASSWORD',
)
_REQUIRED = (
    'ALIYUN_OSS_ACCESS_KEY_ID', 'ALIYUN_OSS_ACCESS_KEY_SECRET', 'ALIYUN_OSS_ENDPOINT',
    'ALIYUN_OSS_BUCKET', 'METABASE_HOST', 'METABASE_USER', 'METABASE_PASSWORD',
)


class OriginalsUnavailable(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def browser_endpoint(endpoint):
    raw = str(endpoint or '').strip()
    if '://' not in raw:
        raw = 'https://' + raw
    host = (urlparse(raw).hostname or '').lower()
    if host.endswith('.aliyuncs.com') and '-internal.' in host:
        host = host.replace('-internal.', '.', 1)
    if not host or host.endswith('.local') or 'internal' in host.split('.')[0]:
        raise OriginalsUnavailable(503, '原片下载需要公网 OSS Endpoint')
    return 'https://' + host


def _parse_env(path):
    file = Path(path)
    if not file.is_file():
        return {}
    owner = next((name for name in ('development', 'staging', 'production', 'test')
                  if file.name in {f'.env.{name}', f'{name}.env'}), None)
    if owner and owner != environment():
        return {}
    values = {}
    for raw in file.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def credentials():
    values = {key: os.environ.get(key, '').strip() for key in _CREDENTIALS}
    if environment() == 'development':
        paths = []
        dev_env = os.environ.get('EXACTCUT_DEV_FETCH_ENV', '').strip()
        if dev_env:
            paths.append(dev_env)
        paths.append(CODE_ROOT / '.env')
        for path in paths:
            parsed = _parse_env(path)
            for key in _CREDENTIALS:
                if not values.get(key) and parsed.get(key):
                    values[key] = parsed[key]
    if not values.get('ALIYUN_OSS_SOURCE_BUCKET'):
        values['ALIYUN_OSS_SOURCE_BUCKET'] = SOURCE_BUCKET_DEFAULT
    if any(not values.get(key) for key in _REQUIRED):
        raise OriginalsUnavailable(503, '原片下载配置缺失')
    values['ALIYUN_OSS_ENDPOINT'] = browser_endpoint(values['ALIYUN_OSS_ENDPOINT'])
    return values


def _episode_number(name):
    match = FILE_NAME.fullmatch(name.rsplit('/', 1)[-1])
    if not match:
        return None
    number = int(match.group(1))
    return number if number <= 500 else None


def _choose(items):
    chosen = {}
    for key, size in items:
        number = _episode_number(key)
        if number is None:
            continue
        canonical = key.rsplit('/', 1)[-1].upper() == f'EP{number}.MP4'
        rank = (0 if canonical else 1, key)
        current = chosen.get(number)
        if current is None or rank < current[0]:
            chosen[number] = (rank, key, int(size))
    return {number: (key, size) for number, (_, key, size) in chosen.items()}


def _label_number(value):
    text = str(value or '').strip()
    if re.fullmatch(r'\d{1,3}', text):
        number = int(text)
    else:
        match = EPISODE_LABEL.search(text)
        if not match:
            return None
        number = int(match.group(1))
    return number if 1 <= number <= 500 else None


def shelf_sql(drama_id):
    series_id = int(drama_id)
    return f"""
SELECT trailer_ep, publish_at, order_num
FROM toc_episode
WHERE series_id = {series_id}
  AND deleted = 0
  AND hidden = 0
  AND publish_at <= NOW()
ORDER BY order_num ASC
"""


def fetch_listed(drama_id, *, query=None):
    rows = (query or metabase_query)(shelf_sql(drama_id))
    listed = set()
    for row in rows:
        number = _label_number(row.get('trailer_ep'))
        if number is not None:
            listed.add(number)
    return listed


def assemble(drama_id, content_items, source_items, listed, *, source_gap=False):
    content = _choose(content_items)
    source = _choose(source_items)
    episodes = []
    for number in sorted(set(content) | set(source)):
        _, size = content[number] if number in content else source[number]
        episodes.append({'episode': f'EP{number}', 'listed': number in listed, 'bytes': size})
    result = {'drama_id': drama_id, 'episodes': episodes}
    if source_gap:
        result['notice'] = '源桶暂时读不到，列表只包含内容桶里的原片。'
    return result


def local_only():
    return environment() == 'development' and os.environ.get('EXACTCUT_ALLOW_MATERIAL_FETCH') != '1'


def _local_files(drama_id):
    check_drama(drama_id)
    found = {}
    for base, directory in (
        (source_root(), source_root() / drama_id),
        (data_root(), data_root() / 'projects' / drama_id / 'source' / 'videos'),
    ):
        # Reject links at every relative directory component, including a linked
        # drama or project/source directory. The configured roots are protected.
        cursor = directory
        while cursor != base:
            if cursor.is_symlink():
                break
            cursor = cursor.parent
        else:
            protect_path(directory)
            for path in directory.glob('EP*.mp4'):
                try:
                    check_episode(path.stem)
                except RuntimeError:
                    continue
                if not path.is_symlink() and path.is_file() and path.stat().st_size > 0:
                    found.setdefault(path.stem, path)
    return found


def local_original(drama_id, episode):
    check_drama(drama_id)
    check_episode(episode)
    path = _local_files(drama_id).get(episode)
    if path is None:
        raise OriginalsUnavailable(404, '本地正片尚未就绪')
    return path


def load_originals(drama_id, *, store=None, listed=None):
    if local_only():
        files = _local_files(drama_id)
        result = {'drama_id': drama_id, 'episodes': [
            {'episode': episode, 'listed': None, 'bytes': files[episode].stat().st_size}
            for episode in sorted(files, key=lambda value: int(value[2:]))
        ]}
        return result
    require_material_fetch()
    check_drama(drama_id)
    store = store or connect()
    if listed is None:
        listed = fetch_listed(drama_id)
    content_prefix = f'{CONTENT_ROOT}/{drama_id}/video/'
    source_prefix = f'{SOURCE_ROOT}/{drama_id}/'
    content_items = store.keys('content', content_prefix)
    source_gap = False
    try:
        source_items = store.keys('source', source_prefix)
    except OriginalsUnavailable:
        source_items = []
        source_gap = True
    return assemble(drama_id, content_items, source_items, listed, source_gap=source_gap)


def _locate(store, drama_id, number):
    canonical = (
        ('content', f'{CONTENT_ROOT}/{drama_id}/video/EP{number}.mp4'),
        ('source', f'{SOURCE_ROOT}/{drama_id}/EP{number}.mp4'),
    )
    for role, key in canonical:
        if store.head(role, key):
            return role, key
    for role, prefix in (('content', f'{CONTENT_ROOT}/{drama_id}/video/'), ('source', f'{SOURCE_ROOT}/{drama_id}/')):
        try:
            found = _choose(store.keys(role, prefix))
        except OriginalsUnavailable:
            continue
        if number in found and store.head(role, found[number][0]):
            return role, found[number][0]
    return None


def sign_original(drama_id, episode, *, store=None):
    require_material_fetch()
    check_drama(drama_id)
    check_episode(episode)
    store = store or connect()
    found = _locate(store, drama_id, int(episode[2:]))
    if found is None:
        raise OriginalsUnavailable(404, '源片不存在')
    role, key = found
    return {'url': store.sign(role, key, episode), 'expires_in': LINK_SECONDS}


class _Remote:
    def __init__(self, response):
        self._response = response
        self.status = response.status_code
        self.headers = response.headers

    def iter_bytes(self):
        return self._response.iter_content(chunk_size=256 * 1024)

    def close(self):
        self._response.close()


def _get_stream(url, start=0):
    import requests
    session = requests.Session()
    session.trust_env = False
    # Content-Length and Range offsets must describe the bytes we forward.
    headers = {'Accept-Encoding': 'identity'}
    if start:
        headers['Range'] = f'bytes={start}-'
    try:
        response = session.get(url, stream=True, timeout=(20, 300), headers=headers)
    except requests.RequestException as exc:
        raise OriginalsUnavailable(502, '原片暂时下不了') from exc
    return _Remote(response)


def open_original(drama_id, episode, *, store=None, getter=None, start=0):
    """Open the object, optionally continuing from ``start`` bytes already delivered."""
    signed = sign_original(drama_id, episode, store=store)
    diag.bind(upstream_url=diag.redact_url(signed['url']))
    get = getter or _get_stream
    remote = get(signed['url'], start=start) if start else get(signed['url'])
    expected = 206 if start else 200
    if remote.status != expected:
        remote.close()
        raise OriginalsUnavailable(502, '原片暂时下不了')
    if str(remote.headers.get('Content-Encoding') or '').strip().lower() not in ('', 'identity'):
        remote.close()
        raise OriginalsUnavailable(502, '原片暂时无法下载，请稍后重试')
    return remote


def validate_resume(remote, start, *, length=None, etag=None):
    """Reject a different byte range or object before forwarding its body."""
    known_length = length is not None and str(length).isdigit()
    if start == 0 and remote.status == 200:
        valid = not known_length or remote.headers.get('Content-Length') == str(length)
    else:
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)', str(remote.headers.get('Content-Range', '')))
        valid = remote.status == 206 and match is not None
        if valid:
            first, last = int(match[1]), int(match[2])
            total = None if match[3] == '*' else int(match[3])
            valid = first == start and last >= first and (total is None or last == total - 1)
            if known_length:
                valid = valid and total == int(length)
            remaining = remote.headers.get('Content-Length')
            if remaining is not None:
                valid = valid and str(remaining).isdigit() and int(remaining) == last - first + 1
    if etag:
        valid = valid and not etag.startswith('W/') and remote.headers.get('ETag') == etag
    if not valid:
        remote.close()
        raise OriginalsUnavailable(502, '原片续传响应不一致，请重新下载')


def metabase_query(sql):
    import requests
    values = credentials()
    session = requests.Session()
    session.trust_env = False
    try:
        login = session.post(
            values['METABASE_HOST'].rstrip('/') + '/api/session',
            json={'username': values['METABASE_USER'], 'password': values['METABASE_PASSWORD']},
            timeout=20,
        )
        token = login.json().get('id') if login.ok else None
        if not token:
            raise OriginalsUnavailable(503, '上架状态暂时读不到')
        response = session.post(
            values['METABASE_HOST'].rstrip('/') + '/api/dataset',
            headers={'X-Metabase-Session': token},
            json={'database': 2, 'type': 'native', 'native': {'query': sql}},
            timeout=60,
        )
        payload = response.json() if response.ok else {}
    except OriginalsUnavailable:
        raise
    except Exception as exc:
        raise OriginalsUnavailable(503, '上架状态暂时读不到') from exc
    if not response.ok or payload.get('error'):
        raise OriginalsUnavailable(503, '上架状态暂时读不到')
    data = payload.get('data') or {}
    columns = [item.get('name') for item in data.get('cols') or []]
    return [dict(zip(columns, row)) for row in data.get('rows') or []]


class OssPair:
    """HEAD, list, and sign only. This object has no copy or download method."""

    def __init__(self, values):
        import oss2
        token = values.get('ALIYUN_SECURITY_TOKEN') or ''
        auth = oss2.StsAuth(values['ALIYUN_OSS_ACCESS_KEY_ID'], values['ALIYUN_OSS_ACCESS_KEY_SECRET'], token) if token else oss2.Auth(
            values['ALIYUN_OSS_ACCESS_KEY_ID'], values['ALIYUN_OSS_ACCESS_KEY_SECRET'])
        endpoint = values['ALIYUN_OSS_ENDPOINT']
        self.buckets = {
            'content': oss2.Bucket(auth, endpoint, values['ALIYUN_OSS_BUCKET'], connect_timeout=30),
            'source': oss2.Bucket(auth, endpoint, values['ALIYUN_OSS_SOURCE_BUCKET'], connect_timeout=30),
        }
        for bucket in self.buckets.values():
            bucket.session.session.trust_env = False

    def head(self, role, key):
        import oss2
        try:
            self.buckets[role].head_object(key)
            return True
        except oss2.exceptions.OssError as exc:
            if getattr(exc, 'status', None) == 404:
                return False
            raise OriginalsUnavailable(503, '原片暂时无法确认') from exc

    def keys(self, role, prefix):
        import oss2
        try:
            found = []
            for item in oss2.ObjectIterator(self.buckets[role], prefix=prefix):
                if not item.key.endswith('/'):
                    found.append((item.key, int(item.size)))
            return found
        except Exception as exc:
            raise OriginalsUnavailable(503, '原片列表暂时读不到') from exc

    def sign(self, role, key, episode):
        disposition = f'attachment; filename="{episode}.mp4"'
        return self.buckets[role].sign_url(
            'GET', key, LINK_SECONDS,
            params={'response-content-disposition': disposition},
            slash_safe=True,
        )


def connect():
    return OssPair(credentials())
