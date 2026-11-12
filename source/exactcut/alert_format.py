"""Notification formatting adapted from the standalone production monitor."""
import hashlib
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

SHANGHAI = timezone(timedelta(hours=8))


def stamp(value=None, seconds=False):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            value = None
    return datetime.fromtimestamp(time.time() if value is None else value, SHANGHAI).strftime('%m-%d %H:%M:%S' if seconds else '%m-%d %H:%M')


def valid_batch(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def context_clean(value, limit=200, webhook=''):
    """样例专用：保留 URL 的主机/路径、API 路径和关联 id，其余仍脱敏。"""
    text = str(value).replace('\x00', '')
    if webhook:
        text = text.replace(webhook, '[链接]')
    kept = []

    def keep(value):
        kept.append(value)
        return f'\x00{len(kept) - 1}\x00'

    def url(match):
        try:
            parts = urlsplit(match.group())
            if not parts.hostname or re.search(r'(?i)(webhook|/hooks?(?:/|$)|/bot/)', parts.path):
                return '[链接]'
            # 不保留 userinfo、query 或 fragment。
            host = parts.hostname
            if ':' in host:
                host = f'[{host}]'
            if parts.port:
                host += f':{parts.port}'
            return keep(urlunsplit((parts.scheme, host, parts.path, '', '')))
        except ValueError:
            return '[链接]'

    text = re.sub(r'https?://[^\s<>"\']+', url, text, flags=re.I)
    text = re.sub(r'''(?ix)(?:bearer\s+|["']?(?:token|secret|password|api[_-]?key|access[_-]?key(?:id)?|signature|expires|webhook)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', '[已脱敏]', text)

    def path(match):
        value = match.group()
        route = re.split(r'[?#]', value, maxsplit=1)[0]
        if route.startswith('/api/') and not re.search(r'(?:^|/)\.\.(?:/|$)|%2e|%2f|%5c', route, re.I):
            return keep(route)
        return '[路径]'

    text = re.sub(r'(?<![\w/])/(?:[^\s,;<>"\'()]+)', path, text)
    text = re.sub(r'\x00(\d+)\x00', lambda match: kept[int(match[1])], text)
    text = ' '.join(text.split())
    if len(text) > limit:
        # 尽量在词边界截断，并明确标出省略。
        end = text.rfind(' ', max(0, limit - 80), limit)
        text = text[:end if end > 0 else limit] + '…'
    return text


def event_context(event, config, webhook=''):
    def safe(value, limit=200):
        return context_clean(value, limit, webhook)

    lines = []
    child = event.get('batch_id')
    batch = event.get('parent_batch_id') if valid_batch(event.get('parent_batch_id')) else child
    if valid_batch(child) and batch != child:
        lines.append(f'内部批次：{child}')
    if valid_batch(batch):
        base = safe(config['public_url'], 1000).rstrip('/')
        if base.startswith(('https://', 'http://')):
            lines.append(f'项目：{base}/#{batch}')
        lines.append(f'项目 id：{batch}')
    elif batch is not None:
        lines.append('项目 id：[无效]')
    for key in ('project_name', 'batch_name', 'title', 'name'):
        if event.get(key):
            lines.append(f'项目名：{safe(event[key])}')
            break
    fields = []
    if event.get('actor'):
        fields.append(f'账号：{safe(event["actor"])}')
    route = safe(event.get('route') or '', 1000)
    asset = next((event[k] for k in ('asset', 'asset_id', 'asset_name', 'material_id') if event.get(k)), None)
    if asset is None:
        match = re.search(r'/originals/([^/\s]+)', route)
        asset = match[1] if match else None
    if asset is not None:
        fields.append(f'素材：{safe(asset)}')
    id_keys = {'request_id', 'client_request_id', 'trace_id', 'job_id', 'render_id'}
    id_keys.update(k for k in event if k.startswith('render_') and k.endswith('_id'))
    for key in sorted(id_keys):
        if event.get(key) is not None:
            fields.append(f'{key}：{safe(event[key])}')
    if fields:
        lines.append('；'.join(fields))
    http = []
    for key in ('method', 'route', 'status', 'duration_ms'):
        if event.get(key) is not None:
            http.append(route if key == 'route' else f'{key}={safe(event[key])}')
    if http:
        lines.append('请求：' + ' '.join(http))
    if event.get('upstream_url'):
        lines.append('上游：' + safe(event['upstream_url'], 1000))
    if event.get('at'):
        lines.append(f'样例时间：{stamp(event["at"], seconds=True)}')
    lines.append(f'主机：{safe(config["host_label"], 40)}')
    if event.get('source_file'):
        lines.append(f'日志：{safe(Path(str(event["source_file"])).name)}')
    err = event.get('error') or {}
    frames = err.get('frames') or []
    if frames:
        frame = frames[-1]
        parts = [safe(Path(str(frame['file'])).name)] if frame.get('file') else []
        if frame.get('line') is not None:
            parts.append(safe(frame['line'], 20))
        location = ':'.join(parts)
        if frame.get('function'):
            location += ' ' + safe(frame['function'])
        if location:
            lines.append('栈：' + location.strip())
    message = err.get('message') or event.get('message') or event.get('reason') or '无摘要'
    lines.append(f'错误：{safe(err.get("type") or "", 100)} {safe(message, 800)}')
    return lines


def signature(event):
    err = event.get('error') or {}
    frames = err.get('frames') or []
    if frames:
        key = f"{event.get('event')}|{err.get('type')}|{frames[0].get('file')}:{frames[0].get('function')}"
    else:
        message = str(err.get('message') or event.get('message') or '')
        message = re.sub(r'0x[0-9a-fA-F]+|\b[0-9a-fA-F]{16,}\b|\d+', '', message[:60])
        key = f"{event.get('event')}|{message}"
    return hashlib.sha256(key.encode()).hexdigest()
