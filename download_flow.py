import asyncio
import json
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
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            text = await response.text()
    except aiohttp.InvalidUrlClientError as e:
        raise InvalidURLError(f"Goodreads URL isn't valid: {url}") from e
    soup = BeautifulSoup(text, 'html.parser')

    # Try JSON-LD structured data first (most reliable)
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            isbn = data.get('isbn')
            if isbn:
                return isbn
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue

    # Fallback: search page text for ISBN patterns
    match = re.search(r'isbn.{0,5}(\d{10,13})', text, re.IGNORECASE)
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
async def get_book_md5(isbn: str, ext: str = "epub") -> list[str]:
    query = f'https://{settings.annas_archive_domain}/search?q={isbn}&ext={ext}&lang=en&lang=he'
    async with aiohttp.ClientSession() as session, session.get(query) as response:
        text = await response.text()
    soup = BeautifulSoup(text, 'html.parser')
    hashes = []
    for a_tag in soup.find_all('a', href=re.compile(r'/md5/[0-9a-f]+')):
        match = re.search(r'/md5/([0-9a-f]+)', a_tag['href'])
        if match:
            hashes.append(match.group(1))
    return hashes


@log_call
async def search_books(query: str) -> list[dict]:
    url = f'https://www.goodreads.com/search?q={query}'
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        text = await response.text()
    soup = BeautifulSoup(text, 'html.parser')

    results = []
    for row in soup.find_all('tr', attrs={'itemtype': 'http://schema.org/Book'}):
        title_tag = row.find('a', class_='bookTitle')
        author_tag = row.find('a', class_='authorName')
        img_tag = row.find('img')

        if not title_tag:
            continue

        goodreads_url = f'https://www.goodreads.com{title_tag["href"]}'
        results.append({
            "title": title_tag.text.strip(),
            "author": author_tag.text.strip() if author_tag else "",
            "goodreads_url": goodreads_url,
            "cover_url": img_tag["src"] if img_tag else "",
        })

    return results


async def _download_via_annas_api(md5: str, ext: str) -> tuple[bytes, str]:
    if not settings.annas_archive_api_key:
        raise DownloadError("No Anna's Archive API key configured")
    uri = f'/dyn/api/fast_download.json?md5={md5}&key={settings.annas_archive_api_key}'
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://{settings.annas_archive_domain}/{uri}') as response:
            data = await response.json()
            download_url = data.get('download_url')
            if not download_url:
                raise DownloadError("Anna's Archive API did not return a download URL")
        async with session.get(download_url) as response:
            file_data = await response.read()
    if not file_data:
        raise DownloadError(f"Downloaded file is empty (md5={md5})")
    return file_data, f'{md5}.{ext}'


async def _download_via_libgen(isbn: str, md5_list: list[str]) -> Path:
    libgen_mirror = await choose_libgen_mirror()
    url = await get_libgen_link(isbn, md5_list, libgen_mirror)
    return await asyncio.to_thread(download_book_using_selenium, url)


async def ebook_download_by_md5(md5: str, ext: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status):
        if on_status:
            on_status(status)

    _emit("downloading")

    # Fallback: AA API -> LibGen
    md5_list = [md5]
    book_path: Path | None = None
    file_data = b''
    filename = ''
    last_error: Exception | None = None

    for source in ("annas_api", "libgen"):
        try:
            if source == "annas_api":
                file_data, filename = await _download_via_annas_api(md5, ext)
            else:
                book_path = await _download_via_libgen(md5, md5_list)
            break
        except (ConnectionError, DownloadError, BookNotFoundError) as e:
            logger.warning(f"Download via {source} ({ext}) failed: {e}")
            last_error = e
    else:
        raise DownloadError(f"All download attempts failed for md5={md5}") from last_error

    _emit("sending")
    if book_path:
        await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)
    else:
        await asyncio.to_thread(send_to_kindle, kindle_mail, None, file_data, filename)

    _emit("done")


async def ebook_download(goodreads_url: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status):
        if on_status:
            on_status(status)

    _emit("fetching_isbn")
    isbn = await get_isbn(goodreads_url)

    _emit("searching")
    epub_hashes = await get_book_md5(isbn, ext="epub")
    pdf_hashes: list[str] = []
    if not epub_hashes:
        logger.warning(f"No epub results for ISBN {isbn}, falling back to pdf")
        pdf_hashes = await get_book_md5(isbn, ext="pdf")
    if not epub_hashes and not pdf_hashes:
        raise BookNotFoundError(f"No book found for ISBN {isbn}")

    _emit("downloading")

    # Fallback chain: AA epub -> AA pdf -> LibGen epub -> LibGen pdf
    attempts: list[tuple[str, list[str], str]] = []
    if epub_hashes:
        attempts.append(("annas_api", epub_hashes, "epub"))
    if pdf_hashes:
        attempts.append(("annas_api", pdf_hashes, "pdf"))
    if epub_hashes:
        attempts.append(("libgen", epub_hashes, "epub"))
    if pdf_hashes:
        attempts.append(("libgen", pdf_hashes, "pdf"))

    book_path: Path | None = None
    file_data = b''
    filename = ''
    last_error: Exception | None = None

    for source, md5_list, ext in attempts:
        try:
            if source == "annas_api":
                file_data, filename = await _download_via_annas_api(md5_list[0], ext)
            else:
                book_path = await _download_via_libgen(isbn, md5_list)
            break
        except (ConnectionError, DownloadError, BookNotFoundError) as e:
            logger.warning(f"Download via {source} ({ext}) failed: {e}")
            last_error = e
    else:
        raise DownloadError(f"All download attempts failed for ISBN {isbn}") from last_error

    _emit("sending")
    if book_path:
        await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)
    else:
        await asyncio.to_thread(send_to_kindle, kindle_mail, None, file_data, filename)

    _emit("done")
