"""Episode files keyed by drama and episode, outside any batch directory."""
from __future__ import annotations

import contextvars
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .runtime import data_root, source_root
from .storage import GIB, require_space, policy
from .util import file_sha256, now_iso, write_json_atomic

EVICT_BELOW = 20 * GIB
EPISODE_BUDGET = 2 * GIB
FETCH_TOTAL_TIMEOUT = 7200
FETCH_IDLE_TIMEOUT = 300
EPISODE_TEXT = re.compile(r'EP([1-9]\d{0,2})(?!\d)')
_REUSED = contextvars.ContextVar('exactcut_source_reused', default=())
_EXCLUSIVE = contextvars.ContextVar('exactcut_exclusive_episodes', default=frozenset())
_HELD: dict[tuple[str, str], object] = {}


def check_drama(drama_id: str) -> None:
    if not isinstance(drama_id, str) or not drama_id.isdigit() or not 101050 <= int(drama_id) <= 999999:
        raise RuntimeError(f'无效剧号：{drama_id}')


def check_episode(episode: str) -> None:
    match = re.fullmatch(r'EP([1-9]\d{0,2})', episode or '')
    if not match or not 1 <= int(match.group(1)) <= 500:
        raise RuntimeError(f'无效集号：{episode}')


def reused_episodes() -> list[str]:
    return list(_REUSED.get())


@contextmanager
def episode_lock(drama_id: str, episode: str, *, blocking=True):
    check_drama(drama_id)
    check_episode(episode)
    path = source_root() / drama_id / f'{episode}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o750)
    held = _EXCLUSIVE.get()
    token = _EXCLUSIVE.set(held | {(drama_id, episode)})
    try:
        with path.open('a+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                from .stage_results import ResourceDeferred
                raise ResourceDeferred(time.time() + 1, f'正片正在使用：{episode}，稍后重试') from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _EXCLUSIVE.reset(token)


def hold_shared(drama_id: str, episode: str) -> None:
    """Shared lock held until this process exits. Review and master call this before opening the mp4."""
    key = (drama_id, episode)
    if key in _HELD:
        return
    path = source_root() / drama_id / f'{episode}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    fcntl.flock(handle, fcntl.LOCK_SH)
    _HELD[key] = handle


def _cache_key(path: Path):
    try:
        relative = Path(path).resolve().relative_to(source_root())
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative.parts) != 2 or not relative.parts[1].endswith('.mp4'):
        return None
    return relative.parts[0], Path(relative.parts[1]).stem


def hold_shared_for(path: Path) -> None:
    key = _cache_key(path)
    if key:
        hold_shared(*key)


@contextmanager
def shared_for(path: Path):
    """Shared lock for one read. Released on exit so the web process does not pin the episode."""
    key = _cache_key(path)
    if key is None or key in _HELD:
        yield
        return
    lock = source_root() / key[0] / f'{key[1]}.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _usable(path: Path) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            return False
        from .material_download import validate_downloaded_video
        validate_downloaded_video(path)
    except (FileNotFoundError, RuntimeError):
        return False
    return True


def _reserve_for_fetch() -> None:
    limits = policy()
    root = source_root()
    root.mkdir(parents=True, exist_ok=True)
    anchor = root
    while not anchor.exists():
        anchor = anchor.parent
    free = shutil.disk_usage(anchor).free
    total = source_bytes(root)
    reclaim = max(limits['source_evict_below_bytes'] - free,
                  total + limits['episode_budget_bytes'] - limits['source_cache_bytes'])
    if reclaim > 0:
        evict_oldest(required_bytes=reclaim)
    if source_bytes(root) + limits['episode_budget_bytes'] > limits['source_cache_bytes']:
        raise ValueError('正片缓存容量不足：使用中的正片不能回收，请归档已完成批次或调整缓存上限')
    require_space(root, limits['episode_budget_bytes'])


def source_bytes(root):
    return sum(path.stat().st_size for path in root.glob('*/*.mp4')
               if not path.parent.is_symlink() and not path.is_symlink() and path.is_file())


def hub_script() -> Path:
    return Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'


def _fetch_argv() -> list[str] | None:
    if os.environ.get('EXACTCUT_ALLOW_MATERIAL_FETCH') != '1':
        return None
    raw = os.environ.get('EXACTCUT_MATERIAL_FETCH_CMD', '').strip()
    if raw:
        return shlex.split(raw)
    script = hub_script()
    if script.is_file():
        return [sys.executable, str(script), 'fetch-episode']
    return None


def _run_fetch(argv: list[str], drama_id: str, episode: str, dest: Path, on_progress=None) -> None:
    from .runtime import require_material_fetch
    part = dest.with_name(dest.name + '.part')
    part.parent.mkdir(parents=True, exist_ok=True)
    success = False
    process = None
    previous = signal.getsignal(signal.SIGTERM)

    def on_term(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_term)
    try:
        require_material_fetch()
        process = subprocess.Popen(argv + [drama_id, episode, str(part)])
        code = _wait_fetch(process, part, episode, on_progress)
        if code != 0:
            raise RuntimeError(f'物料命令失败：{episode}')
        from .material_download import validate_downloaded_video
        validate_downloaded_video(part)
        digest = file_sha256(part)
        size = part.stat().st_size
        os.replace(part, dest)
        write_json_atomic(dest.with_suffix('.json'), {
            'sha256': digest, 'bytes': size, 'fetched_at': now_iso(), 'complete': True,
        })
        success = True
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if not success:
            part.unlink(missing_ok=True)


def _incoming_download(part: Path):
    """Bytes already on disk for this episode, including oss2's resumable temp file.

    oss2 writes ``<dest>.part.tmp-<random>`` and renames it to ``<dest>.part`` only
    after every part finishes. Watching only the final name treats a live download
    as idle for the whole episode.
    """
    paths = [part, *sorted(part.parent.glob(part.name + '.tmp-*'))]
    signature = []
    files = 0
    total = 0
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if not stat.st_size and path != part:
            continue
        signature.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_blocks))
        files += 1
        total += stat.st_blocks * 512
    progress = part.with_name(part.name + '.progress.json')
    try:
        stat = progress.stat()
        payload = json.loads(progress.read_text(encoding='utf-8'))
        received, expected = payload['received'], payload['total']
        if isinstance(received, int) and isinstance(expected, int) and 0 <= received <= expected and expected > 0:
            signature.append((progress.name, stat.st_mtime_ns, received))
            return tuple(signature), files, received, expected
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return tuple(signature), files, total, 0


def _wait_fetch(process, part: Path, episode: str, on_progress=None) -> int:
    """Allow slow overseas downloads while stopping after 300s without file activity."""
    elapsed = idle = 0.0
    previous = time.monotonic()
    signature = object()
    while process.poll() is None:
        now = time.monotonic()
        delta = now - previous
        previous = now
        delta = delta if delta < 10 else 2.0
        elapsed += delta
        current, files, received, total = _incoming_download(part)
        if current != signature:
            idle = 0.0
            signature = current
            if on_progress is not None:
                on_progress(episode, received, total)
        else:
            idle += delta
        if elapsed >= FETCH_TOTAL_TIMEOUT or idle >= FETCH_IDLE_TIMEOUT:
            reason = '下载总时长超限' if elapsed >= FETCH_TOTAL_TIMEOUT else '下载长时间无文件进展'
            raise TimeoutError(reason + '，已停止本次下载，可重试')
        time.sleep(2 if FETCH_IDLE_TIMEOUT >= 2 else 0.05)
    return process.returncode


def _ensure_one(drama_id: str, episode: str, on_progress=None) -> tuple[Path, bool]:
    dest = source_root() / drama_id / f'{episode}.mp4'
    if _usable(dest):
        return dest, True
    with episode_lock(drama_id, episode, blocking=False):
        # Another downloader may have published between the first check and locking.
        if _usable(dest):
            return dest, True
        _reserve_for_fetch()
        argv = _fetch_argv()
        if argv is None:
            raise RuntimeError(f'缺少剪辑必需正片：{episode}')
        _run_fetch(argv, drama_id, episode, dest, on_progress)
        if not _usable(dest):
            raise RuntimeError(f'缺少剪辑必需正片：{episode}')
        return dest, False


def ensure(drama_id: str, episodes, on_progress=None) -> dict[str, Path]:
    check_drama(drama_id)
    found: dict[str, Path] = {}
    reused: list[str] = []
    for episode in episodes:
        check_episode(episode)
        path, hit = _ensure_one(drama_id, episode, on_progress)
        found[episode] = path
        if hit:
            reused.append(episode)
    _REUSED.set(tuple(reused))
    return found


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _draft_body(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    body = payload.get('draft')
    return body if isinstance(body, dict) else payload


def _add_confirmed(keep: set[tuple[str, str]], path: Path) -> None:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return
    drama = str(payload.get('drama_id') or '').strip()
    for request in payload.get('requests') or []:
        if not isinstance(request, dict):
            continue
        for item in request.get('timeline') or []:
            if not isinstance(item, dict) or item.get('type') != 'episode':
                continue
            episode = item.get('episode')
            if isinstance(episode, str) and episode:
                keep.add((drama, episode))


def _add_timeline_text(keep: set[tuple[str, str]], path: Path) -> None:
    payload = _read_json(path)
    body = _draft_body(payload)
    drama = str(body.get('drama_id') or '').strip()
    for row in body.get('rows') or []:
        if not isinstance(row, dict):
            continue
        for match in EPISODE_TEXT.finditer(str(row.get('timeline_text') or '')):
            number = int(match.group(1))
            if 1 <= number <= 500:
                keep.add((drama, f'EP{number}'))


def _add_plots(keep: set[tuple[str, str]], path: Path) -> None:
    payload = _read_json(path)
    body = _draft_body(payload)
    drama = str(body.get('drama_id') or '').strip()
    for output in body.get('outputs') or []:
        if not isinstance(output, dict):
            continue
        for plot in output.get('plots') or []:
            if not isinstance(plot, dict):
                continue
            episode = str(plot.get('episode') or '')
            if episode:
                keep.add((drama, episode))


def named_episodes() -> set[tuple[str, str]]:
    """Episodes named by unarchived batches. Paused batches count. Archived batches do not."""
    keep: set[tuple[str, str]] = set()
    root = data_root() / '.web-state' / 'batches'
    if not root.is_dir():
        return keep
    for batch in root.iterdir():
        if not batch.is_dir():
            continue
        control = _read_json(batch / 'control.json')
        if isinstance(control, dict) and control.get('archived'):
            continue
        confirmed = batch / 'confirmed.json'
        draft = batch / 'draft.json'
        if confirmed.is_file():
            _add_confirmed(keep, confirmed)
        elif draft.is_file():
            _add_timeline_text(keep, draft)
        if draft.is_file():
            _add_plots(keep, draft)
        wanted = _read_json(batch / 'source-prefetch.json')
        if isinstance(wanted, dict):
            drama = wanted.get('drama_id')
            for episode in wanted.get('episodes', []):
                try:
                    check_drama(drama)
                    check_episode(episode)
                except (RuntimeError, TypeError):
                    continue
                keep.add((drama, episode))
    return keep


def _locked_unlink(directory: Path, episode: str, meta: Path) -> bool:
    lock = directory / f'{episode}.lock'
    video = directory / f'{episode}.mp4'
    if lock.is_symlink():
        return False
    with lock.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            video.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            return True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def evict_oldest(*, required_bytes=None) -> dict[str, int]:
    root = source_root()
    if not root.is_dir():
        return {'removed': 0}
    keep = named_episodes()
    busy = _EXCLUSIVE.get()
    removed = freed = 0
    for meta in sorted(root.glob('*/*.json'), key=lambda path: path.stat().st_mtime):
        if meta.is_symlink() or meta.parent.is_symlink():
            continue
        episode = meta.stem
        if not re.fullmatch(r'EP[1-9]\d{0,2}', episode):
            continue
        payload = _read_json(meta)
        if not isinstance(payload, dict) or payload.get('complete') is not True:
            continue
        if (meta.parent.name, episode) in keep or (meta.parent.name, episode) in busy:
            continue
        video = meta.with_suffix('.mp4')
        if video.is_symlink():
            continue
        size = video.stat().st_size if video.is_file() else 0
        if _locked_unlink(meta.parent, episode, meta):
            removed += 1
            freed += size
            if required_bytes is not None and freed >= required_bytes:
                break
    return {'removed': removed}


def cached_names(drama_id: str) -> list[str]:
    check_drama(drama_id)
    root = source_root() / drama_id
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob('EP*.mp4') if path.is_file() and not path.is_symlink())


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['evict']:
        evict_oldest()
        return 0
    raise SystemExit('用法：python -m exactcut.source_cache evict')


if __name__ == '__main__':
    raise SystemExit(main())
