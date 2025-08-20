import asyncio
import logging
import re
import time
from pathlib import Path

import aiohttp
import requests
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from utils import log_function_call, log_coroutine_call, \
    find_newest_file_in_downloads

logger = logging.getLogger(__name__)


@log_coroutine_call
async def choose_libgen_mirror() -> str:
    """
    Return the first active libgen mirror.

    This function will return the first libgen mirror that is available.
    If no mirrors are available, it will raise a `ConnectionError`.
    """
    mirrors = ["https://libgen.is", "https://libgen.st", "https://libgen.bz",
               "https://libgen.gs", "https://libgen.la", "https://libgen.gl",
               "https://libgen.li", "https://libgen.rs"]
    status = await gather_page_status(mirrors)
    mirrors = [stat for stat in status if stat]
    if len(mirrors) == 0:
        raise ConnectionError('No active libgen mirror found')
    return mirrors[0]


@log_coroutine_call
async def get_libgen_link(isbn, book_md5_list, libgen_mirror) -> str:
    links = [f'{libgen_mirror}/get.php?md5={md5}' for md5 in book_md5_list]

    # filter out matches with no valid download on libgen
    status = await gather_page_status(links)
    active_links = [stat for stat in status if stat]

    # gather metadata on each result from annas archive
    pages = [requests.get(link).text for link in active_links]
    matches = [{'Title': re.findall(r'<td>Title: ([\w: ]+?)<br>', page),
                'Author': re.findall(r'Author\(s\): (.+?)<br>', page),
                'ISBN': re.findall(r'ISBN: ([\d ;]+?)<br>', page)} for page in
               pages]
    # add filler for matches with no ISBN
    for match in matches:
        if not match['ISBN']:
            match['ISBN'] = ['*']
    # filter out matches with the wrong ISBN
    matches = [isbn in match['ISBN'][0] for match in matches]
    correct_active_links = [link for link, match in zip(active_links, matches) if
                            match]

    return correct_active_links[0]


@log_function_call
def download_book_using_selenium(url: str) -> Path:
    options = Options()
    # options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)
    try:
        driver.get(url)
        original_handle = driver.current_window_handle
        wait = WebDriverWait(driver, 10)

        button_xpath = "/html/body/table/tbody/tr[1]/td[2]/a"
        ad_xpath = '[ @ id = "lky1s"]'

        # Try clicking, handle intercepted clicks by closing ads and retrying
        attempts = 3
        for i in range(attempts):
            try:
                elem = driver.find_element(By.XPATH, button_xpath)
                elem.click()
                break
            except ElementClickInterceptedException:
                click_and_close_the_popup(driver, original_handle)

        book_path = find_newest_file_in_downloads()
        while book_path.suffix == 'part':
            time.sleep(0.5)
            book_path = find_newest_file_in_downloads()
        time.sleep(0.5)
        driver.close()
        return book_path
    except NoSuchElementException:
        driver.close()
        raise RuntimeError('Failed to find book in libgen')


@log_function_call
def click_and_close_the_popup(driver, original_handle):
    # click somewhere on the screen
    elem = driver.find_element(By.TAG_NAME, 'body')
    elem.click()
    driver.switch_to.window(original_handle)


@log_coroutine_call
async def gather_page_status(urls: list[str]):
    async with aiohttp.ClientSession() as session:
        tasks = [check_page_status(session, url) for url in urls]
        return await asyncio.gather(*tasks)


async def check_page_status(session, url: str):
    """
    No additional logging is required for this function;
    gather_page_status provides sufficient debugging information.
    """
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                return url
            else:
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None