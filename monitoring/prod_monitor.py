#!/usr/bin/env python3
"""ExactCut 线上监控，仅使用 Python 标准库。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit

SHANGHAI = timezone(timedelta(hours=8))
DEFAULTS = {
    'host_label': '线上', 'health_port': 18792,
    'public_url': 'https://autocut.creativefitting.ai/home/',
    'process_match': 'exactcut.webserver', 'log_dir': '/srv/exactcut/data/logs',
    'mounts': [], 'failure_count': 2, 'heartbeat_seconds': 120,
    'disk_high': 85, 'disk_low': 80, 'memory_low': 10, 'memory_high': 15,
    'load_high': 1.5, 'load_low': 1.0, 'load_count': 3,
    'cooldown_seconds': 1800, 'warning_events': [],
    'ignore_errors': [{'event': e, 'type': t} for e in
                      ('http.failed', 'http.handler_failed') for t in
                      ('BrokenPipeError', 'ConnectionResetError')],
}


def clean(value, limit=120):
    """输出统一脱敏，不暴露链接、令牌和本机路径。"""
    text = str(value)
    text = re.sub(r'https?://[^\s<>"\']+', '[链接]', text, flags=re.I)
    text = re.sub(r'(?i)(?:bearer\s+|(?:token|secret|password|api_key|webhook)\s*[=:]\s*)\S+', '[已脱敏]', text)
    text = re.sub(r'\b[0-9a-fA-F]{16,}\b', '[标识]', text)
    text = re.sub(r'(?<!\w)/(?:[^\s/:]+/)+[^\s,;]*', '[路径]', text)
    return text.replace('\n', ' ').replace('\r', ' ')[:limit]


def log_error(area, exc):
    print(f'检查异常：{area}（{type(exc).__name__}）', file=sys.stderr)


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
    text = str(value)
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
    batch = event.get('batch_id')
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


def save_state(directory, state):
    fd, name = tempfile.mkstemp(prefix='.state-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, directory / 'state.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def webhook_value(filename):
    path = Path(filename).expanduser()
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key == 'FEISHU_ALERT_WEBHOOK':
                return value.strip().strip('\"\'')
    return os.environ.get('FEISHU_ALERT_WEBHOOK', '')


def send_message(webhook, text):
    if not webhook:
        raise ValueError('未配置通知地址')
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}}).encode()
    req = request.Request(webhook, data=payload, headers={'Content-Type': 'application/json'})
    with request.urlopen(req, timeout=10) as response:
        if response.status != 200 or json.load(response).get('code') != 0:
            raise ValueError('通知未成功')


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_check(config):
    url = f"http://127.0.0.1:{int(config['health_port'])}/healthz"
    with request.urlopen(url, timeout=10) as response:
        if response.status != 200:
            return False, '健康接口响应异常', {}
        data = json.load(response)
    if data.get('maintenance') is True:
        return True, '维护中', data
    good = (data.get('status') == 'ok' and data.get('worker_alive') is True
            and float(data.get('worker_heartbeat_age_seconds', float('inf'))) <= config['heartbeat_seconds'])
    return good, '健康接口与心跳正常' if good else '健康接口或工作进程心跳异常', data


def public_check(config):
    try:
        with request.build_opener(NoRedirect).open(config['public_url'], timeout=10) as response:
            code = response.status
    except error.HTTPError as exc:
        code = exc.code
    return 200 <= code < 400


def process_check(config):
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            if config['process_match'] in path.read_bytes().replace(b'\0', b' ').decode(errors='replace'):
                return True
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return False


def resources(config):
    values = []

    def collect(label, function):
        try:
            values.append(function())
        except Exception as exc:
            values.append((None, label, exc))

    for mount in dict.fromkeys(['/', *config['mounts']]):
        def disk():
            stat = os.statvfs(mount)
            used = stat.f_blocks - stat.f_bfree
            percent = 100 * used / max(1, used + stat.f_bavail)
            return (f'disk:{mount}', f'磁盘 {mount}', percent > config['disk_high'], percent < config['disk_low'], 1, f'使用率 {percent:.1f}%')
        collect(f'磁盘 {mount}', disk)

    def memory():
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            mem[key] = int(value.split()[0])
        available = 100 * mem['MemAvailable'] / mem['MemTotal']
        return ('memory', '内存', available < config['memory_low'], available > config['memory_high'], 1, f'可用 {available:.1f}%')
    collect('内存', memory)

    def load_check():
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
        return ('load', '负载', load > config['load_high'], load < config['load_low'], config['load_count'], f'每核负载 {load:.2f}')
    collect('负载', load_check)
    return values


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


def scan_events(config, state, bootstrap=False):
    """只提交完整行的游标；首次运行直接建立末尾基线。"""
    directory = Path(config['log_dir'])
    cursors = dict(state.get('cursors', {}))
    first = not state.get('scan_initialized', False)
    groups = {}
    paths = sorted(directory.glob('events-????????-*.jsonl'))
    # 显式检查目录，避免 glob 在部分系统上把权限错误当成空目录。
    with os.scandir(directory):
        pass
    present = set()
    for path in paths:
        present.add(path.name)
        with path.open('rb') as handle:
            stat = os.fstat(handle.fileno())
            old = cursors.get(path.name, {})
            offset = old.get('offset', 0) if old.get('inode') == stat.st_ino else 0
            if offset > stat.st_size:
                offset = 0
            if bootstrap or first:
                # 尾部半行也跳过，避免下轮把历史残片解析成新事件。
                handle.seek(0, 2)
                offset = handle.tell()
                skip_partial = False
                if offset:
                    handle.seek(offset - 1)
                    skip_partial = handle.read(1) != b'\n'
            else:
                skip_partial = old.get('skip_partial', False) if offset else False
                handle.seek(offset)
                while True:
                    line = handle.readline()
                    if not line or not line.endswith(b'\n'):
                        break
                    offset = handle.tell()
                    if skip_partial:
                        skip_partial = False
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        err = event.get('error') or {}
                        if event.get('level', '').lower() not in ('error', 'critical') and event.get('event') not in config['warning_events']:
                            continue
                        if any(event.get('event') == item['event'] and err.get('type') == item['type'] for item in config['ignore_errors']):
                            continue
                        key = signature(event)
                        event['source_file'] = path.name
                        group = groups.setdefault(key, {'count': 0, 'first': event.get('at'), 'last': event.get('at'), 'batches': [], 'sample': event})
                        group['count'] += 1
                        group['last'] = event.get('at')
                        batch = event.get('batch_id')
                        if batch is not None and batch not in group['batches'] and len(group['batches']) < 3:
                            group['batches'].append(batch)
                    except (ValueError, TypeError, AttributeError, KeyError):
                        print('报错日志含无效行，已跳过', file=sys.stderr)
            cursors[path.name] = {'inode': stat.st_ino, 'offset': offset, 'skip_partial': skip_partial}
    state['cursors'] = {key: value for key, value in cursors.items() if key in present}
    state['scan_initialized'] = True
    return groups


class Monitor:
    def __init__(self, config, state, webhook='', dry_run=False, bootstrap=False, now=None):
        self.config, self.state, self.webhook = config, state, webhook
        self.dry_run, self.bootstrap = dry_run, bootstrap
        self.now = time.time() if now is None else now
        self.messages = []
        self.failed = False

    def message(self, kind, text):
        if not self.bootstrap:
            self.messages.append(f'【ExactCut {kind}】{clean(self.config["host_label"], 40)} {stamp(self.now)}\n{text}')

    def enqueue(self, messages):
        """限制待发队列长度，优先保留最新通知。"""
        queue = self.state.setdefault('outbox', [])
        queue.extend(messages)
        if len(queue) > 20:
            dropped = len(queue) - 20
            del queue[:dropped]
            print(f'待发消息超过 20 条，已丢弃最旧的 {dropped} 条', file=sys.stderr)

    def check(self, key, label, bad, recovered, count, detail=''):
        item = self.state.setdefault('checks', {}).setdefault(key, {})
        item['label'] = label
        item['detail'] = clean(detail)
        if self.bootstrap:
            item.update(failures=0, active=False)
            return
        if bad:
            item['failures'] = item.get('failures', 0) + 1
            item.setdefault('since', self.now)
            if item['failures'] >= count and not item.get('active'):
                item['active'] = True
                self.message('告警', f'{label}：{clean(detail)}')
        elif recovered:
            if item.get('active'):
                duration = max(0, int((self.now - item.get('since', self.now)) / 60))
                self.message('恢复', f'{label}已恢复，持续 {duration} 分钟')
            item.update(failures=0, active=False)
            item.pop('since', None)
        else:
            item['failures'] = 0
            if not item.get('active'):
                item.pop('since', None)

    def guarded(self, label, func):
        try:
            func()
        except Exception as exc:
            self.failed = True
            log_error(label, exc)

    def services(self):
        def health():
            try:
                good, detail, data = health_check(self.config)
            except Exception:
                good, detail, data = False, '健康接口请求失败', {}
            self.check('health', '本机服务', not good, good, self.config['failure_count'], detail)
            maintenance = data.get('maintenance') is True
            if maintenance != self.state.get('maintenance', False):
                self.message('提示', '维护中' if maintenance else '维护已结束')
            self.state['maintenance'] = maintenance
            release = data.get('release')
            old = self.state.get('release')
            if release is not None:
                if old is not None and old != release:
                    self.message('提示', f'版本已更新：{clean(old, 40)}→{clean(release, 40)}')
                self.state['release'] = release
        self.guarded('本机服务', health)
        def public():
            try:
                good = public_check(self.config)
            except Exception:
                good = False
            self.check('public', '公网首页', not good, good, self.config['failure_count'], '访问正常' if good else '访问异常')
        self.guarded('公网首页', public)
        def process():
            good = process_check(self.config)
            self.check('process', '服务进程', not good, good, self.config['failure_count'], '进程存在' if good else '未找到服务进程')
        self.guarded('服务进程', process)

    def events(self):
        try:
            groups = scan_events(self.config, self.state, self.bootstrap)
            status = '正常'
        except PermissionError:
            groups, status = {}, '无读权限'
        except FileNotFoundError:
            groups, status = {}, '目录不存在'
        previous = self.state.get('scan_status', '正常')
        self.state['scan_status'] = status
        if status != previous and (status == '正常' or previous == '正常'):
            self.message('恢复' if status == '正常' else '提示', '报错扫描已恢复' if status == '正常' else f'报错扫描不可用：{status}')
        records = self.state.setdefault('signatures', {})
        for key, group in groups.items():
            record = records.setdefault(key, {'pending': 0})
            record['pending'] += group['count']
            record.setdefault('first', group['first'])
            record['last'] = group['last']
            record['sample'] = group['sample']
            record['batches'] = list(dict.fromkeys(record.get('batches', []) + group['batches']))[:3]
        lines = []
        for record in records.values():
            if not record['pending'] or self.now - record.get('sent_at', -1e20) < self.config['cooldown_seconds']:
                continue
            event = record['sample']
            event_name = str(event.get('event', '未知事件'))
            route = str(event.get('route', ''))
            task = next((name for name in ('render', 'mosaic', 'scan', 'http') if name in event_name + route), event_name.split('.')[0])
            repeat = f'，期间又出现 {record["pending"]} 次' if 'sent_at' in record else ''
            details = [f'—— {clean(task, 20)} / {clean(event_name, 50)}：{record["pending"]} 次{repeat}；{stamp(record["first"], seconds=True)}—{stamp(record["last"], seconds=True)}']
            details.extend(event_context(event, self.config, self.webhook))
            batches = [x for x in record.get('batches', []) if valid_batch(x) and x != event.get('batch_id')]
            if batches:
                details.append('同组其他项目 id：' + '、'.join(batches))
            lines.append('\n'.join(details))
            record.update(pending=0, sent_at=self.now, batches=[])
            record.pop('first', None)
            record.pop('sample', None)
        if lines:
            extra = f'\n另有 {len(lines) - 10} 类' if len(lines) > 10 else ''
            self.message('告警', '报错事件合并\n' + '\n'.join(lines[:10]) + extra)
        # 无待发事件的旧签名在一天后清理，避免状态无限增长。
        self.state['signatures'] = {key: value for key, value in records.items() if value['pending'] or self.now - value.get('sent_at', self.now) < 86400}

    def run(self):
        self.services()
        def resource_checks():
            for args in resources(self.config):
                if args[0] is None:
                    self.failed = True
                    log_error(args[1], args[2])
                else:
                    self.guarded(args[1], lambda args=args: self.check(*args))
        self.guarded('资源', resource_checks)
        self.guarded('报错扫描', self.events)
        queue = self.state.setdefault('outbox', [])
        if self.bootstrap:
            queue.clear()
        self.enqueue(self.messages)
        sending_failed = False
        while queue:
            try:
                if self.dry_run:
                    print(queue[0])
                else:
                    send_message(self.webhook, queue[0])
                queue.pop(0)
            except Exception as exc:
                log_error('发送通知', exc)
                sending_failed = True
                break
        failed = self.failed or sending_failed
        previous = self.state.get('self_failures', 0)
        self.state['self_failures'] = previous + 1 if failed else 0
        if not self.bootstrap:
            if failed and self.state['self_failures'] >= 3 and not self.state.get('self_alert'):
                self.state['self_alert'] = True
                self.message('告警', '监控脚本自身异常：连续多轮检查或发送失败')
                self.enqueue([self.messages[-1]])
            elif not failed and self.state.get('self_alert'):
                self.state['self_alert'] = False
                self.message('恢复', '监控脚本自身异常已恢复')
                self.enqueue([self.messages[-1]])
            else:
                return
            try:
                if self.dry_run:
                    print(queue[-1])
                else:
                    send_message(self.webhook, queue[-1])
                queue.pop()
            except Exception as exc:
                log_error('脚本状态通知', exc)


def main(argv=None):
    # 通用参数允许出现在子命令前或后。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config')
    common.add_argument('--webhook-file', default=str(Path.home() / '.exactcut-monitor/webhook.env'))
    common.add_argument('--state-dir', default=str(Path.home() / '.exactcut-monitor'))
    common.add_argument('--dry-run', action='store_true')
    parser = argparse.ArgumentParser(description='ExactCut 线上监控', parents=[common])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'test-message', 'notify', 'status'):
        sub = commands.add_parser(name)
        for action in common._actions:
            if action.dest != 'help':
                # 子命令未指定的值不覆盖全局值。
                kwargs = {'default': argparse.SUPPRESS}
                if isinstance(action, argparse._StoreTrueAction):
                    kwargs['action'] = 'store_true'
                sub.add_argument(*action.option_strings, **kwargs)
        if name == 'run':
            sub.add_argument('--bootstrap', action='store_true')
        if name in ('notify', 'test-message'):
            sub.add_argument('--text', default='ExactCut 监控测试消息', required=name == 'notify')
    args = parser.parse_args(argv)
    try:
        config = dict(DEFAULTS)
        config_path = Path(args.config) if args.config else Path(__file__).with_name('config.json')
        if config_path.exists():
            config.update(json.loads(config_path.read_text()))
        elif args.config:
            raise FileNotFoundError('配置不存在')
        directory = Path(args.state_dir).expanduser()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        lock_fd = os.open(directory / 'monitor.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('已有一轮监控运行，跳过本轮')
                return 0
            state_path = directory / 'state.json'
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if args.command == 'status':
                print(f'主机：{clean(config["host_label"])}；报错扫描：{state.get("scan_status", "尚未运行")}；待发消息：{len(state.get("outbox", []))}；自身连续失败：{state.get("self_failures", 0)}')
                for item in state.get('checks', {}).values():
                    print(f'{clean(item.get("label", "检查项"))} {"故障" if item.get("active") else "正常或观察中"}：{clean(item.get("detail", ""))}；连续失败 {item.get("failures", 0)} 次')
                return 0
            webhook = '' if args.dry_run else webhook_value(args.webhook_file)
            monitor = Monitor(config, state, webhook, args.dry_run, getattr(args, 'bootstrap', False))
            if args.command == 'run':
                monitor.run()
                save_state(directory, state)
            else:
                monitor.message('提示', clean(args.text, 2000))
                if args.dry_run:
                    print(monitor.messages[0])
                else:
                    send_message(webhook, monitor.messages[0])
        return 0
    except Exception as exc:
        log_error('监控执行', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
