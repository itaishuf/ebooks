from pathlib import Path

import pytest

import download_flow
from config import settings
from download_flow import ebook_download


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_file_type_fallback_to_pdf(monkeypatch):
    """ebook_download falls back to pdf when no epub results are available."""
    statuses = []

    async def fake_search_aa_all_formats(_isbn, title=""):
        return {"epub": [], "pdf": ["pdf-md5"], "mobi": []}

    async def fake_download_via_libgen(_isbn: str, _md5_list: list[str]) -> Path:
        return Path("/tmp/fallback.pdf")

    def fake_send_to_kindle(_email: str, book_path: Path | None = None, book_data: bytes = b"", filename: str = ""):
        return None

    monkeypatch.setattr(download_flow, "search_aa_all_formats", fake_search_aa_all_formats)
    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_download_via_libgen)
    monkeypatch.setattr(download_flow, "send_to_kindle", fake_send_to_kindle)

    await ebook_download(
        settings.test_goodreads_url,
        settings.test_kindle_email,
        on_status=statuses.append,
    )

    expected_stages = ["fetching_isbn", "searching", "downloading", "sending", "done"]
    assert statuses == expected_stages, f"Expected {expected_stages}, got {statuses}"


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
