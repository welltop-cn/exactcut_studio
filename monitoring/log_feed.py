#!/usr/bin/env python3
"""由 root 定时执行的只读日志复制器；仅使用 Python 3.12 标准库。"""
import argparse
import errno
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone

MAX_LINES = 5000
PATTERN = 'events-????????-*.jsonl'


def directory(path, mode, owner=None):
    """拒绝目录本身为符号链接，后续操作固定使用已打开的目录。"""
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, *owner)
        return fd
    except BaseException:
        os.close(fd)
        raise


def regular_open(name, flags, mode=0o600, *, dir_fd=None):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('拒绝非普通文件')
    return fd


def load_state(state_fd):
    try:
        fd = regular_open('state.json', os.O_RDONLY, dir_fd=state_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        state = json.load(handle)
    # 损坏状态必须报错，不能悄悄跳过尚未复制的日志。
    if not isinstance(state, dict) or not isinstance(state.get('cursors'), dict):
        raise ValueError('游标状态无效')
    for name, cursor in state['cursors'].items():
        if (not isinstance(name, str) or not isinstance(cursor, dict)
                or type(cursor.get('inode')) is not int
                or type(cursor.get('offset')) is not int or cursor['offset'] < 0
                or type(cursor.get('skip_partial')) is not bool):
            raise ValueError('游标状态无效')
    return state


def save_state(state_fd, state):
    """临时文件同步后原子替换，并同步目录项。"""
    name = f'.state-{os.getpid()}-{time.time_ns()}'
    try:
        fd = regular_open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=state_fd)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, 'state.json', src_dir_fd=state_fd, dst_dir_fd=state_fd)
        os.fsync(state_fd)
    finally:
        try:
            os.unlink(name, dir_fd=state_fd)
        except FileNotFoundError:
            pass


def event_date(event, now):
    try:
        value = datetime.fromisoformat(event['at'].replace('Z', '+00:00'))
        # 无时区的日期按 UTC 理解，避免依赖主机本地时区。
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat().replace('-', '')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%d')


def copy_logs(source, dest_fd, state, *, bootstrap=False, dry_run=False,
              owner=None, now=None):
    now = time.time() if now is None else now
    baseline = state is None or bootstrap
    cursors = {} if state is None else dict(state['cursors'])
    present = set()
    copied = 0
    with ExitStack() as stack:
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, source_fd)
        outputs = {}
        # 扫描目录时即使达到上限仍保留尚未处理文件的旧游标。
        for name in sorted(os.listdir(source_fd)):
            if not fnmatch.fnmatchcase(name, PATTERN):
                continue
            try:
                fd = regular_open(name, os.O_RDONLY, dir_fd=source_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOENT):
                    continue
                raise
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                present.add(name)
                old = cursors.get(name, {})
                offset = old.get('offset', 0)
                skip = old.get('skip_partial', False)
                if old.get('inode') != info.st_ino or offset > info.st_size:
                    offset, skip = 0, False
                if baseline:
                    offset = info.st_size
                    if offset:
                        handle.seek(offset - 1)
                        skip = handle.read(1) != b'\n'
                else:
                    handle.seek(offset)
                    while copied < MAX_LINES:
                        line = handle.readline()
                        if not line or not line.endswith(b'\n'):
                            break
                        offset = handle.tell()
                        if skip:
                            skip = False
                            continue
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        level = event.get('level')
                        if not isinstance(level, str) or level.lower() not in ('error', 'critical'):
                            continue
                        if not dry_run:
                            target = f'events-{event_date(event, now)}-feed.jsonl'
                            if target not in outputs:
                                out_fd = regular_open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                                                      0o640, dir_fd=dest_fd)
                                out = stack.enter_context(os.fdopen(out_fd, 'ab'))
                                # 目标目录归 deploy 所有，拒绝硬链接，避免 root 把其他文件改属主或写入。
                                if os.fstat(out_fd).st_nlink != 1:
                                    raise ValueError('拒绝硬链接输出文件')
                                os.fchmod(out_fd, 0o640)
                                if owner is not None:
                                    os.fchown(out_fd, *owner)
                                outputs[target] = out
                            outputs[target].write(line)
                        copied += 1
                cursors[name] = {'inode': info.st_ino, 'offset': offset, 'skip_partial': skip}
        for out in outputs.values():
            out.flush()
            os.fsync(out.fileno())
        if outputs:
            os.fsync(dest_fd)
    return {'cursors': {name: cursor for name, cursor in cursors.items() if name in present}}, copied, baseline


def cleanup(dest_fd, retention_days, now):
    removed = 0
    for name in os.listdir(dest_fd):
        if not fnmatch.fnmatchcase(name, 'events-*-feed.jsonl'):
            continue
        info = os.stat(name, dir_fd=dest_fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode) and info.st_mtime < now - retention_days * 86400:
            os.unlink(name, dir_fd=dest_fd)
            removed += 1
    if removed:
        os.fsync(dest_fd)
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='/srv/exactcut/data/logs', help='源日志目录（只读）')
    parser.add_argument('--dest', default='/home/deploy/exactcut-monitor/feed', help='复制输出目录')
    parser.add_argument('--state-dir', default='/var/lib/exactcut-log-feed', help='root 私有状态目录')
    parser.add_argument('--owner', default='deploy', help='输出属主；空字符串关闭属主切换')
    parser.add_argument('--retention-days', type=int, default=7, help='按修改时间保留天数')
    parser.add_argument('--bootstrap', action='store_true', help='重建基线，跳过已有日志')
    parser.add_argument('--dry-run', action='store_true', help='只统计，不写输出、不清理、不保存游标')
    args = parser.parse_args(argv)
    try:
        if args.retention_days < 1:
            raise ValueError('保留天数必须大于零')
        owner = None
        if args.owner:
            account = pwd.getpwnam(args.owner)
            owner = (account.pw_uid, account.pw_gid)
        source, dest, state_dir = map(Path, (args.source, args.dest, args.state_dir))
        # 目录重叠会导致复制自身或修改源目录权限，必须在创建目录前拒绝。
        paths = [path.resolve() for path in (source, dest, state_dir)]
        for index, path in enumerate(paths):
            for other in paths[index + 1:]:
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError('源、目标和状态目录不得重叠')
        if dest.is_symlink():
            raise ValueError('拒绝目标目录符号链接')
        with ExitStack() as stack:
            state_fd = directory(state_dir, 0o700)
            stack.callback(os.close, state_fd)
            lock_fd = regular_open('lock', os.O_RDWR | os.O_CREAT, dir_fd=state_fd)
            stack.callback(os.close, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('日志复制失败：已有任务持有文件锁', file=sys.stderr)
                return 1
            state = load_state(state_fd)
            dest_fd = None
            if not args.dry_run:
                dest_fd = directory(dest, 0o750, owner)
                stack.callback(os.close, dest_fd)
            now = time.time()
            state, copied, baseline = copy_logs(source, dest_fd, state, bootstrap=args.bootstrap,
                                                dry_run=args.dry_run, owner=owner, now=now)
            removed = 0
            if not args.dry_run:
                # 数据全部同步成功才提交游标；中断最多重复，不提前消费日志。
                save_state(state_fd, state)
                removed = cleanup(dest_fd, args.retention_days, now)
            action = '演练拟复制' if args.dry_run else '复制'
            print(f'日志复制：{action} {copied} 行；基线 {"是" if baseline else "否"}；清理 {removed} 个文件')
        return 0
    except Exception as exc:
        print(f'日志复制失败：{type(exc).__name__}（请检查目录、权限、属主和状态文件）', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
