import asyncio
import logging
import re
import time
from pathlib import Path

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
from exceptions import BookNotFoundError, DownloadError
from utils import find_newest_file_in_downloads, log_call

logger = logging.getLogger(__name__)


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
    links = [f'{libgen_mirror}/get.php?md5={md5}' for md5 in book_md5_list]

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
        raise BookNotFoundError(f"No libgen download found matching ISBN {isbn}")
    return correct_active_links[0]


@log_call
def download_book_using_selenium(url: str) -> Path:
    options = Options()
    options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)
    try:
        driver.get(url)
        original_handle = driver.current_window_handle

        button_xpath = "/html/body/table/tbody/tr[1]/td[2]/a"

        for _ in range(settings.selenium_click_attempts):
            try:
                elem = driver.find_element(By.XPATH, button_xpath)
                elem.click()
                break
            except ElementClickInterceptedException:
                driver.find_element(By.TAG_NAME, 'body').click()
                driver.switch_to.window(original_handle)

        book_path = find_newest_file_in_downloads()
        while book_path.suffix == '.part':
            time.sleep(0.5)
            book_path = find_newest_file_in_downloads()
        time.sleep(0.5)
        return book_path
    except NoSuchElementException as e:
        raise DownloadError('Failed to find book in libgen') from e
    finally:
        driver.close()


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
