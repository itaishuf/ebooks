from pathlib import Path

import pytest

import download_flow
from config import settings
from download_flow import ebook_download


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_which_queries_find_9780802165176():
    """Figure out which Google Books queries surface the missing edition."""
    download_flow._GOOGLE_CACHE.clear()
    download_flow._record_google_provider_success()

    has_isbn = "9780802165176"

    queries = [
        'intitle:"Heart the Lover" inauthor:"Lily King"',
        'intitle:"Heart the Lover: A Novel" inauthor:"Lily King"',
        'intitle:Heart intitle:Lover inauthor:"Lily King"',
        'Heart the Lover: A Novel Lily King',
        'Heart the Lover A Novel Lily King',
        'Heart the Lover Lily King',
        'Heart the Lover',
    ]

    lines = []
    for query in queries:
        download_flow._GOOGLE_CACHE.clear()
        try:
            results = await download_flow._google_metadata_results(query)
            all_isbns = {i for r in results for i in r.get("isbns", [])}
            found = has_isbn in all_isbns
            lines.append(
                f"  {'FOUND' if found else 'MISS '}  {query:<65} → "
                f"{len(results)} results, isbns={sorted(all_isbns)}"
            )
        except download_flow.GoogleBooksProviderError as e:
            lines.append(f"  ERROR  {query:<65} → {e.outcome_code}")

    pytest.fail("Query coverage:\n" + "\n".join(lines))


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_aa_isbn_mismatch_does_not_reject_different_edition(monkeypatch):
    """AA download succeeds even when the MD5 page ISBN is a different edition."""
    import download_with_annas_archive as aa

    aa_results = await download_flow.search_aa_all_formats(
        "9781837265503", title="Heart the Lover", author="Lily King"
    )
    epub_hashes = aa_results.get("epub", [])
    assert epub_hashes, "No EPUB results for Heart the Lover on AA"

    known_isbns = {"9781837265503", "1837265496"}

    found_mismatch = False
    for md5 in epub_hashes[:5]:
        html = await aa._fetch_md5_page(md5)
        page_isbns = aa._page_isbns(html)
        if page_isbns and not any(
            any(isbn in p or p in isbn for isbn in known_isbns) for p in page_isbns
        ):
            found_mismatch = True
            break
    assert found_mismatch, "No AA result with mismatched ISBNs found"

    fake_file = Path("/tmp/aa_test_result.epub")
    fake_file.write_bytes(b"test")

    async def fake_ia(*_a, **_kw):
        raise aa.DownloadError("test skip")

    async def fake_slow(*_a, **_kw):
        return fake_file

    monkeypatch.setattr(aa, "_try_internet_archive", fake_ia)
    monkeypatch.setattr(aa, "_download_via_slow_partners", fake_slow)

    result = await aa.download_book_from_annas_archive(md5, isbns=known_isbns)
    assert result, "Should have downloaded successfully despite ISBN mismatch"


@pytest.mark.asyncio
async def test_file_type_fallback_to_pdf(monkeypatch, tmp_path):
    """ebook_download falls back to pdf when no epub results are available."""
    statuses = []
    fallback_pdf = tmp_path / "fallback.pdf"
    fallback_pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + b"\x00" * 1000)

    async def fake_get_book_info(_url: str) -> dict[str, str]:
        return {"isbn": "9780141439600", "title": "Test Book", "author": "Test Author"}

    async def fake_search_aa_all_formats(_isbn, title="", author=""):
        return {"epub": [], "pdf": ["pdf-md5"], "mobi": []}

    async def fake_download_via_libgen(_isbn: str, _md5_list: list[str], **kwargs) -> Path:
        return fallback_pdf

    def fake_send_to_kindle(_email: str, book_path: Path | None = None, book_data: bytes = b"", filename: str = ""):
        return None

    monkeypatch.setattr(download_flow, "get_book_info", fake_get_book_info)
    monkeypatch.setattr(download_flow, "search_aa_all_formats", fake_search_aa_all_formats)
    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_download_via_libgen)
    monkeypatch.setattr(download_flow, "send_to_kindle", fake_send_to_kindle)

    await ebook_download(
        "https://www.goodreads.com/book/show/2657.To_Kill_a_Mockingbird",
        "test@example.com",
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
