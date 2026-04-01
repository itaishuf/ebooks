import os
from pathlib import Path

import pytest
from selenium.common.exceptions import WebDriverException

import download_flow
import download_with_libgen
import service
from exceptions import DownloadError, ManualDownloadRequiredError


class _FakeElement:
    def click(self):
        return None


class _FakeSwitchTo:
    def window(self, _handle):
        return None


class _FakeDriver:
    def __init__(self, options=None):
        self.options = options
        self.current_window_handle = "main"
        self.switch_to = _FakeSwitchTo()
        self.visited_urls = []
        self.quit_called = False

    def get(self, url):
        self.visited_urls.append(url)

    def find_element(self, _by, _value):
        return _FakeElement()

    def quit(self):
        self.quit_called = True


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
        result_path.write_bytes(b"downloaded")
        return result_path

    monkeypatch.setattr(download_with_libgen, "_wait_for_download", fake_wait_for_download)

    book_path = download_with_libgen.download_book_using_selenium("https://libgen.test/get.php?md5=abc")

    assert book_path.name == "book.epub"
    assert book_path.parent.parent == tmp_path
    assert book_path.parent.name.startswith("selenium-")
    assert driver.quit_called is True
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
    assert driver.quit_called is True


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

    async def fake_get_isbn(_url):
        return "isbn-123"

    async def fake_get_book_md5(_isbn, ext="epub"):
        return ["epub-md5"] if ext == "epub" else ["pdf-md5"]

    async def fake_download_via_libgen(_isbn, _md5_list):
        result = downloaded_paths.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def fake_send_to_kindle(_email, book_path=None, book_data=b"", filename=""):
        sent_books.append(book_path or filename or book_data)

    monkeypatch.setattr(download_flow, "get_isbn", fake_get_isbn)
    monkeypatch.setattr(download_flow, "get_book_md5", fake_get_book_md5)
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
    async def fake_download_via_libgen(_isbn, _md5_list):
        raise ManualDownloadRequiredError(
            "Automatic download failed because Selenium never detected a new downloaded file.",
            fallback_url="https://libgen.test/get.php?md5=md5",
            fallback_message="Try downloading the file manually from LibGen.",
        )

    monkeypatch.setattr(download_flow, "_download_via_libgen", fake_download_via_libgen)

    with pytest.raises(ManualDownloadRequiredError) as exc:
        await download_flow.ebook_download_by_md5(
            "0123456789abcdef0123456789abcdef",
            "epub",
            "reader@example.com",
        )

    assert exc.value.fallback_url == "https://libgen.test/get.php?md5=md5"
