"""Drive the real persistent dispatcher in existing editor integration tests."""
import pytest
from exactcut.interactive_queue import Dispatcher


@pytest.fixture(autouse=True)
def interactive_dispatcher(request):
    # Pure validation tests need no video fixture or service.
    root=next((name for name in ('ready','fixture','workspace') if name in request.fixturenames),None)
    if root is None:
        yield;return
    request.getfixturevalue(root)
    dispatcher=Dispatcher(poll_seconds=.01)
    assert dispatcher.start()
    try:yield dispatcher
    finally:dispatcher.stop(timeout=None)
