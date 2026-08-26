import asyncio
import logging
import os
import time
from pathlib import Path

import pytest
from selenium.common.exceptions import WebDriverException

import download_flow
import download_with_annas_archive
import download_with_libgen
import service
from exceptions import BookNotFoundError, DownloadError, ManualDownloadRequiredError


class _FakeElement:
    def click(self):
        return None


class _FakeSwitchTo:
    def window(self, _handle):
        return None


class _FakeProcess:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        pass


class _FakeService:
    def __init__(self):
        self.process = _FakeProcess()


class _FakeDriver:
    def __init__(self, options=None):
        self.options = options
        self.current_window_handle = "main"
        self.switch_to = _FakeSwitchTo()
        self.visited_urls = []
        self.service = _FakeService()

    def get(self, url):
        self.visited_urls.append(url)

    def find_element(self, _by, _value):
        return _FakeElement()

    def quit(self):
        pass


def _make_valid_epub_bytes(min_bytes: int = 60_000) -> bytes:
    """Generate a minimal valid EPUB exceeding *min_bytes*.

    The EPUB spec requires mimetype as the first entry (uncompressed)
    and a META-INF/container.xml.  Padding is added to exceed the
    download-validation size threshold.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # mimetype MUST be first and uncompressed per EPUB spec
        z.writestr(
            zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0)),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("OEBPS/content.opf", "<package/>")
        # Pad to exceed validation threshold
        padding = b"\x00" * max(0, min_bytes - len(buf.getvalue()) - 200)
        z.writestr("OEBPS/chapter1.xhtml", f"<html><body>{padding.decode('latin-1')}</body></html>")
    return buf.getvalue()


def test_download_book_using_selenium_retries_until_new_file(monkeypatch, tmp_path):
    driver = _FakeDriver()
    wait_calls = []

    monkeypatch.setattr(download_with_libgen.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(download_with_libgen.webdriver, "Firefox", lambda options=None: driver)
    monkeypatch.setattr(download_with_libgen.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(download_with_libgen, "_click_download_button", lambda *_args: None)

    def fake_wait_for_download(download_dir, since, url, page_attempt):
        assert since is not None
        wait_calls.append((download_dir, url, page_attempt))
        if len(wait_calls) == 1:
            return None
        result_path = download_dir / "book.epub"
        result_path.write_bytes(_make_valid_epub_bytes())
        return result_path

    monkeypatch.setattr(download_with_libgen, "_wait_for_download", fake_wait_for_download)

    book_path = download_with_libgen.download_book_using_selenium("https://libgen.test/get.php?md5=abc")

    assert book_path.name == "book.epub"
    assert book_path.parent.parent == tmp_path
    assert book_path.parent.name.startswith("selenium-")
    assert driver.service.process.killed is True
    assert len(wait_calls) == 2
    assert wait_calls[0][0] == wait_calls[1][0]
    assert wait_calls[0][0] != tmp_path


def test_download_book_using_selenium_raises_manual_fallback(monkeypatch, tmp_path):
    driver = _FakeDriver()

    monkeypatch.setattr(download_with_libgen.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(download_with_libgen.webdriver, "Firefox", lambda options=None: driver)
    monkeypatch.setattr(download_with_libgen.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(download_with_libgen, "_click_download_button", lambda *_args: None)
    monkeypatch.setattr(download_with_libgen, "_wait_for_download", lambda *_args: None)

    with pytest.raises(ManualDownloadRequiredError) as exc:
        download_with_libgen.download_book_using_selenium("https://libgen.test/get.php?md5=def")

    assert "Selenium never detected a new downloaded file" in str(exc.value)
    assert exc.value.fallback_url == "https://libgen.test/get.php?md5=def"
    assert driver.service.process.killed is True


def test_download_book_using_selenium_converts_webdriver_exception(monkeypatch, tmp_path):
    class _ErrorDriver(_FakeDriver):
        def get(self, url):
            self.visited_urls.append(url)
            raise WebDriverException("Reached error page: about:neterror?e=connectionFailure")

    driver = _ErrorDriver()

    monkeypatch.setattr(download_with_libgen.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(download_with_libgen.webdriver, "Firefox", lambda options=None: driver)

    with pytest.raises(DownloadError, match="Failed to download book from libgen") as exc:
        download_with_libgen.download_book_using_selenium("https://libgen.test/get.php?md5=neterror")

    assert "about:neterror" in str(exc.value)


def test_wait_for_download_returns_completed_file_when_part_is_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(download_with_libgen.time, "sleep", lambda _seconds: None)
    download_dir = tmp_path / "selenium-job"
    download_dir.mkdir()

    completed_path = download_dir / "book.epub"
    completed_path.write_bytes(b"finished")
    partial_path = download_dir / "book.epub.part"
    partial_path.write_bytes(b"partial")
    os.utime(completed_path, (100.0, 100.0))
    os.utime(partial_path, (200.0, 200.0))

    result = download_with_libgen._wait_for_download(
        download_dir,
        50.0,
        "https://libgen.test/get.php?md5=ghi",
        1,
    )

    assert result == completed_path


def test_wait_for_download_ignores_files_outside_session_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(download_with_libgen.time, "sleep", lambda _seconds: None)
    parent_download_dir = tmp_path
    session_download_dir = parent_download_dir / "selenium-job"
    session_download_dir.mkdir()

    unrelated_root_file = parent_download_dir / "other-job.pdf"
    unrelated_root_file.write_bytes(b"other job")
    os.utime(unrelated_root_file, (300.0, 300.0))
    completed_path = session_download_dir / "book.epub"
    completed_path.write_bytes(b"finished")
    os.utime(completed_path, (200.0, 200.0))

    result = download_with_libgen._wait_for_download(
        session_download_dir,
        50.0,
        "https://libgen.test/get.php?md5=jkl",
        1,
    )

    assert result == completed_path


def test_send_to_kindle_logs_recipient_email_once(monkeypatch, tmp_path):
    class _FakeSMTP:
        def __init__(self, _host, _port):
            self.logged_in = None
            self.sent_to = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def login(self, username, password):
            self.logged_in = (username, password)

        def send_message(self, msg):
            self.sent_to = msg["To"]

    logged_messages = []

    book_path = tmp_path / "book.epub"
    book_path.write_bytes(b"ebook-bytes")

    monkeypatch.setattr(download_flow.settings, "gmail_account", "sender@example.com")
    monkeypatch.setattr(download_flow.settings, "gmail_password", "gmail-password")
    monkeypatch.setattr(download_flow.smtplib, "SMTP_SSL", _FakeSMTP)

    def fake_info(message, *args, **kwargs):
        logged_messages.append((message, kwargs))

    monkeypatch.setattr(download_flow.logger, "info", fake_info)

    download_flow.send_to_kindle("reader@example.com", book_path)

    matching = [entry for entry in logged_messages if "reader@example.com" in entry[0]]
    assert matching == [
        ("Sending ebook to Kindle email reader@example.com", {"extra": {"allow_email_log": True}})
    ]


@pytest.mark.asyncio
async def test_run_job_maps_manual_fallback_error_to_job():
    service.jobs.clear()
    job_id = service._make_job()

    async def failing_coro():
        raise ManualDownloadRequiredError(
            "All download attempts failed for ISBN 123",
            fallback_url="https://libgen.test/get.php?md5=123",
            fallback_message="Try downloading the file manually from LibGen.",
        )

    await service._run_job(job_id, failing_coro())

    assert service.jobs[job_id]["status"] == "error"
    assert service.jobs[job_id]["error"] == "Automatic download failed after trying the available sources."
    assert service.jobs[job_id]["fallback"] == {
        "url": "https://libgen.test/get.php?md5=123",
        "message": "Try downloading the file manually from LibGen.",
    }
    assert service.jobs[job_id]["error_code"] == "manual_download_available"


def test_public_job_payload_exposes_only_safe_recovery_fields():
    service.jobs.clear()
    job_id = service._make_job(source="annas_archive")
    service.jobs[job_id].update(
        current_source="annas_archive",
        format="epub",
        attempt_summary={"downloading": 2},
        owner_email="private@example.com",
        client_ip="192.0.2.10",
    )

    payload = service._public_job_payload(service.jobs[job_id])

    assert payload["current_source"] == "annas_archive"
    assert payload["format"] == "epub"
    assert payload["attempt_summary"] == {"downloading": 2}
    assert "owner_email" not in payload
    assert "client_ip" not in payload


@pytest.mark.asyncio
async def test_run_job_terminates_stalled_stage(monkeypatch):
    service.jobs.clear()
    job_id = service._make_job()
    monkeypatch.setattr(service.settings, "job_stage_timeout_seconds", 0)

    async def stalled_coro():
        await asyncio.sleep(60)

    await service._run_job(job_id, stalled_coro())

    assert service.jobs[job_id]["status"] == "error"
    assert service.jobs[job_id]["error_code"] == "stage_timeout"


@pytest.mark.asyncio
async def test_ebook_download_recovers_from_epub_failure_without_fallback_leak(monkeypatch):
    service.jobs.clear()
    job_id = service._make_job()
    statuses = []
    sent_books = []
    downloaded_paths = [ManualDownloadRequiredError(
        "Automatic download failed because Selenium never detected a new downloaded file.",
        fallback_url="https://libgen.test/get.php?md5=epub",
        fallback_message="Try downloading the file manually from LibGen.",
    ), Path("/tmp/final.pdf")]

    async def fake_get_book_info(_url):
        return {"isbn": "isbn-123", "title": "Test Book", "author": "Test Author"}

    async def fake_search_aa_all_formats(_isbn, title="", author=""):
        return {"epub": ["epub-md5"], "pdf": ["pdf-md5"], "mobi": []}

    async def fake_download_via_libgen(_isbn, _md5_list, **kwargs):
        result = downloaded_paths.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def fake_send_to_kindle(_email, book_path=None, book_data=b"", filename=""):
        sent_books.append(book_path or filename or book_data)

    monkeypatch.setattr(download_flow, "get_book_info", fake_get_book_info)
    monkeypatch.setattr(download_flow, "search_aa_all_formats", fake_search_aa_all_formats)
    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_download_via_libgen)
    monkeypatch.setattr(download_flow, "send_to_kindle", fake_send_to_kindle)

    def on_status(status):
        statuses.append(status)
        service.jobs[job_id]["status"] = status

    await service._run_job(
        job_id,
        download_flow.ebook_download(
            "https://goodreads.test/book",
            "reader@example.com",
            on_status=on_status,
        ),
    )

    assert service.jobs[job_id]["status"] == "done"
    assert service.jobs[job_id]["fallback"] is None
    assert statuses == ["fetching_isbn", "searching", "downloading", "sending", "done"]
    assert sent_books == [Path("/tmp/final.pdf")]


@pytest.mark.asyncio
async def test_ebook_download_by_md5_surfaces_manual_fallback(monkeypatch):
    async def fake_download_via_libgen(_isbn, _md5_list, **kwargs):
        raise ManualDownloadRequiredError(
            "Automatic download failed because Selenium never detected a new downloaded file.",
            fallback_url="https://libgen.test/get.php?md5=md5",
            fallback_message="Try downloading the file manually from LibGen.",
        )

    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_download_via_libgen)

    with pytest.raises(ManualDownloadRequiredError) as exc:
        await download_flow.ebook_download_by_md5(
            "0123456789abcdef0123456789abcdef",
            "reader@example.com",
        )

    assert exc.value.fallback_url == "https://libgen.test/get.php?md5=md5"


# ---------------------------------------------------------------------------
# AA ISBN validation tests
# ---------------------------------------------------------------------------

_ISBN13 = "9780670016907"
_ISBN10 = "0670016907"
_OTHER_ISBN = "9780140449136"

_HTML_WITH_ISBN13 = f"<html><body>ISBN: {_ISBN13}</body></html>"
_HTML_WITH_ISBN10 = f"<html><body>ISBN: {_ISBN10}</body></html>"
_HTML_WITH_WRONG_ISBN = f"<html><body>ISBN: {_OTHER_ISBN}</body></html>"
_HTML_NO_ISBN = "<html><body>No metadata here.</body></html>"


def test_page_isbns_finds_isbn13():
    assert download_with_annas_archive._page_isbns(_HTML_WITH_ISBN13) == [_ISBN13]


def test_page_isbns_finds_isbn10():
    assert download_with_annas_archive._page_isbns(_HTML_WITH_ISBN10) == [_ISBN10]


def test_page_isbns_empty_on_no_isbn():
    assert download_with_annas_archive._page_isbns(_HTML_NO_ISBN) == []


def test_slow_partner_selection_prefers_recent_success_and_skips_cooldown(monkeypatch):
    urls = [
        "https://annas.example/slow_download/first",
        "https://annas.example/slow_download/second",
        "https://annas.example/slow_download/third",
    ]
    download_with_annas_archive._PARTNER_HEALTH.clear()
    download_with_annas_archive._PARTNER_HEALTH["/slow_download/second"] = {
        "successes": 3,
        "failures": 0,
        "cooldown_until": 0.0,
    }
    download_with_annas_archive._PARTNER_HEALTH["/slow_download/first"] = {
        "successes": 0,
        "failures": 2,
        "cooldown_until": 200.0,
    }

    selected = download_with_annas_archive._rank_slow_partner_urls(urls, now=100.0)

    assert selected == [urls[1], urls[2]]


@pytest.mark.asyncio
async def test_slow_partner_attempts_are_bounded_and_classified(monkeypatch, tmp_path):
    urls = [
        "https://annas.example/slow_download/one",
        "https://annas.example/slow_download/two",
        "https://annas.example/slow_download/three",
    ]
    attempts = []
    download_with_annas_archive._PARTNER_HEALTH.clear()
    monkeypatch.setattr(download_with_annas_archive.settings, "download_dir", str(tmp_path))

    async def fake_trawl(_md5, slow_url):
        attempts.append(slow_url)
        raise download_with_annas_archive.AnnaPartnerError("trawl_aa_ddg_not_cleared")

    monkeypatch.setattr(download_with_annas_archive, "_download_via_trawl_browser", fake_trawl)

    with pytest.raises(download_with_annas_archive.AnnaPartnerError, match="trawl_aa_ddg_not_cleared"):
        await download_with_annas_archive._download_via_slow_partners("deadbeef", urls)

    assert attempts == urls


@pytest.mark.asyncio
async def test_slow_partner_sanitizes_download_filename(monkeypatch, tmp_path):
    download_with_annas_archive._PARTNER_HEALTH.clear()
    monkeypatch.setattr(download_with_annas_archive.settings, "download_dir", str(tmp_path))

    async def fake_trawl(_md5, slow_url):
        return _make_valid_epub_bytes(), "../../evil.epub"

    monkeypatch.setattr(download_with_annas_archive, "_download_via_trawl_browser", fake_trawl)

    path = await download_with_annas_archive._download_via_slow_partners(
        "deadbeef", ["https://annas.example/slow_download/one"]
    )

    assert path == tmp_path / "aa-deadbeef" / "evil.epub"
    assert len(path.read_bytes()) > 50_000  # valid EPUB
    assert not (tmp_path / "evil.epub").exists()


@pytest.mark.asyncio
async def test_trawl_browser_rejects_oversized_response(monkeypatch, tmp_path):
    download_with_annas_archive._PARTNER_HEALTH.clear()
    monkeypatch.setattr(download_with_annas_archive.settings, "trawl_url", "http://trawl:8191")
    monkeypatch.setattr(download_with_annas_archive, "_MAX_DOWNLOAD_BYTES", 10)

    class FakeResponse:
        status = 200
        headers = {"X-AA-Filename": "book.epub"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self) -> bytes:
            return b"x" * 20

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        # aiohttp's session.post() returns an async context manager (not a coroutine).
        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(download_with_annas_archive.aiohttp, "ClientSession", lambda *_a, **_k: FakeSession())

    with pytest.raises(download_with_annas_archive.AnnaPartnerError, match="file_validation_failed"):
        await download_with_annas_archive._download_via_trawl_browser(
            "deadbeef", "https://annas.example/slow_download/one"
        )


@pytest.mark.asyncio
async def test_slow_partner_emits_status_per_attempt(monkeypatch, tmp_path):
    download_with_annas_archive._PARTNER_HEALTH.clear()
    monkeypatch.setattr(download_with_annas_archive.settings, "download_dir", str(tmp_path))

    emits = []

    async def fake_trawl(_md5, slow_url):
        if slow_url.endswith(("one", "two")):
            raise download_with_annas_archive.AnnaPartnerError("trawl_aa_no_d3_link")
        return _make_valid_epub_bytes(), "book.epub"

    def on_status(status, **details):
        emits.append((status, details))

    monkeypatch.setattr(download_with_annas_archive, "_download_via_trawl_browser", fake_trawl)

    path = await download_with_annas_archive._download_via_slow_partners(
        "deadbeef",
        [
            "https://annas.example/slow_download/one",
            "https://annas.example/slow_download/two",
            "https://annas.example/slow_download/three",
        ],
        on_status=on_status,
    )

    assert path.name == "book.epub"
    assert emits == [
        ("downloading", {"source": "annas_archive", "attempt": 1, "total": 3}),
        ("downloading", {"source": "annas_archive", "attempt": 2, "total": 3}),
        ("downloading", {"source": "annas_archive", "attempt": 3, "total": 3}),
    ]


def test_partner_health_evicts_entries_to_stay_bounded(monkeypatch):
    download_with_annas_archive._PARTNER_HEALTH.clear()
    monkeypatch.setattr(download_with_annas_archive, "_PARTNER_HEALTH_MAX_ENTRIES", 3)
    future = download_with_annas_archive.time.monotonic() + 3600
    for key in ("/a", "/b", "/c"):
        download_with_annas_archive._PARTNER_HEALTH[key] = {
            "successes": 0,
            "failures": 0,
            "cooldown_until": future,
        }

    download_with_annas_archive._record_partner_outcome(
        "https://annas.example/slow_download/d", success=False
    )

    assert set(download_with_annas_archive._PARTNER_HEALTH) == {"/b", "/c", "/slow_download/d"}
    assert int(download_with_annas_archive._PARTNER_HEALTH["/slow_download/d"]["failures"]) == 1


@pytest.mark.asyncio
async def test_download_book_from_annas_archive_skips_wrong_isbn(monkeypatch, tmp_path):
    """MD5 page with a different ISBN proceeds past the check (different edition is ok)."""
    async def fake_fetch(_md5):
        return _HTML_WITH_WRONG_ISBN

    monkeypatch.setattr(download_with_annas_archive, "_fetch_md5_page", fake_fetch)

    reached_ia = False

    async def fake_try_ia(_md5, _html):
        nonlocal reached_ia
        reached_ia = True
        out = tmp_path / "book.epub"
        out.write_bytes(b"epub-data")
        return out

    monkeypatch.setattr(download_with_annas_archive, "_try_internet_archive", fake_try_ia)

    await download_with_annas_archive.download_book_from_annas_archive(
        "deadbeef", isbns={_ISBN13}
    )
    assert reached_ia, "Should have proceeded past ISBN check to Internet Archive"


@pytest.mark.asyncio
async def test_download_book_from_annas_archive_allows_matching_isbn13(monkeypatch, tmp_path):
    """MD5 page whose ISBN-13 matches the target proceeds past the check."""
    async def fake_fetch(_md5):
        return _HTML_WITH_ISBN13

    async def fake_try_ia(_md5, _html):
        out = tmp_path / "book.epub"
        out.write_bytes(b"epub-data")
        return out

    monkeypatch.setattr(download_with_annas_archive, "_fetch_md5_page", fake_fetch)
    monkeypatch.setattr(download_with_annas_archive, "_try_internet_archive", fake_try_ia)

    result = await download_with_annas_archive.download_book_from_annas_archive(
        "deadbeef", isbns={_ISBN13}
    )
    assert result.name == "book.epub"


@pytest.mark.asyncio
async def test_search_books_uses_google_books_metadata(monkeypatch, caplog):
    async def fake_fetch(_query: str) -> list[dict]:
        return [
            {
                "volumeInfo": {
                    "title": "Example Book",
                    "authors": ["Example Author"],
                    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "978-0-123456-47-2"}],
                    "imageLinks": {"thumbnail": "http://images.example/book.jpg"},
                    "language": "en",
                }
            }
        ]

    monkeypatch.setattr(download_flow, "_fetch_google_books_search", fake_fetch)
    caplog.set_level(logging.INFO, logger="download_flow")

    results = await download_flow.search_books("Example Book")

    assert results == [
        {
            "title": "Example Book",
            "author": "Example Author",
            "isbn": "9780123456472",
            "isbns": ["9780123456472"],
            "cover_url": "https://images.example/book.jpg",
            "md5": "",
            "format": "",
            "language": "en",
            "source": "google_books",
        }
    ]
    assert "Google Books ISBN decisions volumes=1 selected=1" in caplog.text
    assert "language_codes=['en']" in caplog.text


def test_google_cover_url_prefers_largest_supported_image():
    assert download_flow._google_cover_url(
        {
            "smallThumbnail": "https://images.example/small.jpg",
            "thumbnail": "https://images.example/thumbnail.jpg",
            "large": "http://images.example/large.jpg#fragment",
        }
    ) == "https://images.example/large.jpg"


@pytest.mark.parametrize(
    "image_links",
    [
        {"thumbnail": "relative-cover.jpg"},
        {"thumbnail": "ftp://images.example/cover.jpg"},
        {"thumbnail": "https://user:password@images.example/cover.jpg"},
        {"thumbnail": "http://127.0.0.1/cover.jpg"},
        {"thumbnail": "https://covers.local/cover.jpg"},
        {"thumbnail": ["not-a-url"]},
    ],
)
def test_google_cover_url_rejects_unsafe_or_malformed_values(image_links):
    assert download_flow._google_cover_url(image_links) == ""


def test_google_edition_isbns_from_cache():
    """Collect all ISBNs from cached volumes matching title and language."""
    now = time.monotonic()
    expiry = now + 60
    result_entry = {"title": "Heart the Lover", "author": "Lily King"}
    volumes = [
        {"volumeInfo": {"title": "Heart the Lover", "language": "en",
         "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781837265503"}]}},
        {"volumeInfo": {"title": "Heart the Lover", "language": "en",
         "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781955765121"},
                                  {"type": "ISBN_10", "identifier": "195576512X"}]}},
        {"volumeInfo": {"title": "Heart the Lover", "language": "fr",
         "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780000000001"}]}},
    ]
    download_flow._GOOGLE_CACHE.clear()
    download_flow._GOOGLE_CACHE["test_query_heart"] = (expiry, [result_entry], volumes)

    en_isbns = download_flow._google_edition_isbns("Heart the Lover", language="en")
    assert en_isbns == {"9781837265503", "9781955765121", "195576512X"}

    fr_isbns = download_flow._google_edition_isbns("Heart the Lover", language="fr")
    assert fr_isbns == {"9780000000001"}

    no_lang = download_flow._google_edition_isbns("Heart the Lover")
    assert no_lang == {"9781837265503", "9781955765121", "9780000000001", "195576512X"}

    empty = download_flow._google_edition_isbns("Nonexistent", language="en")
    assert empty == set()

    download_flow._GOOGLE_CACHE.clear()



@pytest.mark.asyncio
async def test_google_cover_search_retries_transient_provider_failure(monkeypatch):
    statuses = [503, 200]
    calls = 0

    class FakeResponse:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return {"items": []}

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, _url, **_kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse(statuses.pop(0))

    async def no_sleep(_seconds: float):
        return None

    monkeypatch.setattr(download_flow.settings, "google_books_api_key", "test-key")
    monkeypatch.setattr(download_flow.settings, "google_books_cover_request_attempts", 2)
    monkeypatch.setattr(download_flow.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(download_flow.asyncio, "sleep", no_sleep)

    assert await download_flow._fetch_google_books_cover_search("Example Book", "Example Author") == []
    assert calls == 2


def test_carousel_uses_google_result_cover_url_contract():
    page = Path(__file__).parents[1] / "static" / "index.html"

    assert '<img :src="r.cover_url" :alt="r.title"' in page.read_text()


_AA_METADATA_HTML = """
<div class="js-aarecord-list-outer">
  <div class="flex">
    <a href="/md5/0123456789abcdef0123456789abcdef"></a>
    <img src="http://covers.example/hebrew-book.jpg#preview" alt="">
    <div>
      <a class="text-lg">הביתה</a>
      <a class="text-sm">אסף ענברי</a>
      <div class="text-gray-800 font-semibold text-sm">Hebrew [he] · EPUB · 0.6MB</div>
    </div>
  </div>
</div>
<div class="js-aarecord-list-outer">
  <div class="flex">
    <a href="/md5/0123456789abcdef0123456789abcdef"></a>
    <div>
      <a class="text-lg">Duplicate</a>
      <a class="text-sm">Duplicate Author</a>
      <div class="text-gray-800 font-semibold text-sm">English [en] · EPUB</div>
    </div>
  </div>
</div>
<div class="js-aarecord-list-outer">
  <div class="flex">
    <a href="/md5/fedcba98765432100123456789abcdef"></a>
    <div>
      <a class="text-lg">No Format</a>
      <a class="text-sm">Unknown</a>
    </div>
  </div>
</div>
"""


def test_parse_aa_metadata_results_returns_unique_supported_records():
    assert download_flow._parse_aa_metadata_results(_AA_METADATA_HTML) == [
        {
            "title": "הביתה",
            "author": "אסף ענברי",
            "isbn": "",
            "cover_url": "https://covers.example/hebrew-book.jpg",
            "md5": "0123456789abcdef0123456789abcdef",
            "format": "epub",
            "language": "he",
            "source": "annas_archive",
        }
    ]


def test_parse_aa_metadata_results_reads_all_cards_in_a_container():
    html = """
    <div class="js-aarecord-list-outer">
      <div class="flex">
        <a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"></a>
        <img src="https://covers.example/first.jpg">
        <div>
          <a class="text-lg">First book</a>
          <a class="text-sm">First author</a>
          <div class="text-gray-800 font-semibold text-sm">English [en] · EPUB</div>
        </div>
      </div>
      <div class="flex">
        <a href="/md5/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"></a>
        <img src="https://covers.example/second.jpg">
        <div>
          <a class="text-lg">Second book</a>
          <a class="text-sm">Second author</a>
          <div class="text-gray-800 font-semibold text-sm">Hebrew [he] · PDF</div>
        </div>
      </div>
    </div>
    """

    results = download_flow._parse_aa_metadata_results(html)

    assert [(result["title"], result["cover_url"]) for result in results] == [
        ("First book", "https://covers.example/first.jpg"),
        ("Second book", "https://covers.example/second.jpg"),
    ]


def test_parse_aa_metadata_results_rejects_unsafe_cover_url():
    html = _AA_METADATA_HTML.replace(
        "http://covers.example/hebrew-book.jpg#preview",
        "http://127.0.0.1/private-cover.jpg",
    )

    assert download_flow._parse_aa_metadata_results(html)[0]["cover_url"] == ""


@pytest.mark.asyncio
async def test_search_books_falls_back_to_aa_when_google_has_no_isbn(monkeypatch):
    async def fake_google_search(_query: str) -> list[dict]:
        return [{"volumeInfo": {"title": "No ISBN", "industryIdentifiers": []}}]

    async def fake_aa_search(query: str) -> list[dict]:
        assert query == "הביתה אסף ענברי"
        return [{"title": "הביתה", "md5": "0123456789abcdef0123456789abcdef"}]

    monkeypatch.setattr(download_flow, "_fetch_google_books_search", fake_google_search)
    monkeypatch.setattr(download_flow, "_search_aa_metadata", fake_aa_search)

    assert await download_flow.search_books("הביתה אסף ענברי") == [
        {"title": "הביתה", "md5": "0123456789abcdef0123456789abcdef"}
    ]


@pytest.mark.parametrize(
    ("query", "title", "author", "expected"),
    [
        ("The Dune", "Dune", "Frank Herbert", True),
        ("Frank Herbert", "Children of Dune", "Frank Herbert", True),
        ("Dune Frank Herbert", "Dune", "Frank Herbert", True),
        ("Frank Herbert Dune", "Dune", "Frank Herbert", True),
        ("הַבַּיְתָה!", "הביתה", "אסף ענברי", True),
        ("the and of", "Unrelated", "Nobody", False),
        ("art", "Earth", "Someone", False),
        ("נתן עמוס", "על האינדיאנים", "מגד עמוס רון נתן", True),
        ("   ", "Dune", "Frank Herbert", False),
    ],
)
def test_aa_result_relevance_matches_meaningful_title_or_author_tokens(
    query, title, author, expected
):
    assert download_flow._aa_result_is_relevant(query, title, author) is expected


@pytest.mark.asyncio
async def test_aa_metadata_filter_runs_before_result_limit_and_preserves_download_fields(monkeypatch):
    records = []
    for index in range(20):
        records.append(
            f"""
            <div class="flex">
              <a href="/md5/{index:032x}"></a>
              <div>
                <a class="text-lg">Unrelated volume {index}</a>
                <a class="text-sm">Other author</a>
                <div class="text-gray-800 font-semibold text-sm">English [en] · EPUB</div>
              </div>
            </div>
            """
        )
    records.append(
        """
        <div class="flex">
          <a href="/md5/ffffffffffffffffffffffffffffffff"></a>
          <div>
            <a class="text-lg">Target book</a>
            <a class="text-sm">Target author</a>
            <div class="text-gray-800 font-semibold text-sm">Hebrew [he] · PDF</div>
          </div>
        </div>
        """
    )
    html = f'<div class="js-aarecord-list-outer">{"".join(records)}</div>'

    async def fake_fetch(_path: str) -> str:
        return html

    monkeypatch.setattr(download_flow.settings, "annas_archive_url", "https://annas.example")
    monkeypatch.setattr("mirror_selector.fetch_aa_html", fake_fetch)

    results = await download_flow._search_aa_metadata("Target author")

    assert results == [
        {
            "title": "Target book",
            "author": "Target author",
            "isbn": "",
            "cover_url": "",
            "md5": "ffffffffffffffffffffffffffffffff",
            "format": "pdf",
            "language": "he",
            "source": "annas_archive",
        }
    ]


def _anna_result(**overrides) -> dict:
    result = {
        "title": "Example Book",
        "author": "Example Author",
        "isbn": "",
        "cover_url": "",
        "md5": "0123456789abcdef0123456789abcdef",
        "format": "epub",
        "language": "en",
        "source": "annas_archive",
    }
    result.update(overrides)
    return result


def _google_volume(
    *,
    title: str = "Example Book",
    author: str = "Example Author",
    language: str = "en",
    cover_url: str = "https://books.google.com/books/content?id=example",
) -> dict:
    return {
        "volumeInfo": {
            "title": title,
            "authors": [author],
            "language": language,
            "imageLinks": {"thumbnail": cover_url},
        }
    }


@pytest.mark.asyncio
async def test_anna_cover_enrichment_preserves_download_fields(monkeypatch):
    download_flow._GOOGLE_CACHE.clear()
    download_flow._GOOGLE_COVER_CACHE.clear()

    async def fake_cover_search(_title: str, _author: str) -> list[dict]:
        return [_google_volume()]

    original = _anna_result()
    monkeypatch.setattr(download_flow, "_fetch_google_books_cover_search", fake_cover_search)

    results = await download_flow._enrich_anna_missing_covers([original], "Example Book")

    assert original["cover_url"] == ""
    assert results == [
        {
            **original,
            "cover_url": "https://books.google.com/books/content?id=example",
        }
    ]


@pytest.mark.asyncio
async def test_anna_cover_enrichment_reuses_primary_google_response(monkeypatch):
    download_flow._GOOGLE_CACHE.clear()
    download_flow._GOOGLE_COVER_CACHE.clear()

    async def fake_primary_search(_query: str) -> list[dict]:
        return [_google_volume()]

    async def fake_aa_search(_query: str) -> list[dict]:
        return [_anna_result()]

    async def cover_search_must_not_run(_title: str, _author: str) -> list[dict]:
        raise AssertionError("primary Google response should provide the cover")

    monkeypatch.setattr(download_flow, "_fetch_google_books_search", fake_primary_search)
    monkeypatch.setattr(download_flow, "_search_aa_metadata", fake_aa_search)
    monkeypatch.setattr(download_flow, "_fetch_google_books_cover_search", cover_search_must_not_run)

    results = await download_flow.search_books("Example Book")

    assert results[0]["cover_url"] == "https://books.google.com/books/content?id=example"
    assert results[0]["md5"] == "0123456789abcdef0123456789abcdef"
    assert results[0]["source"] == "annas_archive"


@pytest.mark.asyncio
async def test_anna_cover_enrichment_skips_existing_cover_and_uses_negative_cache(monkeypatch):
    download_flow._GOOGLE_COVER_CACHE.clear()
    calls = 0

    async def fake_cover_search(_title: str, _author: str) -> list[dict]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(download_flow, "_fetch_google_books_cover_search", fake_cover_search)

    covered = _anna_result(cover_url="https://archive.org/cover.jpg")
    missing = _anna_result(title="No cover")
    first = await download_flow._enrich_anna_missing_covers([covered, missing], "No cover")
    second = await download_flow._enrich_anna_missing_covers([missing], "No cover")

    assert first[0] == covered
    assert first[1]["cover_url"] == ""
    assert second[0]["cover_url"] == ""
    assert calls == 2


@pytest.mark.asyncio
async def test_anna_cover_enrichment_rejects_mismatched_or_unsafe_google_candidates(monkeypatch):
    download_flow._GOOGLE_CACHE.clear()
    download_flow._GOOGLE_COVER_CACHE.clear()
    mismatched = [
        _google_volume(author="Different Author"),
        _google_volume(language="he"),
        _google_volume(cover_url="https://untrusted.example/cover.jpg"),
    ]

    async def fake_cover_search(_title: str, _author: str) -> list[dict]:
        return mismatched

    monkeypatch.setattr(download_flow, "_fetch_google_books_cover_search", fake_cover_search)

    assert await download_flow._enrich_anna_missing_covers([_anna_result()], "Example Book") == [
        _anna_result()
    ]


@pytest.mark.asyncio
async def test_anna_cover_enrichment_bounds_lookup_attempts_and_provider_failures(monkeypatch, caplog):
    download_flow._GOOGLE_COVER_CACHE.clear()
    download_flow._GOOGLE_BREAKER.update(consecutive_failures=0, open_until=0.0)
    calls = 0

    async def fake_cover_search(_title: str, _author: str) -> list[dict]:
        nonlocal calls
        calls += 1
        raise download_flow.GoogleBooksProviderError("timeout")

    monkeypatch.setattr(download_flow.settings, "google_books_cover_lookup_limit", 2)
    monkeypatch.setattr(download_flow, "_fetch_google_books_cover_search", fake_cover_search)
    caplog.set_level(logging.INFO, logger="download_flow")
    results = await download_flow._enrich_anna_missing_covers(
        [_anna_result(title=f"Book {index}") for index in range(5)],
        "Book",
    )

    assert calls == 2
    assert all(result["cover_url"] == "" for result in results)
    assert all(result["md5"] == "0123456789abcdef0123456789abcdef" for result in results)
    assert "provider_outcomes={'timeout': 2}" in caplog.text
    assert "books.google.com/books/content" not in caplog.text
    download_flow._GOOGLE_BREAKER.update(consecutive_failures=0, open_until=0.0)


def test_google_cover_cache_is_bounded_and_expires(monkeypatch):
    download_flow._GOOGLE_COVER_CACHE.clear()
    monkeypatch.setattr(download_flow.settings, "google_books_cover_cache_max_entries", 1)
    monkeypatch.setattr(download_flow.settings, "google_books_cover_cache_ttl_seconds", 1)
    first_key = ("first", "", "en")
    second_key = ("second", "", "en")

    download_flow._cache_google_cover(first_key, "", now=10.0)
    download_flow._cache_google_cover(second_key, "https://books.google.com/books/content?id=second", now=11.0)

    assert first_key not in download_flow._GOOGLE_COVER_CACHE
    assert download_flow._get_cached_google_cover(second_key, now=11.5)
    assert download_flow._get_cached_google_cover(second_key, now=12.1) is None


@pytest.mark.asyncio
async def test_search_books_uses_cached_google_results(monkeypatch):
    calls = 0
    download_flow._GOOGLE_CACHE.clear()
    download_flow._GOOGLE_BREAKER.update(consecutive_failures=0, open_until=0.0)

    async def fake_google_search(_query: str) -> list[dict]:
        nonlocal calls
        calls += 1
        return [
            {
                "volumeInfo": {
                    "title": "Dune",
                    "authors": ["Frank Herbert"],
                    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441172719"}],
                }
            }
        ]

    monkeypatch.setattr(download_flow, "_fetch_google_books_search", fake_google_search)

    first = await download_flow.search_books("Dune")
    second = await download_flow.search_books("  dune  ")

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_search_books_uses_anna_while_google_circuit_is_open(monkeypatch):
    download_flow._GOOGLE_CACHE.clear()
    download_flow._GOOGLE_BREAKER.update(consecutive_failures=2, open_until=download_flow.time.monotonic() + 60)

    async def google_must_not_run(_query: str) -> list[dict]:
        raise AssertionError("open circuit must skip Google")

    async def fake_aa_search(_query: str) -> list[dict]:
        return [{"title": "Fallback book", "md5": "0123456789abcdef0123456789abcdef"}]

    monkeypatch.setattr(download_flow, "_fetch_google_books_search", google_must_not_run)
    monkeypatch.setattr(download_flow, "_search_aa_metadata", fake_aa_search)

    try:
        assert await download_flow.search_books("fallback book") == [
            {"title": "Fallback book", "md5": "0123456789abcdef0123456789abcdef"}
        ]
    finally:
        download_flow._GOOGLE_BREAKER.update(consecutive_failures=0, open_until=0.0)


def test_google_results_drop_unrelated_isbn_matches():
    volumes = [
        {
            "volumeInfo": {
                "title": "The Sand Chronicles",
                "authors": ["Someone Else"],
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441172719"}],
            }
        },
        {
            "volumeInfo": {
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441013593"}],
            }
        },
    ]

    results = download_flow._parse_google_books_results(volumes, "Dune Frank Herbert")

    assert [result["title"] for result in results] == ["Dune"]


@pytest.mark.asyncio
async def test_ebook_download_from_annas_md5_sends_downloaded_file(monkeypatch, tmp_path):
    statuses = []
    sent_paths = []

    epub_path = tmp_path / "hebrew-book.epub"
    epub_path.write_bytes(_make_valid_epub_bytes())

    async def fake_download(_md5: str, isbns=None, on_status=None) -> Path:
        return epub_path

    def fake_send(_email: str, book_path: Path | None = None, **_kwargs):
        sent_paths.append(book_path)

    monkeypatch.setattr(download_flow, "download_book_from_annas_archive", fake_download)
    monkeypatch.setattr(download_flow, "send_to_kindle", fake_send)

    await download_flow.ebook_download_from_annas_md5(
        "0123456789abcdef0123456789abcdef",
        "reader@example.com",
        on_status=statuses.append,
    )

    assert statuses == ["downloading", "sending", "done"]
    assert sent_paths == [epub_path]


@pytest.mark.asyncio
async def test_ebook_download_from_annas_md5_falls_back_to_libgen(monkeypatch):
    sent_paths = []

    async def fake_aa_download(_md5: str, isbns=None, on_status=None) -> Path:
        raise DownloadError("Anna CDN unavailable")

    async def fake_libgen_download(isbns: set[str], md5_list: list[str], **kwargs) -> Path:
        assert "0123456789abcdef0123456789abcdef" in isbns
        assert md5_list == ["0123456789abcdef0123456789abcdef"]
        return Path("/tmp/libgen-book.epub")

    def fake_send(_email: str, book_path: Path | None = None, **_kwargs):
        sent_paths.append(book_path)

    async def no_sleep(_seconds: float):
        return None

    monkeypatch.setattr(download_flow, "download_book_from_annas_archive", fake_aa_download)
    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_libgen_download)
    monkeypatch.setattr(download_flow, "send_to_kindle", fake_send)
    monkeypatch.setattr(download_flow.asyncio, "sleep", no_sleep)

    await download_flow.ebook_download_from_annas_md5(
        "0123456789abcdef0123456789abcdef",
        "reader@example.com",
    )

    assert sent_paths == [Path("/tmp/libgen-book.epub")]


@pytest.mark.asyncio
async def test_download_book_from_annas_archive_allows_isbn10_match(monkeypatch, tmp_path):
    """ISBN-10 on the page matches an ISBN-13 target (substring check)."""
    async def fake_fetch(_md5):
        return _HTML_WITH_ISBN10

    async def fake_try_ia(_md5, _html):
        out = tmp_path / "book.epub"
        out.write_bytes(b"epub-data")
        return out

    monkeypatch.setattr(download_with_annas_archive, "_fetch_md5_page", fake_fetch)
    monkeypatch.setattr(download_with_annas_archive, "_try_internet_archive", fake_try_ia)

    result = await download_with_annas_archive.download_book_from_annas_archive(
        "deadbeef", isbns={_ISBN13}
    )
    assert result.name == "book.epub"


@pytest.mark.asyncio
async def test_download_book_from_annas_archive_allows_no_isbn_on_page(monkeypatch, tmp_path):
    """Page with no ISBN metadata is allowed through (graceful fallback)."""
    async def fake_fetch(_md5):
        return _HTML_NO_ISBN

    async def fake_try_ia(_md5, _html):
        out = tmp_path / "book.epub"
        out.write_bytes(b"epub-data")
        return out

    monkeypatch.setattr(download_with_annas_archive, "_fetch_md5_page", fake_fetch)
    monkeypatch.setattr(download_with_annas_archive, "_try_internet_archive", fake_try_ia)

    result = await download_with_annas_archive.download_book_from_annas_archive(
        "deadbeef", isbns={_ISBN13}
    )
    assert result.name == "book.epub"


def _libgen_page_html(title: str = "", author: str = "", isbn: str = "") -> str:
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if author:
        parts.append(f"Author(s): {author}")
    if isbn:
        parts.append(f"ISBN: {isbn}")
    return f"<html><body><table><tr><td>{'<br>'.join(parts)}</td></tr></table></body></html>"


def _libgen_link_monkeypatch(monkeypatch, pages: dict[str, str]):
    async def fake_gather(urls):
        return list(urls)

    async def fake_fetch(_session, url):
        md5 = url.rsplit("md5=", 1)[-1]
        return pages.get(md5, "<html></html>")

    monkeypatch.setattr(download_with_libgen, "gather_page_status", fake_gather)
    monkeypatch.setattr(download_with_libgen, "_fetch_page", fake_fetch)


def test_extract_libgen_identity_extracts_title_author_isbn():
    identity = download_with_libgen._extract_libgen_identity(
        _libgen_page_html(title="The Great Gatsby", author="F. Scott Fitzgerald", isbn="9780743273565")
    )
    assert identity["title"] == "The Great Gatsby"
    assert identity["author"] == "F. Scott Fitzgerald"
    assert "9780743273565" in identity["isbn"]


def test_libgen_identity_confirmed_accepts_last_first_author_order():
    assert download_with_libgen._libgen_identity_confirmed(
        "The Great Gatsby", "F. Scott Fitzgerald", "The Great Gatsby", "King, Stephen"
    ) is False
    assert download_with_libgen._libgen_identity_confirmed(
        "The Great Gatsby", "Stephen King", "The Great Gatsby", "King, Stephen"
    ) is True


@pytest.mark.asyncio
async def test_get_libgen_link_selects_isbn_confirmed_link(monkeypatch):
    pages = {
        "a1" * 16: _libgen_page_html(title="Wrong Book", author="Other Author", isbn="9780000000000"),
        "b2" * 16: _libgen_page_html(title="The Great Gatsby", author="F. Scott Fitzgerald", isbn="9780743273565"),
    }
    _libgen_link_monkeypatch(monkeypatch, pages)

    link = await download_with_libgen.get_libgen_link(
        {"9780743273565"}, [("a1" * 16), ("b2" * 16)], "https://libgen.test",
        title="The Great Gatsby", author="F. Scott Fitzgerald",
    )
    assert link.endswith("b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2")


@pytest.mark.asyncio
async def test_get_libgen_link_accepts_identity_confirmed_when_isbn_missing(monkeypatch):
    pages = {
        "c3" * 16: _libgen_page_html(title="The Great Gatsby", author="F. Scott Fitzgerald", isbn=""),
    }
    _libgen_link_monkeypatch(monkeypatch, pages)

    link = await download_with_libgen.get_libgen_link(
        {"9780743273565"}, ["c3" * 16], "https://libgen.test",
        title="The Great Gatsby", author="F. Scott Fitzgerald",
    )
    assert link.endswith("c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3")


@pytest.mark.asyncio
async def test_get_libgen_link_rejects_unconfirmed_when_confirmation_required(monkeypatch):
    pages = {
        "d4" * 16: _libgen_page_html(title="Unrelated Book", author="Someone Else", isbn=""),
    }
    _libgen_link_monkeypatch(monkeypatch, pages)

    with pytest.raises(BookNotFoundError):
        await download_with_libgen.get_libgen_link(
            {"9780743273565"}, ["d4" * 16], "https://libgen.test",
            title="The Great Gatsby", author="F. Scott Fitzgerald",
        )


@pytest.mark.asyncio
async def test_get_libgen_link_permissive_fallback_when_confirmation_not_required(monkeypatch):
    pages = {
        "e5" * 16: _libgen_page_html(title="", author="", isbn=""),
    }
    _libgen_link_monkeypatch(monkeypatch, pages)

    link = await download_with_libgen.get_libgen_link(
        {"e5" * 16}, ["e5" * 16], "https://libgen.test", require_confirmation=False
    )
    assert link.endswith("e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5")


@pytest.mark.asyncio
async def test_get_libgen_link_matches_isbn10_page_for_isbn13_target(monkeypatch):
    pages = {
        "f6" * 16: _libgen_page_html(title="The Great Gatsby", author="F. Scott Fitzgerald", isbn=_ISBN10),
    }
    _libgen_link_monkeypatch(monkeypatch, pages)

    link = await download_with_libgen.get_libgen_link(
        {_ISBN13}, ["f6" * 16], "https://libgen.test",
        title="The Great Gatsby", author="F. Scott Fitzgerald",
    )
    assert link.endswith("f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6")


# ---------------------------------------------------------------------------
# Kindle delivery: Gmail 552 BlockedMessage fix (EPUB watermark stripping)
# ---------------------------------------------------------------------------

def _build_epub_bytes(*, marker: str | None = "oceanofpdf.com") -> bytes:
    """Build a minimal EPUB zip, optionally embedding a root-level marker file."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("OEBPS/content.opf", "<package/>")
        z.writestr("OEBPS/c1.xhtml", "<html><body>hi</body></html>")
        if marker:
            z.writestr(marker, marker)
    return buf.getvalue()


def test_sanitize_epub_strips_root_watermark_file():
    data = _build_epub_bytes(marker="oceanofpdf.com")
    clean = download_flow._sanitize_epub_bytes(data)

    import io as _io
    import zipfile as _zipfile
    with _zipfile.ZipFile(_io.BytesIO(clean)) as z:
        names = z.namelist()
    assert "oceanofpdf.com" not in names
    assert "mimetype" in names
    assert "META-INF/container.xml" in names
    assert "OEBPS/content.opf" in names
    assert "OEBPS/c1.xhtml" in names


def test_sanitize_epub_keeps_mimetype_first_and_stored():
    data = _build_epub_bytes()
    clean = download_flow._sanitize_epub_bytes(data)

    import io as _io
    import zipfile as _zipfile
    with _zipfile.ZipFile(_io.BytesIO(clean)) as z:
        infos = z.infolist()
        mimetype = z.read("mimetype")
    assert infos[0].filename == "mimetype"
    assert infos[0].compress_type == _zipfile.ZIP_STORED
    assert mimetype == b"application/epub+zip"


def test_sanitize_epub_passes_through_non_epub():
    pdf = b"%PDF-1.3 fake pdf bytes"
    assert download_flow._sanitize_epub_bytes(pdf) == pdf
    assert download_flow._sanitize_epub_bytes(b"") == b""
    assert download_flow._sanitize_epub_bytes(b"not a zip at all") == b"not a zip at all"


def test_send_to_kindle_sanitizes_epub_before_attach(monkeypatch, tmp_path):
    """The delivery path must send sanitized bytes (no watermark)."""
    import io as _io
    import smtplib as _smtplib
    import zipfile as _zipfile

    seen: dict = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            for part in msg.iter_attachments():
                seen["payload"] = part.get_payload(decode=True)
                seen["filename"] = part.get_filename()

    monkeypatch.setattr(_smtplib, "SMTP_SSL", _FakeSMTP)

    book = tmp_path / "book.epub"
    book.write_bytes(_build_epub_bytes(marker="oceanofpdf.com"))
    download_flow.send_to_kindle("reader@example.com", book_path=book)

    with _zipfile.ZipFile(_io.BytesIO(seen["payload"])) as z:
        names = z.namelist()
    assert "oceanofpdf.com" not in names
    assert "OEBPS/content.opf" in names


def test_ebook_extension_sniffs_epub_and_pdf():
    epub = _build_epub_bytes(marker=None)
    assert download_with_annas_archive._ebook_extension(epub) == ".epub"
    assert download_with_annas_archive._ebook_extension(b"%PDF-1.7 x") == ".pdf"
    assert download_with_annas_archive._ebook_extension(b"BOOKMOBIgarbage") == ""
    assert download_with_annas_archive._ebook_extension(b"") == ""
