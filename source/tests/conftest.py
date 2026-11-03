"""Test layers, per-worker data isolation and the unit-layer guard.

Layers (see pyproject markers): unit is the default; ui opens a browser;
integration runs ffmpeg/ffprobe or real media. Explicit marks come from the
measured classification; browser fixtures are auto-marked ui as a fallback.
"""
import os
import shutil
import sys
import tempfile

# Must run before any exactcut import: modules compute data roots at import.
# Each xdist worker (or a serial run) gets a private data root so execution
# locks and queue files under .web-state are never shared between processes.
_WORKER = os.environ.get('PYTEST_XDIST_WORKER', 'main')
if 'EXACTCUT_DATA_ROOT' not in os.environ:
    _DATA_ROOT = tempfile.mkdtemp(prefix=f'exactcut-test-{_WORKER}-')
    os.environ['EXACTCUT_DATA_ROOT'] = _DATA_ROOT
else:
    _DATA_ROOT = None

import pytest  # noqa: E402

HEAVY = ('ui', 'integration')
BROWSER_FIXTURES = {'editor', 'page', 'frame_page', 'player', 'playback'}
MEDIA_TOOLS = ('ffmpeg', 'ffprobe')


def pytest_collection_modifyitems(config, items):
    for item in items:
        names = {m.name for m in item.iter_markers()}
        if not names & set(HEAVY) and BROWSER_FIXTURES & set(getattr(item, 'fixturenames', ())):
            item.add_marker(pytest.mark.ui)
            names.add('ui')
        if not names & set(HEAVY):
            item.add_marker(pytest.mark.unit)


# Unit guard: a test without ui/integration marks must not start a browser or
# media tool. The audit hook sees every Popen, including processing_child
# wrappers that carry the real ffmpeg command in their arguments.
_guard = {'active': None, 'violations': []}


def _violation(args):
    executable, argv = args[0], args[1]
    words = [os.fsdecode(a) for a in argv] if isinstance(argv, (list, tuple)) else str(argv).split()
    first = os.path.basename(os.fsdecode(executable) if executable else (words[0] if words else ''))
    if first in MEDIA_TOOLS or any(os.path.basename(w) in MEDIA_TOOLS for w in words[:1]):
        return f'{first} {" ".join(words[1:3])}'
    joined = ' '.join(words)
    if 'run-driver' in joined:
        return 'playwright browser driver'
    if 'exactcut.processing_child' in joined and any(t in joined for t in ('"ffmpeg"', '"ffprobe"')):
        return 'ffmpeg via processing_child'
    return None


def _audit(event, args):
    if event != 'subprocess.Popen' or _guard['active'] is None:
        return
    found = _violation(args)
    if found:
        _guard['violations'].append(found)
        raise RuntimeError(
            f'unit 层测试启动了 {found}；请给 {_guard["active"]} 加 @pytest.mark.integration 或 @pytest.mark.ui')


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def _unit_layer_guard(request):
    if request.node.get_closest_marker('ui') or request.node.get_closest_marker('integration'):
        yield
        return
    _guard['active'], _guard['violations'] = request.node.nodeid, []
    try:
        yield
    finally:
        _guard['active'] = None
    if _guard['violations']:
        pytest.fail(f'unit 层测试启动了重型进程：{_guard["violations"][0]}', pytrace=False)


def pytest_sessionfinish(session):
    try:
        import browser_support
        browser_support.stop()
    except Exception:
        pass
    if _DATA_ROOT:
        shutil.rmtree(_DATA_ROOT, ignore_errors=True)
