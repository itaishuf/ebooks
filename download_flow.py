import asyncio
import functools
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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

# redirect console output so script will run with pythonw
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

logger = logging.getLogger(__name__)


def log_coroutine_call(func):
    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        logger.info(f"function name:{func.__name__}, function arguments: {args}")
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        logger.info(
            f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        return result


def log_function_call(func):
    """Logs the execution time for both async and sync functions."""

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        logger.info(f"function name:{func.__name__}, function arguments: {args}")
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        end_time = time.perf_counter()
        logger.info(
            f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        return result


@log_function_call
def get_isbn(url):
    response = requests.get(url)
    text = response.text
    text = re.search(r'isbn...(\d+)', text).groups()[0]
    return text


@log_coroutine_call
async def choose_libgen_mirror():
    mirrors = ["https://libgen.is", "https://libgen.st", "https://libgen.bz",
               "https://libgen.gs", "https://libgen.la", "https://libgen.gl",
               "https://libgen.li", "https://libgen.rs"]
    status = await check_libgen_mirrors(mirrors)
    mirrors = [stat for stat in status if stat]
    if len(mirrors) == 0:
        raise ConnectionError('No active libgen mirror found')
    return mirrors[0]


@log_coroutine_call
async def check_mirror_status(session, url):
    try:
        async with session.get(url, timeout=5):
            return url
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


@log_coroutine_call
async def check_libgen_mirrors(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [check_mirror_status(session, url) for url in urls]
        return await asyncio.gather(*tasks)


@log_function_call
def get_libgen_link(isbn, libgen_mirror):
    query = f'https://annas-archive.org/search?q={isbn}&ext=epub'
    response = requests.get(query)
    text = response.text
    md5 = re.search('href...md5.([0-9a-f]+)', text).groups()[0]
    link = f'{libgen_mirror}/get.php?md5={md5}'
    return link


@log_function_call
def send_to_kindle(book_path, email):
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

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(os.getenv('GMAIL_ACCOUNT'), os.getenv('GMAIL_PASSWORD'))
        smtp.send_message(msg)

    os.remove(book_path)


@log_function_call
def download_book_using_selenium(url):
    options = Options()
    # options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)
    driver.get(url)
    elem = driver.find_element(By.XPATH, "/html/body/table/tbody/tr[1]/td[2]/a")
    elem.click()
    driver.close()
    book_path = find_newest_file_in_downloads()
    return book_path


@log_function_call
def find_newest_file_in_downloads():
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
    except Exception as e:
        raise FileNotFoundError("Error locating the file downloaded with Selenium")


async def ebook_download(goodreads_url, kindle_mail):
    libgen_mirror = await choose_libgen_mirror()
    isbn = get_isbn(goodreads_url)
    url = get_libgen_link(isbn, libgen_mirror)
    book_path = download_book_using_selenium(url)
    send_to_kindle(book_path, kindle_mail)
