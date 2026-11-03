"""Runtime probe for test layering (measurement only, never imported by product code).

Installed by the sibling ``sitecustomize.py`` when ``TEST_LAYERS_PROBE_DIR`` is set.
It adds a Python audit hook that appends one JSON line per event to
``$TEST_LAYERS_PROBE_DIR/<pid>.jsonl``. Each line is tagged with the current test
node id (``TEST_LAYERS_TESTID``, set by ``probe_plugin``); child processes inherit
the variable, so work done in subprocesses is attributed to the test that started it.

Event kinds (``k``):
  popen      [exe basename, truncated argv, tool]  tool is media/browser/node/python/other
  browser    shared in-process Chromium reused by a test (see probe_plugin)
  media      a media file was opened
  bind       socket.bind [host, port]
  connect    non-loopback socket.connect [host, port]
  dns        non-loopback getaddrinfo host
  srcwrite   file written inside TEST_LAYERS_PROBE_SRC (path relative to it)
  tmpwrite   file written under /tmp outside pytest's tmp_path
"""
import json
import os
import sys

MEDIA_SUFFIXES = ('.mp4', '.mov', '.mkv', '.wav', '.m4a', '.webm', '.aac', '.ts')
MEDIA_TOOLS = ('ffmpeg', 'ffprobe')
LOOPBACK = ('127.0.0.1', '::1', 'localhost', '0.0.0.0', '', 'None')

_state = {'file': None, 'busy': False, 'dir': None, 'src': ''}


def emit(kind, detail):
    if not _state['dir']:
        return
    if _state['file'] is None:
        path = os.path.join(_state['dir'], f'{os.getpid()}.jsonl')
        _state['file'] = open(path, 'a', buffering=1, encoding='utf-8')
    record = {'t': os.environ.get('TEST_LAYERS_TESTID', ''), 'k': kind, 'd': detail, 'p': os.getpid()}
    try:
        _state['file'].write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        pass


def tool_of(words):
    """Classify a command line (list of strings) the same way tests/conftest.py's guard does."""
    joined = ' '.join(words)
    first = os.path.basename(words[0]) if words else ''
    if first in MEDIA_TOOLS:
        return 'media'
    if 'exactcut.processing_child' in joined and any(f'"{t}"' in joined for t in MEDIA_TOOLS):
        return 'media'
    if 'run-driver' in joined or 'playwright' in joined:
        return 'browser'
    if first == 'node':
        return 'node'
    if first.startswith('python'):
        return 'python'
    return 'other'


def _popen(args):
    exe, argv = args[0], args[1]
    if isinstance(argv, (list, tuple)):
        words = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in argv]
    else:
        words = str(argv).split() if argv else []
    if not words and exe:
        words = [os.fsdecode(exe)]
    emit('popen', [os.path.basename(words[0]) if words else '', ' '.join(words)[:200], tool_of(words)])


def _open(args):
    path, mode = args[0], args[1]
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    p = os.fsdecode(path)
    if p.lower().endswith(MEDIA_SUFFIXES):
        emit('media', [p[-120:], str(mode)])
    if not (mode and any(c in str(mode) for c in 'wax+')):
        return
    src = _state['src']
    absolute = os.path.abspath(p)
    if src and absolute.startswith(src + os.sep) and '__pycache__' not in p:
        emit('srcwrite', os.path.relpath(absolute, src))
    elif p.startswith('/tmp/') and '/pytest-of-' not in p:
        emit('tmpwrite', p[:150])


def _hook(event, args):
    if _state['busy']:
        return
    _state['busy'] = True
    try:
        if event == 'subprocess.Popen':
            _popen(args)
        elif event in ('os.posix_spawn', 'os.exec', 'os.system'):
            emit(event, str(args[0])[:200])
        elif event == 'socket.bind' and isinstance(args[1], tuple):
            addr = args[1]
            emit('bind', [str(addr[0]), addr[1] if len(addr) > 1 else None])
        elif event == 'socket.connect' and isinstance(args[1], tuple) and args[1]:
            addr = args[1]
            if str(addr[0]) not in LOOPBACK:
                emit('connect', [str(addr[0]), addr[1] if len(addr) > 1 else None])
        elif event == 'socket.getaddrinfo' and str(args[0]) not in LOOPBACK:
            emit('dns', str(args[0]))
        elif event == 'open':
            _open(args)
    except Exception:
        pass
    finally:
        _state['busy'] = False


def install():
    directory = os.environ.get('TEST_LAYERS_PROBE_DIR')
    if not directory or _state['dir']:
        return
    _state['dir'] = directory
    _state['src'] = os.path.abspath(os.environ['TEST_LAYERS_PROBE_SRC']) if os.environ.get('TEST_LAYERS_PROBE_SRC') else ''
    sys.addaudithook(_hook)
