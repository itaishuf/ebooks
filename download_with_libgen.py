import asyncio
import logging
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

from config import settings
from exceptions import BookNotFoundError, DownloadError, ManualDownloadRequiredError
from utils import log_call

logger = logging.getLogger(__name__)
DOWNLOAD_POLL_INTERVAL_SECONDS = 0.5
DOWNLOAD_POLL_ATTEMPTS = 10
DOWNLOAD_RECLICK_ATTEMPTS = 2


@log_call
async def choose_libgen_mirror() -> str:
    """
    Return the first active libgen mirror.

    If no mirrors are available, raises ``ConnectionError``.
    """
    status = await gather_page_status(settings.libgen_mirrors)
    mirrors = [stat for stat in status if stat]
    if not mirrors:
        raise ConnectionError('No active libgen mirror found')
    return mirrors[0]


async def _fetch_page(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as response:
        return await response.text()


@log_call
async def get_libgen_link(isbn: str, book_md5_list: list[str], libgen_mirror: str) -> str:
    links = [f'{libgen_mirror}/get.php?md5={quote(md5)}' for md5 in book_md5_list]

    status = await gather_page_status(links)
    active_links = [stat for stat in status if stat]

    async with aiohttp.ClientSession() as session:
        pages = await asyncio.gather(*[_fetch_page(session, link) for link in active_links])

    correct_active_links = []
    for link, page in zip(active_links, pages):
        soup = BeautifulSoup(page, 'html.parser')
        isbn_text = ""
        for td in soup.find_all('td'):
            text = td.get_text()
            if 'ISBN' in text:
                isbn_text = text
                break
        if isbn in isbn_text or not isbn_text:
            correct_active_links.append(link)

    if not correct_active_links:
        correct_active_links = active_links
    if not correct_active_links:
        raise BookNotFoundError(f"No libgen download found matching ISBN {isbn}")
    return correct_active_links[0]


def _force_quit_driver(driver: webdriver.Firefox) -> None:
    """Kill geckodriver directly, bypassing the HTTP session teardown that can hang."""
    try:
        driver.service.process.kill()
        driver.service.process.wait(timeout=5)
    except Exception as e:
        logger.warning(f"Browser cleanup failed: {e}")


@log_call
def download_book_using_selenium(url: str) -> Path:
    base_download_dir = Path(settings.download_dir).resolve()
    base_download_dir.mkdir(parents=True, exist_ok=True)
    # Isolate each Selenium session so concurrent jobs never inspect each other's artifacts.
    download_dir = Path(tempfile.mkdtemp(prefix="selenium-", dir=base_download_dir))

    options = Options()
    options.add_argument("--headless")
    options.set_preference("browser.download.folderList", 2)
    options.set_preference("browser.download.dir", str(download_dir))
    options.set_preference("browser.download.useDownloadDir", True)
    options.set_preference("browser.helperApps.neverAsk.saveToDisk",
                           "application/epub+zip,application/pdf,application/octet-stream")
    options.set_preference("browser.download.manager.showWhenStarting", False)
    options.set_preference("pdfjs.disabled", True)

    logger.info(
        f"Starting Selenium download for {url} with base_download_dir={base_download_dir}, "
        f"session_download_dir={download_dir}, "
        f"headless=True, click_attempts={settings.selenium_click_attempts}"
    )

    driver = webdriver.Firefox(options=options)
    try:
        driver.get(url)
        button_xpath = "/html/body/table/tbody/tr[1]/td[2]/a"

        for page_attempt in range(DOWNLOAD_RECLICK_ATTEMPTS):
            attempt_started_at = time.time()
            logger.info(
                f"LibGen Selenium page attempt {page_attempt + 1}/{DOWNLOAD_RECLICK_ATTEMPTS} "
                f"for {url} with attempt_started_at={attempt_started_at:.3f}"
            )
            _log_download_dir_state(download_dir, f"Before click page attempt {page_attempt + 1}")
            _click_download_button(driver, button_xpath, url)
            book_path = _wait_for_download(download_dir, attempt_started_at, url, page_attempt + 1)
            if book_path:
                time.sleep(DOWNLOAD_POLL_INTERVAL_SECONDS)
                logger.info(
                    f"Selenium download completed for {url}: {book_path} "
                    f"(session_download_dir={download_dir})"
                )
                return book_path

            logger.warning(
                f"No completed download artifact detected for {url} after page attempt "
                f"{page_attempt + 1}/{DOWNLOAD_RECLICK_ATTEMPTS}"
            )
            if page_attempt < DOWNLOAD_RECLICK_ATTEMPTS - 1:
                driver.get(url)

        fallback_message = (
            "Automatic download failed after multiple attempts. "
            "Please open the LibGen link and download the file manually."
        )
        raise ManualDownloadRequiredError(
            "Automatic download failed because Selenium never detected a new downloaded file.",
            fallback_url=url,
            fallback_message=fallback_message,
        )
    except NoSuchElementException as e:
        raise DownloadError('Failed to find book in libgen') from e
    finally:
        _force_quit_driver(driver)


def _click_download_button(driver: webdriver.Firefox, button_xpath: str, url: str) -> None:
    original_handle = driver.current_window_handle
    for click_attempt in range(settings.selenium_click_attempts):
        try:
            elem = driver.find_element(By.XPATH, button_xpath)
            elem.click()
            logger.info(
                f"Clicked LibGen download button for {url} on click attempt "
                f"{click_attempt + 1}/{settings.selenium_click_attempts}"
            )
            return
        except ElementClickInterceptedException as e:
            logger.warning(
                f"LibGen click intercepted for {url} on attempt "
                f"{click_attempt + 1}/{settings.selenium_click_attempts}: {e}"
            )
            driver.find_element(By.TAG_NAME, 'body').click()
            driver.switch_to.window(original_handle)
    raise DownloadError(f"Failed to click LibGen download button for {url}")


def _wait_for_download(download_dir: Path, since: float, url: str, page_attempt: int) -> Path | None:
    for poll_attempt in range(DOWNLOAD_POLL_ATTEMPTS):
        try:
            candidates = _find_download_candidates(download_dir, since)
            book_path = candidates[0]
            logger.info(
                f"Detected download candidate for {url} on page attempt {page_attempt}, "
                f"poll attempt {poll_attempt + 1}/{DOWNLOAD_POLL_ATTEMPTS}: {book_path.name}"
            )
            completed_candidates = [
                candidate for candidate in candidates
                if candidate.suffix != '.part' and candidate.stat().st_size > 0
            ]
            if completed_candidates:
                completed_path = completed_candidates[0]
                logger.info(
                    f"Detected completed download for {url} on page attempt {page_attempt}, "
                    f"poll attempt {poll_attempt + 1}/{DOWNLOAD_POLL_ATTEMPTS}: {completed_path.name}"
                )
                return completed_path
            logger.info(
                f"Download for {url} is still partial on page attempt {page_attempt}: "
                f"{book_path.name}"
            )
        except FileNotFoundError as e:
            logger.warning(
                f"No new download artifact yet for {url} on page attempt {page_attempt}, "
                f"poll attempt {poll_attempt + 1}/{DOWNLOAD_POLL_ATTEMPTS}: {e}"
            )
        _log_download_dir_state(
            download_dir,
            f"After poll attempt {poll_attempt + 1} for page attempt {page_attempt}"
        )
        time.sleep(DOWNLOAD_POLL_INTERVAL_SECONDS)
    return None


def _find_download_candidates(download_dir: Path, since: float) -> list[Path]:
    try:
        files = [path for path in download_dir.iterdir() if path.is_file()]
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Download directory {download_dir} does not exist") from e

    candidates = [path for path in files if path.stat().st_mtime >= since]
    if not candidates:
        raise FileNotFoundError(
            f"No new file detected in isolated download directory {download_dir} after Selenium click"
        )

    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates


def _log_download_dir_state(download_dir: Path, label: str) -> None:
    files = [f for f in download_dir.iterdir() if f.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    details = []
    now = time.time()
    for path in files[:5]:
        age_s = now - path.stat().st_mtime
        details.append(f"{path.name}(suffix={path.suffix or '<none>'},age_s={age_s:.2f})")
    logger.info(
        f"{label}: download_dir={download_dir}, file_count={len(files)}, "
        f"files={details}"
    )


@log_call
async def gather_page_status(urls: list[str]) -> list[str | None]:
    async with aiohttp.ClientSession() as session:
        tasks = [check_page_status(session, url) for url in urls]
        return await asyncio.gather(*tasks)


async def check_page_status(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                return url
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
