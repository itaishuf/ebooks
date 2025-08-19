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
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    NoAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

        xpath = "/html/body/table/tbody/tr[1]/td[2]/a"

        def close_popups_and_return():
            # Dismiss alert if present
            try:
                alert = driver.switch_to.alert
                alert.dismiss()
            except NoAlertPresentException:
                pass

            # Close any newly opened tabs/windows (ads)
            for handle in list(driver.window_handles):
                if handle != original_handle:
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception:
                        pass
            # Return to the original window
            driver.switch_to.window(original_handle)

        # Try clicking, handle intercepted clicks by closing ads and retrying
        attempts = 3
        for i in range(attempts):
            try:
                elem = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                elem.click()
                break  # clicked successfully
            except ElementClickInterceptedException:
                close_popups_and_return()
                # Small wait before retry
                time.sleep(0.5)
            except WebDriverException as e:
                if "intercept" in str(e).lower():
                    close_popups_and_return()
                    time.sleep(0.5)
                else:
                    raise
        else:
            # As a last resort try JS click once
            elem = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
            try:
                driver.execute_script("arguments[0].click();", elem)
            except Exception:
                close_popups_and_return()
                elem = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                driver.execute_script("arguments[0].click();", elem)

        # After clicking, close any ad tabs that may have opened and return
        close_popups_and_return()

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