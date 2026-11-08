"""Stat-validated, worker-thread-only read snapshots, discarded after each tick."""
from contextlib import contextmanager
from pathlib import Path
import pickle
import threading

_local = threading.local()
_lock = threading.Lock()
_active = []


@contextmanager
def scope():
    previous = getattr(_local, 'cache', None)
    cache = {'entries': {}, 'dependencies': {}, 'generation': 0}
    with _lock:
        _active.append(cache)
    _local.cache = cache
    try:
        yield
    finally:
        _local.cache = previous
        with _lock:
            _active[:] = [item for item in _active if item is not cache]
            cache['entries'].clear()
            cache['dependencies'].clear()


def _drop(cache, key):
    entry = cache['entries'].pop(key, None)
    if entry is not None:
        for file in entry[0]:
            keys = cache['dependencies'][file]
            keys.discard(key)
            if not keys:
                del cache['dependencies'][file]


def invalidate(path):
    if not _active:
        return
    path = Path(path).absolute()
    with _lock:
        for cache in _active:
            cache['generation'] += 1
            for key in tuple(cache['dependencies'].get(path, ())):
                _drop(cache, key)


def _fingerprints(files):
    return tuple((s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                 for s in (file.stat() for file in files))


def _read(key, files, loader):
    cache = getattr(_local, 'cache', None)
    if cache is None:
        return loader()
    key = Path(key).absolute()
    files = tuple(Path(file).absolute() for file in files)
    try:
        before = _fingerprints(files)
    except OSError:
        # Preserve the reader's missing-file and validation behavior exactly.
        return loader()
    with _lock:
        entry = cache['entries'].get(key)
        if entry is not None and entry[1] == before:
            return entry[2]
        _drop(cache, key)
        generation = cache['generation']
    value = loader()  # Exceptions, including validation errors, are never cached.
    try:
        after = _fingerprints(files)
    except OSError:
        return value
    with _lock:
        if before == after and generation == cache['generation']:
            cache['entries'][key] = (files, after, value)
            for file in files:
                cache['dependencies'].setdefault(file, set()).add(key)
    return value


def internal(path):
    from . import material_versions as m
    value = _read(path / 'draft.json', [path / 'draft.json'], lambda: m.internal(path))
    return dict(value) if isinstance(value, dict) else value


def parent_state(path):
    from . import material_versions as m
    # batch_control flags and finished_skip fingerprints consume this object read-only.
    return _read(path, [m.registry(path), path / 'media-state.json'], lambda: m.read(path))


def job(file, loader):
    if getattr(_local, 'cache', None) is None:
        return loader()
    value = _read(file, [file], loader)
    # 20k nested JSON job copies: pickle round trips 0.050s, deepcopy 0.230s.
    # Values originate only from JSON; each consumer receives an independent copy.
    return pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
