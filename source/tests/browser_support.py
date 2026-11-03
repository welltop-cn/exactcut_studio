"""One Chromium per test process; every test still gets fresh contexts.

`shared_playwright()` is a drop-in for `sync_playwright()` in UI tests:
`with shared_playwright() as p: browser = p.chromium.launch(headless=True)`.
Launch returns a view of the process-wide browser; closing it (or leaving the
`with` block) closes only the contexts this test created.
"""
import threading

_shared = {}


def _real_browser():
    if 'browser' not in _shared or not _shared['browser'].is_connected():
        from playwright.sync_api import sync_playwright
        if 'driver' not in _shared:
            _shared['driver'] = sync_playwright().start()
        _shared['browser'] = _shared['driver'].chromium.launch(headless=True)
    return _shared['browser']


def stop():
    browser, driver = _shared.pop('browser', None), _shared.pop('driver', None)
    try:
        if browser and browser.is_connected():
            browser.close()
    finally:
        if driver:
            driver.stop()



class _BrowserView:
    def __init__(self, real):
        self._real, self._contexts = real, []

    def new_context(self, **options):
        context = self._real.new_context(**options)
        self._contexts.append(context)
        return context

    def new_page(self, **options):
        return self.new_context(**options).new_page()

    def close(self):
        contexts, self._contexts = self._contexts, []
        for context in contexts:
            try:
                context.close()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Chromium:
    def __init__(self, owner):
        self._owner = owner

    def launch(self, **options):
        if options not in ({}, {'headless': True}):
            raise ValueError('shared browser only supports launch(headless=True)')
        view = _BrowserView(_real_browser())
        self._owner._views.append(view)
        return view


class _SharedPlaywright:
    def __init__(self):
        self._views = []
        self.chromium = _Chromium(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for view in self._views:
            view.close()
        return False


class _Fallback:
    """Sync Playwright is thread-bound; off the main thread use a private driver."""
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._cm = sync_playwright()
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def shared_playwright():
    import pytest
    pytest.importorskip('playwright.sync_api')
    if threading.current_thread() is not threading.main_thread():
        return _Fallback()
    return _SharedPlaywright()
