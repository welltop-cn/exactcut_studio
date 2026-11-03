"""pytest plugin used by probe.py: tags probe events with the running test id.

Also writes ``collection.tsv`` (node id, fixtures, markers) and records a
``browser`` event whenever a test uses tests/browser_support.py's shared
Chromium, because a reused browser does not start a new driver process.
"""
import os
import sys

import pytest

import layer_probe


def pytest_collection_modifyitems(config, items):
    directory = os.environ['TEST_LAYERS_PROBE_DIR']
    worker = os.environ.get('PYTEST_XDIST_WORKER')
    if worker not in (None, 'gw0'):
        return
    with open(os.path.join(directory, 'collection.tsv'), 'w', encoding='utf-8') as f:
        for item in items:
            fixtures = ','.join(getattr(item, 'fixturenames', []))
            markers = ','.join(m.name for m in item.iter_markers())
            f.write(f'{item.nodeid}\t{fixtures}\t{markers}\n')


def _wrap_shared_browser():
    module = sys.modules.get('browser_support')
    chromium = getattr(module, '_Chromium', None)
    if chromium is None or getattr(chromium.launch, '_layer_probe', False):
        return
    original = chromium.launch

    def launch(self, **options):
        layer_probe.emit('browser', 'shared')
        return original(self, **options)

    launch._layer_probe = True
    chromium.launch = launch


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _wrap_shared_browser()
    os.environ['TEST_LAYERS_TESTID'] = item.nodeid
    yield
    os.environ['TEST_LAYERS_TESTID'] = '<between>'
