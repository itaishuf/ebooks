from pathlib import Path

import pytest

import download_flow
import download_with_libgen
import service
from exceptions import ManualDownloadRequiredError


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
    result_path = tmp_path / "book.epub"
    calls = {"count": 0}

    monkeypatch.setattr(download_with_libgen.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(download_with_libgen.webdriver, "Firefox", lambda options=None: driver)
    monkeypatch.setattr(download_with_libgen.time, "sleep", lambda _seconds: None)

    def fake_find_newest_file_in_downloads(since=None):
        assert since is not None
        calls["count"] += 1
        if calls["count"] == 1:
            raise FileNotFoundError("No new file detected after Selenium click")
        return result_path

    monkeypatch.setattr(
        download_with_libgen,
        "find_newest_file_in_downloads",
        fake_find_newest_file_in_downloads,
    )

    book_path = download_with_libgen.download_book_using_selenium("https://libgen.test/get.php?md5=abc")

    assert book_path == result_path
    assert driver.quit_called is True
    assert calls["count"] == 2


def test_download_book_using_selenium_raises_manual_fallback(monkeypatch, tmp_path):
    driver = _FakeDriver()

    monkeypatch.setattr(download_with_libgen.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(download_with_libgen.webdriver, "Firefox", lambda options=None: driver)
    monkeypatch.setattr(download_with_libgen.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        download_with_libgen,
        "find_newest_file_in_downloads",
        lambda since=None: (_ for _ in ()).throw(FileNotFoundError("No new file detected after Selenium click")),
    )

    with pytest.raises(ManualDownloadRequiredError) as exc:
        download_with_libgen.download_book_using_selenium("https://libgen.test/get.php?md5=def")

    assert "Selenium never detected a new downloaded file" in str(exc.value)
    assert exc.value.fallback_url == "https://libgen.test/get.php?md5=def"
    assert driver.quit_called is True


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
    assert service.jobs[job_id]["error"] == "All download attempts failed for ISBN 123"
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
