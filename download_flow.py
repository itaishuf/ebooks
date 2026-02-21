import asyncio
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

from config import settings
from download_with_libgen import (
    choose_libgen_mirror,
    download_book_using_selenium,
    get_libgen_link,
)
from exceptions import (
    BookNotFoundError,
    DownloadError,
    EmailDeliveryError,
    InvalidURLError,
)
from utils import log_call

logger = logging.getLogger(__name__)


@log_call
async def get_isbn(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                text = await response.text()
    except aiohttp.InvalidUrlClientError:
        raise InvalidURLError(f"Goodreads URL isn't valid: {url}")
    soup = BeautifulSoup(text, 'html.parser')
    isbn_tag = soup.find(string=re.compile(r'isbn', re.IGNORECASE))
    if isbn_tag:
        match = re.search(r'(\d{10,13})', str(isbn_tag))
        if match:
            return match.group(1)
    # fallback: search entire page text
    match = re.search(r'isbn...(\d+)', text)
    if not match:
        raise BookNotFoundError(f"No ISBN found on page: {url}")
    return match.group(1)


@log_call
def send_to_kindle(email: str, book_path: Path | None = None,
                   book_data: bytes = b'', filename: str = ''):
    msg = EmailMessage()
    msg['From'] = settings.gmail_account
    msg['To'] = email
    msg['Subject'] = 'book'

    if book_path:
        with open(book_path, 'rb') as f:
            file_data = f.read()
            file_name = f.name
    else:
        file_data = book_data
        file_name = filename
    logger.info(f"file size: {round(len(file_data) / 1000, 1)}KB")
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream',
                       filename=file_name)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(settings.gmail_account, settings.gmail_password)
            smtp.send_message(msg)
    except smtplib.SMTPException as e:
        raise EmailDeliveryError(f"Failed to send email to {email}") from e
    if book_path:
        os.remove(book_path)


@log_call
async def get_book_md5(isbn: str) -> list[str]:
    query = f'https://annas-archive.org/search?q={isbn}&ext=epub&lang=en&lang=he'
    async with aiohttp.ClientSession() as session:
        async with session.get(query) as response:
            text = await response.text()
    soup = BeautifulSoup(text, 'html.parser')
    hashes = []
    for a_tag in soup.find_all('a', href=re.compile(r'/md5/[0-9a-f]+')):
        match = re.search(r'/md5/([0-9a-f]+)', a_tag['href'])
        if match:
            hashes.append(match.group(1))
    return hashes


async def ebook_download(goodreads_url: str, kindle_mail: str) -> None:
    isbn = await get_isbn(goodreads_url)
    book_md5_list = await get_book_md5(isbn)
    if not book_md5_list:
        raise BookNotFoundError(f"No book found for ISBN {isbn}")
    try:
        libgen_mirror = await choose_libgen_mirror()
        url = await get_libgen_link(isbn, book_md5_list, libgen_mirror)
        book_path = await asyncio.to_thread(download_book_using_selenium, url)
        await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)
    except ConnectionError:
        # libgen is down, fall back to anna's archive paid API
        if not settings.annas_archive_api_key:
            raise DownloadError("LibGen is down and no Anna's Archive API key is configured")
        uri = f'/dyn/api/fast_download.json?md5={book_md5_list[0]}&key={settings.annas_archive_api_key}'
        domain = 'annas-archive.org'
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://{domain}/{uri}') as response:
                data = await response.json()
                download_url = data.get('download_url')
                if not download_url:
                    raise DownloadError("Anna's Archive API did not return a download URL")
            async with session.get(download_url) as response:
                file_data = await response.read()
        await asyncio.to_thread(
            send_to_kindle, kindle_mail, None, file_data, f'{book_md5_list[0]}.epub'
        )
