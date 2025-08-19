import asyncio
import asyncio
import re
import time
from pathlib import Path

import aiohttp
import requests
import selenium.common
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

from utils import log_function_call, log_coroutine_call, \
    find_newest_file_in_downloads


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
async def get_libgen_link(isbn, libgen_mirror) -> str:
    query = f'https://annas-archive.org/search?q={isbn}&ext=epub&lang=en&lang=he'
    response = requests.get(query)
    text = response.text
    hashes = re.findall('href...md5.([0-9a-f]+)', text)
    links = [f'{libgen_mirror}/get.php?md5={md5}' for md5 in hashes]

    # filter out matches with no valid download on libgen
    status = await gather_page_status(links)
    active_links = [stat for stat in status if stat]

    # gather metadata on each result from annas archive
    pages = [requests.get(link).text for link in active_links]
    matches = [{'Title': re.findall(r'<td>Title: ([\w: ]+?)<br>', page)[0],
                'Author': re.findall(r'Author\(s\): (.+?)<br>', page)[0],
                'ISBN': re.findall(r'ISBN: ([\d ;]+?)<br>', page)[0]} for page in
               pages]
    # filter out matches with the wrong ISBN
    matches = [isbn in match['ISBN'] for match in matches]
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
        elem = driver.find_element(By.XPATH, "/html/body/table/tbody/tr[1]/td[2]/a")
        elem.click()
        book_path = find_newest_file_in_downloads()
        while book_path.suffix == 'part':
            time.sleep(0.5)
            book_path = find_newest_file_in_downloads()
        time.sleep(0.5)
        driver.close()
        return book_path
    except selenium.common.NoSuchElementException:
        driver.close()
        raise RuntimeError('Failed to find book in libgen')


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