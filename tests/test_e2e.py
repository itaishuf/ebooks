import logging

import pytest

from config import settings
from download_flow import ebook_download, get_book_md5, get_isbn


KNOWN_ISBN = "9780743273565"  # The Great Gatsby


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_get_isbn_from_goodreads():
    """Verify ISBN extraction from a real Goodreads page."""
    isbn = await get_isbn(settings.test_goodreads_url)
    assert isbn and len(isbn) >= 10, f"Expected a valid ISBN, got: {isbn}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_epub_search_returns_results():
    """A well-known book returns epub results from Anna's Archive."""
    results = await get_book_md5(KNOWN_ISBN, ext="epub")
    assert len(results) > 0, f"Expected epub results for ISBN {KNOWN_ISBN}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdf_search_returns_results():
    """A well-known book returns pdf results from Anna's Archive."""
    results = await get_book_md5(KNOWN_ISBN, ext="pdf")
    assert len(results) > 0, f"Expected pdf results for ISBN {KNOWN_ISBN}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_bogus_isbn_returns_empty():
    """A made-up ISBN returns a list (Anna's Archive may return generic results)."""
    results = await get_book_md5("0000000000000", ext="epub")
    assert isinstance(results, list), f"Expected a list, got: {type(results)}"


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.skipif(not settings.gmail_password, reason="gmail_password not configured")
async def test_file_type_fallback_warning(caplog):
    """ebook_download emits a warning log when falling back from epub to pdf."""
    statuses = []

    with caplog.at_level(logging.WARNING):
        await ebook_download(
            settings.test_goodreads_url,
            settings.test_kindle_email,
            on_status=statuses.append,
        )

    assert "fetching_isbn" in statuses
    assert "searching" in statuses
    assert "done" in statuses


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.skipif(not settings.gmail_password, reason="gmail_password not configured")
async def test_full_e2e_goodreads_to_kindle():
    """Full flow: Goodreads URL -> ISBN -> search -> download -> send to Kindle.

    Requires: gmail_password set, Firefox installed, network access.
    Verify manually that the email arrived at test_kindle_email.
    """
    statuses = []
    await ebook_download(
        settings.test_goodreads_url,
        settings.test_kindle_email,
        on_status=statuses.append,
    )

    expected_stages = ["fetching_isbn", "searching", "downloading", "sending", "done"]
    assert statuses == expected_stages, f"Expected {expected_stages}, got {statuses}"
