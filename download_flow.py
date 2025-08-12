import asyncio
import logging
import os
import re
import smtplib
import sys
import time
import winreg
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import aiohttp
import requests
import selenium.common
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

# redirect console output so script will run with pythonw
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

logger = logging.getLogger(__name__)


def log_coroutine_call(func):
    async def async_wrapper(*args, **kwargs):
        logger.info(f"function name:{func.__name__}, function arguments: {args}")
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        end_time = time.perf_counter()
        logger.info(
            f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        return result

    return async_wrapper


def log_function_call(func):
    def sync_wrapper(*args, **kwargs):
        logger.info(f"function name:{func.__name__}, function arguments: {args}")
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        logger.info(
            f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        return result

    return sync_wrapper


@log_function_call
def get_isbn(url: str) -> str:
    try:
        response = requests.get(url)
        text = response.text
        text = re.search(r'isbn...(\d+)', text).groups()[0]
        return text
    except requests.exceptions.MissingSchema:
        raise ConnectionRefusedError("Goodreads URL isn't valid")


@log_coroutine_call
async def choose_libgen_mirror() -> str:
    mirrors = ["https://libgen.is", "https://libgen.st", "https://libgen.bz",
               "https://libgen.gs", "https://libgen.la", "https://libgen.gl",
               "https://libgen.li", "https://libgen.rs"]
    status = await gather_page_status(mirrors)
    mirrors = [stat for stat in status if stat]
    if len(mirrors) == 0:
        raise ConnectionError('No active libgen mirror found')
    return mirrors[0]


async def check_page_status(session, url: str):
    """
    i didnt add logging for this function since it isnt needed for debugging.
    gather_page_status is enough
    """
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                return url
            else:
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


@log_coroutine_call
async def gather_page_status(urls: list[str]):
    async with aiohttp.ClientSession() as session:
        tasks = [check_page_status(session, url) for url in urls]
        return await asyncio.gather(*tasks)


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
def send_to_kindle(book_path: Path, email: str):
    msg = EmailMessage()
    msg['From'] = os.getenv('GMAIL_ACCOUNT')
    msg['To'] = email
    msg['Subject'] = 'book'

    # Attach file
    with open(book_path, 'rb') as f:
        file_data = f.read()
        file_name = f.name
        logger.info(f"file size: {round(len(file_data) / 1000, 1)}KB")
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream',
                       filename=file_name)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(os.getenv('GMAIL_ACCOUNT'), os.getenv('GMAIL_PASSWORD'))
            smtp.send_message(msg)
    except smtplib.SMTPException:
        raise ConnectionRefusedError("Kindle mail isn't valid")

    os.remove(book_path)


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


@log_function_call
def find_newest_file_in_downloads() -> Path:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        downloads_dir = Path(os.path.expandvars(
            winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]))
    try:
        files = [f for f in downloads_dir.iterdir() if f.is_file()]

        newest_file = max(files, key=os.path.getmtime)
        last_modified = datetime.fromtimestamp(
            os.path.getmtime(newest_file)).strftime('%Y-%m-%d %H:%M:%S')

        logger.info({"file name": newest_file.name, "time": last_modified})
        return newest_file.absolute()
    except Exception:
        raise FileNotFoundError("Error locating the file downloaded with Selenium")


async def ebook_download(goodreads_url, kindle_mail):
    libgen_mirror = await choose_libgen_mirror()
    isbn = get_isbn(goodreads_url)
    url = await get_libgen_link(isbn, libgen_mirror)
    book_path = download_book_using_selenium(url)
    send_to_kindle(book_path, kindle_mail)
