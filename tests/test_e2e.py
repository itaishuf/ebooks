import logging
import re
from pathlib import Path

import pytest

import download_flow
from config import settings
from download_flow import ebook_download, get_book_md5, get_isbn


KNOWN_ISBN = "9780743273565"  # The Great Gatsby
BOGUS_ISBN = "0000000000000"


def _assert_md5_results(results: list[str]) -> None:
    assert isinstance(results, list), f"Expected a list, got: {type(results)}"
    assert len(results) == len(set(results)), f"Expected unique md5 results, got: {results}"
    assert all(re.fullmatch(r"[0-9a-f]{32}", md5) for md5 in results), (
        f"Expected md5-shaped results, got: {results}"
    )


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
    _assert_md5_results(results)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdf_search_returns_results():
    """A well-known book returns pdf results from Anna's Archive."""
    results = await get_book_md5(KNOWN_ISBN, ext="pdf")
    assert len(results) > 0, f"Expected pdf results for ISBN {KNOWN_ISBN}"
    _assert_md5_results(results)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_bogus_isbn_returns_empty():
    """A made-up ISBN should not return the exact same result set as a known ISBN."""
    known_results = await get_book_md5(KNOWN_ISBN, ext="epub")
    bogus_results = await get_book_md5(BOGUS_ISBN, ext="epub")
    _assert_md5_results(bogus_results)
    assert bogus_results != known_results, (
        f"Bogus ISBN {BOGUS_ISBN} unexpectedly matched the known result set for {KNOWN_ISBN}"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_file_type_fallback_warning(caplog, monkeypatch):
    """ebook_download emits a warning log when the epub search falls back to pdf."""
    statuses = []
    original_get_book_md5 = download_flow.get_book_md5

    async def fake_get_book_md5(isbn: str, ext: str = "epub") -> list[str]:
        if ext == "epub":
            return []
        return await original_get_book_md5(isbn, ext=ext)

    async def fake_download_via_libgen(_isbn: str, _md5_list: list[str]) -> Path:
        return Path("/tmp/fallback.pdf")

    def fake_send_to_kindle(_email: str, book_path: Path | None = None, book_data: bytes = b"", filename: str = ""):
        return None

    monkeypatch.setattr(download_flow, "get_book_md5", fake_get_book_md5)
    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_download_via_libgen)
    monkeypatch.setattr(download_flow, "send_to_kindle", fake_send_to_kindle)

    with caplog.at_level(logging.WARNING):
        await ebook_download(
            settings.test_goodreads_url,
            settings.test_kindle_email,
            on_status=statuses.append,
        )

    expected_stages = ["fetching_isbn", "searching", "downloading", "sending", "done"]
    assert statuses == expected_stages, f"Expected {expected_stages}, got {statuses}"
    assert f"No epub results for ISBN" in caplog.text, "Expected epub-to-pdf fallback warning in logs"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_e2e_goodreads_to_kindle(delivery_prerequisites):
    """Full flow: Goodreads URL -> ISBN -> search -> download -> send to Kindle.

    Requires: Goodreads access, a healthy Anna's Archive mirror, Firefox/geckodriver,
    gmail_password, and test_kindle_email.
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
