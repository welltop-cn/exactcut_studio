"""Changing the URL hash in the same page opens the linked project."""
import pytest
from test_source_frame_ui import frame_page

pytestmark = pytest.mark.ui


def test_hashchange_opens_listed_batch_only(frame_page):
    page = frame_page
    page.evaluate('''()=>{const other='b'.repeat(32);batchCache=[{id:current.id},{id:other}];
      window.opened=[];window.openBatch=async id=>{opened.push(id);selectedId=id}}''')
    page.evaluate("location.hash='#'+'c'.repeat(32)")  # not in the list
    page.evaluate("location.hash='#'+'b'.repeat(32)")
    page.wait_for_function("opened.length===1")
    assert page.evaluate('opened') == ['b' * 32]
    page.evaluate("location.hash='#'+'b'.repeat(32)+'x'; location.hash='#'+'b'.repeat(32)")  # already selected
    page.wait_for_timeout(100)
    assert page.evaluate('opened') == ['b' * 32]
